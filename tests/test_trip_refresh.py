import http.client
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vw_app_connector import AppState, RequestHandler, TripData, VolkswagenReader


class TripRefreshTests(unittest.TestCase):
    def make_state(self, directory):
        with (
            patch.dict("os.environ", {
                "CACHE_STATE_DIR": str(Path(directory) / "cache"),
                "USAGE_STATE_FILE": str(Path(directory) / "usage.json"),
                "TRIP_POLL_ENABLED": "true", "TRIP_INTERVAL_SECONDS": "3600",
                "BACKGROUND_DAILY_LIMIT": "180", "BACKGROUND_MIN_INTERVAL_SECONDS": "300",
            }),
            patch("threading.Thread.start"),
            patch("vw_app_connector.VolkswagenReader") as reader_class,
            patch("vw_app_connector.MqttPublisher.from_environment", return_value=None),
        ):
            reader_class.error_category.side_effect = VolkswagenReader.error_category
            reader = reader_class.return_value
            reader.action_pending = threading.Event()
            reader.context = threading.local()
            reader.read_latest_trip.return_value = TripData(
                odometerKmAtObservation=107, distanceKm=7,
                tripEndedAtLocal="2026-01-01T10:38",
            )
            return AppState()

    def test_http_authentication_queue_coalescing_disabled_and_pause(self):
        with TemporaryDirectory() as directory:
            state = self.make_state(directory)
            handler = type("TripRefreshHandler", (RequestHandler,), {
                "state": state, "api_key": "test-key",
            })
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection(*server.server_address)

            def post(key=None):
                connection.request(
                    "POST", "/admin/refresh/trips",
                    headers={"X-API-Key": key} if key else {},
                )
                response = connection.getresponse()
                body = response.read()
                return response.status, json.loads(body) if response.status != 401 else None

            try:
                before = state.usage.snapshot()["backgroundUsed"]
                for key in (None, "wrong-key"):
                    self.assertEqual(post(key)[0], 401)
                    self.assertFalse(state.latest_trip.early_refresh_requested)
                status, result = post("test-key")
                self.assertEqual(status, 202)
                self.assertEqual(result["state"], "queued")
                self.assertEqual(result["budgetCost"], 2)
                self.assertTrue(result["budgetPacingPreserved"])
                self.assertTrue(state.latest_trip.get(trigger_refresh=False).refreshRequested)
                self.assertEqual(post("test-key")[1]["state"], "coalesced")
                state.latest_trip.refreshing = True
                self.assertEqual(post("test-key")[1]["state"], "coalesced")
                state.latest_trip.refreshing = False
                self.assertEqual(state.usage.snapshot()["backgroundUsed"], before)
                state.reader.read_latest_trip.assert_not_called()

                trip = state.latest_trip
                state.latest_trip = None
                status, result = post("test-key")
                self.assertEqual((status, result["reason"]), (409, "TRIPS_DISABLED"))
                state.latest_trip = trip
                trip.early_refresh_requested = False
                state.rest_mode.set(True)
                status, result = post("test-key")
                self.assertEqual((status, result["reason"]), (409, "REST_MODE_PAUSED"))
                self.assertFalse(trip.early_refresh_requested)

                handler.api_key = ""
                self.assertEqual(post("test-key")[0], 401)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join(2)

    def test_request_and_worker_keep_cost_and_background_minimum_interval(self):
        with TemporaryDirectory() as directory:
            state = self.make_state(directory)
            clock = [time.time()]
            with (
                patch("time.time", side_effect=lambda: clock[0]),
                patch("time.sleep", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)),
            ):
                state.usage.acquire_background(1)
                before = clock[0]
                result = state.request_trip_refresh()
                self.assertEqual(result["state"], "queued")
                self.assertEqual(state.usage.snapshot()["backgroundUsed"], 1)
                # Worker loader performs the paired read and spends exactly two
                # units after the same minimum interval as scheduled work.
                state.latest_trip.refresh()
                self.assertGreaterEqual(clock[0] - before, 300)
                self.assertEqual(state.usage.snapshot()["backgroundUsed"], 3)
                state.reader.read_latest_trip.assert_called_once_with(None)
                pair = state.latest_trip.get(trigger_refresh=False)
                self.assertEqual((pair.odometerKmAtObservation, pair.distanceKm), (107, 7))
                self.assertFalse(pair.refreshing)
                self.assertFalse(pair.refreshRequested)

    def test_request_keeps_existing_retry_and_active_protections(self):
        for reason in ("retry", "cooldown", "backoff", "budget", "pacing"):
            with self.subTest(reason=reason), TemporaryDirectory() as directory:
                state = self.make_state(directory)
                trip = state.latest_trip
                trip.initial_delay = 0
                if reason == "retry":
                    trip.next_attempt_monotonic = time.monotonic() + 3600
                elif reason == "cooldown":
                    state.usage.record_rate_limit("TOO_MANY_REQUESTS")
                elif reason == "backoff":
                    state.background_backoff.record_failure("APP_UNAVAILABLE")
                elif reason == "budget":
                    with state.usage.lock:
                        state.usage.state["backgroundUsed"] = 180
                else:
                    with state.usage.lock:
                        state.usage.state["backgroundUsed"] = 111
                        state.usage.state["backgroundLastByName"]["latest-trip"] = time.time()
                before = state.usage.snapshot()["backgroundUsed"]
                requested = state.request_trip_refresh()
                self.assertEqual(requested["status"], "accepted")
                self.assertTrue(trip.early_refresh_requested)
                if reason in ("retry", "backoff"):
                    trip.wakeup.wait = Mock(side_effect=RuntimeError("stop worker"))
                    with self.assertRaisesRegex(RuntimeError, "stop worker"):
                        trip._worker()
                    self.assertGreater(trip.wakeup.wait.call_args.args[0], 0)
                else:
                    result = trip.refresh()
                    self.assertEqual(result.errorCategory, (
                        "BACKGROUND_BUDGET_PACED" if reason == "pacing" else "RATE_LIMIT"
                    ))
                state.reader.read_latest_trip.assert_not_called()
                self.assertEqual(state.usage.snapshot()["backgroundUsed"], before)


if __name__ == "__main__":
    unittest.main()
