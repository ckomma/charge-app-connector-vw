import json
import threading
import unittest
from dataclasses import asdict, is_dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from mqtt_publisher import MqttPublisher
from test_parser import FakeMqttModule
from vw_app_connector import (
    AppState, BackgroundCache, DetailData, LocationData, RequestHandler,
    SoftwareData, TransientEndpointState, TripData, VehicleData,
)


class RecordingMqtt:
    def __init__(self):
        self.values = []

    def publish_state(self, name, value):
        payload = asdict(value) if is_dataclass(value) else value
        self.values.append((name, json.loads(json.dumps(payload))))


class RefreshActivityTests(unittest.TestCase):
    def test_cached_payloads_expose_queue_state_without_ui_read_or_mutation(self):
        for factory in (VehicleData, DetailData, LocationData, SoftwareData, TripData):
            with self.subTest(factory=factory.__name__):
                loader = Mock()
                cache = BackgroundCache("test", loader, lambda _: 3600, factory, auto_refresh=False)
                cache.set_value(factory(observedAt="2026-01-01T10:00:00+00:00"))
                previous = asdict(cache.value)
                cache.request_early_refresh()
                payload = cache.get(trigger_refresh=False)
                self.assertTrue(payload.refreshRequested)
                self.assertFalse(payload.refreshing)
                self.assertEqual(payload.refreshStartedAt, "")
                self.assertEqual(payload.lastSuccessfulAt, previous["lastSuccessfulAt"])
                self.assertEqual(payload.observedAt, previous["observedAt"])
                self.assertEqual(asdict(cache.value), previous)
                loader.assert_not_called()

    def test_trip_http_and_mqtt_show_running_then_completed_pair(self):
        started = threading.Event()
        release = threading.Event()

        def load():
            started.set()
            if not release.wait(3):
                raise RuntimeError("Test loader was not released")
            return TripData(
                odometerKmAtObservation=107, distanceKm=7,
                tripEndedAtLocal="2026-01-01T10:38", observedAt="2026-01-01T10:58:00+00:00",
            )

        state = AppState.__new__(AppState)
        state.mqtt = RecordingMqtt()
        cache = BackgroundCache(
            "latest-trip", load, lambda _: 3600, TripData, auto_refresh=False,
            on_refresh_state=lambda: state._refresh_state_updated("latest-trip"),
        )
        state.latest_trip = cache
        cache.set_value(TripData(
            odometerKmAtObservation=100, distanceKm=8,
            tripEndedAtLocal="2025-12-31T18:21", observedAt="2025-12-31T18:25:00+00:00",
        ))
        old_success = cache.last_success_at
        cache.request_early_refresh()
        handler = RequestHandler.__new__(RequestHandler)
        handler.path = "/trips/latest"
        handler.state = state
        handler.send_json = Mock()
        worker = threading.Thread(target=cache.refresh)
        worker.start()
        try:
            self.assertTrue(started.wait(1))
            handler.do_GET()
            payload, status = handler.send_json.call_args.args
            self.assertEqual(status, 200)
            self.assertTrue(payload["refreshing"])
            self.assertTrue(payload["refreshStartedAt"])
            self.assertEqual(payload["odometerKmAtObservation"], 100)
            self.assertEqual(payload["lastSuccessfulAt"], old_success)
            self.assertEqual(state.refresh_activity()["trips"]["state"], "running")
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        handler.do_GET()
        payload, status = handler.send_json.call_args.args
        self.assertFalse(payload["refreshing"])
        self.assertFalse(payload["refreshRequested"])
        self.assertEqual(payload["refreshStartedAt"], "")
        self.assertEqual((payload["odometerKmAtObservation"], payload["distanceKm"]), (107, 7))
        states = [p["trips"]["state"] for n, p in state.mqtt.values if n == "activity"]
        self.assertEqual(states, ["queued", "running", "idle"])
        latest_payload = [p for n, p in state.mqtt.values if n == "latest-trip"][-1]
        self.assertFalse(latest_payload["refreshing"])
        self.assertEqual(latest_payload["odometerKmAtObservation"], 107)

    def test_failure_clears_running_and_preserves_old_measurement(self):
        cache = BackgroundCache(
            "charge", Mock(side_effect=RuntimeError("Unavailable")),
            lambda _: 900, VehicleData, auto_refresh=False,
        )
        cache.set_value(VehicleData(soc=50, observedAt="old"))
        old_success = cache.last_success_at
        cache.refresh()
        result = cache.get(trigger_refresh=False)
        self.assertFalse(result.refreshing)
        self.assertEqual(result.refreshStartedAt, "")
        self.assertGreater(result.retryInSeconds, 0)
        self.assertEqual(result.soc, 50)
        self.assertEqual(result.observedAt, "old")
        self.assertEqual(result.lastSuccessfulAt, old_success)

    def test_pacing_is_waiting_not_running_and_keeps_queued_request(self):
        state = AppState.__new__(AppState)
        cache = BackgroundCache(
            "latest-trip", Mock(side_effect=TransientEndpointState(
                "BACKGROUND_BUDGET_PACED", "Wait", retry_after_seconds=120,
            )), lambda _: 3600, TripData, auto_refresh=False,
        )
        state.latest_trip = cache
        cache.set_value(TripData(odometerKmAtObservation=100))
        cache.request_early_refresh()
        cache.refresh()
        self.assertEqual(state.refresh_activity()["trips"]["state"], "waiting")
        result = cache.get(trigger_refresh=False)
        self.assertFalse(result.refreshing)
        self.assertTrue(result.refreshRequested)
        self.assertGreater(result.retryInSeconds, 0)
        self.assertEqual(state.refresh_activity()["software"]["state"], "disabled")

    def test_progress_is_not_restored_as_a_running_job_after_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            cache = BackgroundCache(
                "charge", Mock(), lambda _: 900, VehicleData,
                auto_refresh=False, state_path=path,
            )
            cache.set_value(VehicleData(soc=50))
            cache.refreshing = True
            cache.refresh_started_at = "2026-01-01T10:00:00+00:00"
            cache._save_persisted(cache.get(trigger_refresh=False))
            raw = json.loads(path.read_text())["value"]
            self.assertNotIn("refreshing", raw)
            restored = BackgroundCache(
                "charge", Mock(), lambda _: 900, VehicleData,
                auto_refresh=False, state_path=path,
            )
            self.assertFalse(restored.get(trigger_refresh=False).refreshing)
            self.assertEqual(restored.get(trigger_refresh=False).refreshStartedAt, "")

    def test_initial_read_exposes_running_with_http_503_until_first_success(self):
        state = AppState.__new__(AppState)
        state.latest_trip = BackgroundCache(
            "latest-trip", Mock(), lambda _: 3600, TripData, auto_refresh=False,
        )
        state.latest_trip.refreshing = True
        state.latest_trip.refresh_started_at = "2026-01-01T10:00:00+00:00"
        handler = RequestHandler.__new__(RequestHandler)
        handler.path = "/trips/latest"
        handler.state = state
        handler.send_json = Mock()
        handler.do_GET()
        payload, status = handler.send_json.call_args.args
        self.assertEqual(status, 503)
        self.assertTrue(payload["refreshing"])
        self.assertFalse(payload["lastSuccessfulAt"])

    def test_mqtt_discovers_diagnostic_refresh_states_without_network_access(self):
        module = FakeMqttModule()
        with patch.dict("os.environ", {"MQTT_HOST": "example.invalid"}):
            publisher = MqttPublisher(lambda: {}, mqtt_module=module)
        publisher._publish_discovery()
        configs = {
            args[0]: json.loads(args[1]) for args, _ in module.client.published
        }
        for name in ("charge", "details", "location", "trips", "software"):
            config = next(v for k, v in configs.items() if k.endswith(f"/{name}_refresh/config"))
            self.assertEqual(config["entity_category"], "diagnostic")
            self.assertTrue(config["state_topic"].endswith("/activity"))
            self.assertIn(f"value_json.{name}.state", config["value_template"])
            self.assertIn(f"value_json.{name}", config["json_attributes_template"])


if __name__ == "__main__":
    unittest.main()
