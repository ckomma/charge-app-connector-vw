import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from vw_app_connector import (
    ActionPriority, ActionQuarantined, AppState, BackgroundCache,
    RequestHandler, RestMode, VehicleData,
)


class RestModeTests(unittest.TestCase):
    def make_state(self, directory):
        state = object.__new__(AppState)
        state.rest_mode = RestMode(Path(directory) / "rest-mode.json")
        state.usage = Mock(lock=threading.Lock())
        state.mqtt = None
        state.verified_app_version = "4.8.1"
        for name in ("charge", "details", "location", "software"):
            cache = BackgroundCache(name, Mock(return_value=VehicleData(soc=80)),
                                    lambda _: 900, VehicleData, auto_refresh=False,
                                    rest_mode=state.rest_mode)
            cache.set_value(VehicleData(soc=80))
            setattr(state, name, cache)
        return state

    def test_default_restart_idempotence_and_freshness(self):
        with TemporaryDirectory() as directory:
            state = self.make_state(directory)
            self.assertFalse(state.rest_mode.paused)
            state.set_rest_mode(True)
            self.assertTrue(RestMode(state.rest_mode.path).paused)
            self.assertFalse(state.set_rest_mode(True)["changed"])
            self.assertFalse(state.charge.refresh_due())
            value = state.charge.get()
            self.assertTrue(value.stale)
            self.assertEqual(value.errorCategory, "REST_MODE_PAUSED")
            self.assertFalse(state.charge.value.stale)
            with self.assertRaises(ActionQuarantined):
                state.ensure_action_allowed("charging/settings")
            self.assertEqual(state.request_charge_refresh()["status"], "paused")
            state.set_rest_mode(False)
            self.assertFalse(RestMode(state.rest_mode.path).paused)
            self.assertTrue(state.charge.early_refresh_requested)
            self.assertEqual(state.charge.get().errorCategory, "REST_MODE_RESUMING")
            state.charge.last_success_at = "9999"
            self.assertFalse(state.charge.get().stale)

    def test_pause_drains_running_background_work(self):
        with TemporaryDirectory() as directory:
            state = self.make_state(directory)
            entered, release, paused = threading.Event(), threading.Event(), threading.Event()
            def running():
                with state.rest_mode.lock:
                    entered.set()
                    release.wait(2)
            worker = threading.Thread(target=running)
            worker.start()
            self.assertTrue(entered.wait(1))
            transition = threading.Thread(target=lambda: (state.set_rest_mode(True), paused.set()))
            transition.start()
            self.assertFalse(paused.wait(.05))
            release.set()
            worker.join(2)
            transition.join(2)
            self.assertTrue(paused.is_set())

    def test_paused_background_never_probes_phone_or_acquires_budget(self):
        with TemporaryDirectory() as directory:
            state = self.make_state(directory)
            state.rest_mode.set(True)
            state.reader = Mock()
            state.priority_lock = threading.Lock()
            state.priority_waiters = 0
            state.usage.background_min_interval = 300
            loader = Mock()
            with self.assertRaises(ActionPriority):
                state.background_loader(loader, 1)()
            state.reader.require_background_adb_transport.assert_not_called()
            state.usage.acquire_background.assert_not_called()
            loader.assert_not_called()

    def test_http_authentication_and_pause_resume(self):
        with TemporaryDirectory() as directory:
            state = self.make_state(directory)
            handler = type("RestHandler", (RequestHandler,), {"state": state, "api_key": "test-key"})
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(urllib.request.Request(url + "/admin/rest/pause", method="POST"))
                self.assertEqual(error.exception.code, 401)
                self.assertFalse(state.rest_mode.paused)
                for operation, expected in (("pause", True), ("resume", False)):
                    request = urllib.request.Request(url + "/admin/rest/" + operation,
                                                     method="POST", headers={"X-API-Key": "test-key"})
                    with urllib.request.urlopen(request) as response:
                        self.assertEqual(json.load(response)["paused"], expected)
                    if expected:
                        with urllib.request.urlopen(url + "/charge") as response:
                            self.assertTrue(json.load(response)["stale"])
                        action = urllib.request.Request(url + "/action/charging/settings", method="POST",
                                                        headers={"X-API-Key": "test-key"})
                        with self.assertRaises(urllib.error.HTTPError) as blocked:
                            urllib.request.urlopen(action)
                        self.assertEqual(blocked.exception.code, 409)
                        self.assertEqual(json.load(blocked.exception)["errorCategory"], "REST_MODE_PAUSED")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)


if __name__ == "__main__":
    unittest.main()
