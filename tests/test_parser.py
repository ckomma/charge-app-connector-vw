import http.client
import json
import subprocess
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from vw_app_connector import (
    ActionJobManager,
    ActionQuarantined,
    AppState,
    BackgroundCache,
    BackgroundTransientBackoff,
    ChargeRefreshInterval,
    ChargingLocationSettingsData,
    ChargingLocationsData,
    ChargingSettingsData,
    CooldownProbeRejected,
    DetailData,
    HealthData,
    IdempotencyConflict,
    LocationData,
    RequestHandler,
    TransientTransportState,
    TransientVolkswagenState,
    UsageLimit,
    UsageLimiter,
    VehicleData,
    VolkswagenRateLimit,
    VolkswagenReader,
)
from http.server import ThreadingHTTPServer
from mqtt_publisher import MqttPublisher


class FakeMqttClient:
    def __init__(self) -> None:
        self.published = []
        self.username = None
        self.password = None

    def username_pw_set(self, username, password):
        self.username = username
        self.password = password

    def tls_set(self):
        self.tls = True

    def will_set(self, *args, **kwargs):
        self.will = (args, kwargs)

    def reconnect_delay_set(self, **kwargs):
        self.reconnect = kwargs

    def connect_async(self, *args, **kwargs):
        self.connection = (args, kwargs)

    def loop_start(self):
        self.loop_started = True

    def publish(self, *args, **kwargs):
        self.published.append((args, kwargs))

    def disconnect(self):
        pass

    def loop_stop(self):
        pass


class FakeMqttModule:
    def __init__(self) -> None:
        self.client = FakeMqttClient()

    def Client(self, **kwargs):
        self.client.client_options = kwargs
        return self.client


class ParserTests(unittest.TestCase):
    def test_app_version_policy(self):
        self.assertEqual(AppState.version_policy("3.63.2", "3.63.2"), (True, ""))
        self.assertEqual(
            AppState.version_policy("3.64.0", "3.63.2"),
            (False, "UNVERIFIED_APP_VERSION"),
        )
        self.assertEqual(
            AppState.version_policy("", "3.63.2"),
            (False, "APP_VERSION_UNKNOWN"),
        )
        self.assertEqual(AppState.version_policy("3.64.0", ""), (True, ""))

    def test_quarantine_skips_read_only_actions(self):
        state = object.__new__(AppState)
        state.verified_app_version = "3.63.2"
        state.reader = Mock()
        state.ensure_action_allowed("charging/settings")
        state.reader.phone_health.assert_not_called()

        with self.assertRaises(ActionQuarantined):
            state.ensure_action_allowed("charging/start", "3.64.0")

    def test_health_reports_quarantine_as_degraded(self):
        state = object.__new__(AppState)
        state.verified_app_version = "3.63.2"
        state.reader = Mock()
        state.reader.phone_health.return_value = HealthData(
            adbState="device", appVersion="3.64.0"
        )
        state.charge = SimpleNamespace(
            last_success_at="now",
            refreshing=False,
            value=VehicleData(status="B"),
            last_error="",
            age=lambda: 1,
        )
        state.details = SimpleNamespace(last_success_at="", age=lambda: 0)
        state.location = SimpleNamespace(last_success_at="", age=lambda: 0)
        state.usage = Mock()
        state.usage.snapshot.return_value = {
            "backgroundUsed": 0,
            "backgroundLimit": 180,
            "actionsUsed": 0,
            "actionsLimit": 20,
            "cooldownSeconds": 0,
        }
        health = state.health()
        self.assertEqual(health.status, "degraded")
        self.assertFalse(health.actionAvailable)
        self.assertEqual(health.actionBlockedReason, "UNVERIFIED_APP_VERSION")

    def test_action_job_lifecycle(self):
        completed = threading.Event()

        def execute(action, query):
            completed.set()
            return ChargingSettingsData(targetSoc=int(query["value"][0]))

        jobs = ActionJobManager(execute)
        submitted = jobs.submit("charging/target-soc", {"value": ["80"]})
        self.assertEqual(submitted["state"], "queued")
        self.assertTrue(completed.wait(1))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            result = jobs.snapshot(str(submitted["jobId"]))
            if result and result["state"] == "succeeded":
                break
            time.sleep(0.01)
        assert result is not None
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result"]["targetSoc"], 80)

    def test_action_job_preserves_transient_volkswagen_category(self):
        jobs = ActionJobManager(
            lambda _action, _query: (_ for _ in ()).throw(
                TransientVolkswagenState("APP_UNAVAILABLE", "unavailable")
            )
        )
        submitted = jobs.submit("charging/settings", {})
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            result = jobs.snapshot(str(submitted["jobId"]))
            if result and result["state"] == "failed":
                break
            time.sleep(0.01)
        self.assertIsNotNone(result)
        self.assertEqual(result["errorCategory"], "APP_UNAVAILABLE")

    def test_action_jobs_are_idempotent(self):
        release = threading.Event()
        calls = []

        def execute(action, query):
            calls.append((action, query))
            release.wait(1)
            return ChargingSettingsData(targetSoc=80)

        jobs = ActionJobManager(execute)
        first = jobs.submit(
            "charging/target-soc", {"value": ["80"]}, "request-1"
        )
        duplicate = jobs.submit(
            "charging/target-soc", {"value": ["80"]}, "request-1"
        )
        self.assertEqual(first["jobId"], duplicate["jobId"])
        with self.assertRaises(IdempotencyConflict):
            jobs.submit("charging/target-soc", {"value": ["90"]}, "request-1")
        release.set()

    def test_concurrent_action_jobs_are_atomically_idempotent(self):
        release_executor = threading.Event()
        release_uuid = threading.Event()
        first_uuid_call = threading.Event()
        both_uuid_calls = threading.Event()
        uuid_calls = []
        uuid_lock = threading.Lock()
        results = []
        errors = []

        def execute(_action, _query):
            release_executor.wait(1)
            return ChargingSettingsData(targetSoc=80)

        def slow_uuid():
            with uuid_lock:
                uuid_calls.append(len(uuid_calls) + 1)
                index = uuid_calls[-1]
                first_uuid_call.set()
                if len(uuid_calls) == 2:
                    both_uuid_calls.set()
            release_uuid.wait(1)
            return SimpleNamespace(hex=f"{index:032x}")

        def submit():
            try:
                results.append(
                    jobs.submit(
                        "charging/target-soc", {"value": ["80"]}, "request-1"
                    )
                )
            except Exception as exc:  # pragma: no cover - assertion aid
                errors.append(exc)

        jobs = ActionJobManager(execute)
        with patch("vw_app_connector.uuid.uuid4", side_effect=slow_uuid):
            first = threading.Thread(target=submit)
            second = threading.Thread(target=submit)
            first.start()
            self.assertTrue(first_uuid_call.wait(1))
            second.start()
            both_uuid_calls.wait(0.1)
            release_uuid.set()
            first.join(1)
            second.join(1)

        release_executor.set()
        self.assertEqual(errors, [])
        self.assertEqual(len(uuid_calls), 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["jobId"], results[1]["jobId"])
        self.assertEqual(len(jobs.jobs), 1)

    def test_http_action_contracts_remain_backward_compatible(self):
        state = Mock()
        state.action.return_value = ChargingSettingsData(targetSoc=80)
        state.submit_action.return_value = {"jobId": "job-1", "state": "queued"}
        state.action_job.return_value = {
            "jobId": "job-1",
            "state": "succeeded",
            "result": {"targetSoc": 80},
        }
        RequestHandler.state = state
        RequestHandler.api_key = "test-key"
        server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address)
            headers = {"X-API-Key": "test-key"}
            connection.request(
                "POST", "/action/charging/target-soc?value=80", headers=headers
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["targetSoc"], 80)

            async_headers = {**headers, "Prefer": "respond-async"}
            connection.request(
                "POST",
                "/action/charging/target-soc?value=80",
                headers=async_headers,
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 202)
            self.assertEqual(response.getheader("Location"), "/actions/job-1")
            self.assertEqual(json.loads(response.read())["state"], "queued")

            connection.request("GET", "/actions/job-1", headers=headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["state"], "succeeded")
        finally:
            server.shutdown()
            server.server_close()

    def test_http_operational_endpoints(self):
        state = Mock()
        state.capabilities.return_value = {"version": 1}
        state.metrics_text.return_value = "vw_app_connector_up 1\n"
        state.diagnostics_index.return_value = {"count": 0, "entries": []}
        RequestHandler.state = state
        RequestHandler.api_key = "test-key"
        server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address)
            connection.request("GET", "/capabilities")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["version"], 1)

            connection.request("GET", "/metrics")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("text/plain", response.getheader("Content-Type"))
            self.assertEqual(response.read().decode(), "vw_app_connector_up 1\n")

            connection.request("GET", "/diagnostics?limit=5")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["count"], 0)
            state.diagnostics_index.assert_called_with(5)
        finally:
            server.shutdown()
            server.server_close()

    def test_http_cooldown_probe_requires_authentication(self):
        state = Mock()
        state.probe_cooldown.return_value = {
            "status": "succeeded",
            "cooldownCleared": True,
        }
        RequestHandler.state = state
        RequestHandler.api_key = "test-key"
        server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address)
            connection.request("POST", "/admin/cooldown/probe")
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            state.probe_cooldown.assert_not_called()

            connection.request(
                "POST",
                "/admin/cooldown/probe",
                headers={"X-API-Key": "test-key"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())["cooldownCleared"])
            state.probe_cooldown.assert_called_once_with()
        finally:
            server.shutdown()
            server.server_close()

    def test_capabilities_describe_actions_and_caches_without_refresh(self):
        state = object.__new__(AppState)
        state.verified_app_version = "3.63.2"
        state.reader = Mock()
        state.reader.phone_health.return_value = HealthData(
            status="ok",
            adbState="device",
            adbMode="auto",
            adbTransport="usb",
            appVersion="3.63.2",
        )
        state.mqtt = object()
        state.usage = Mock()
        state.usage.snapshot.return_value = {
            "backgroundUsed": 1,
            "backgroundLimit": 180,
            "actionsUsed": 2,
            "actionsLimit": 20,
            "cooldownSeconds": 0,
        }
        with patch("threading.Thread.start"):
            state.charge = BackgroundCache(
                "charge", VehicleData, lambda _: 900, VehicleData
            )
            state.details = BackgroundCache(
                "details", ChargingSettingsData, lambda _: 900, ChargingSettingsData
            )
            state.location = BackgroundCache(
                "location", LocationData, lambda _: 900, LocationData
            )
        state.charge.set_value(VehicleData(status="B", soc=55))

        result = state.capabilities()

        self.assertTrue(result["features"]["mqtt"])
        self.assertIn("charging/target-soc", result["actions"]["write"])
        self.assertIn("charging/settings", result["actions"]["readOnly"])
        self.assertEqual(
            result["administrativeEndpoints"]["cooldownProbe"],
            "/admin/cooldown/probe",
        )
        self.assertTrue(result["caches"]["charge"]["available"])
        self.assertFalse(result["caches"]["details"]["available"])

    def test_metrics_report_usage_cache_and_version_labels(self):
        state = object.__new__(AppState)
        state.verified_app_version = "3.63.2"
        state.reader = Mock()
        state.reader.phone_health.return_value = HealthData(
            adbState="device",
            adbTransport="usb",
            appVersion="3.63.2",
            phoneBatteryLevel=87,
        )
        state.usage = Mock()
        state.usage.snapshot.return_value = {
            "backgroundUsed": 3,
            "backgroundLimit": 180,
            "actionsUsed": 4,
            "actionsLimit": 20,
            "cooldownSeconds": 0,
        }
        with patch("threading.Thread.start"):
            state.charge = BackgroundCache(
                "charge", VehicleData, lambda _: 900, VehicleData
            )
            state.details = BackgroundCache(
                "details", ChargingSettingsData, lambda _: 900, ChargingSettingsData
            )
            state.location = BackgroundCache(
                "location", LocationData, lambda _: 900, LocationData
            )
        state.charge.set_value(VehicleData(status="B", soc=55))

        text = state.metrics_text()

        self.assertIn('vw_app_connector_usage_used{kind="background"} 3', text)
        self.assertIn("vw_app_connector_phone_battery_level_percent 87", text)
        self.assertIn('vw_app_connector_cache_age_seconds{cache="charge"}', text)
        self.assertIn(
            'vw_app_connector_app_version_info{app_version="3.63.2",'
            'verified_app_version="3.63.2",verified="true"} 1',
            text,
        )

    def test_diagnostics_index_returns_only_safe_metadata(self):
        with TemporaryDirectory() as directory:
            diagnostics_dir = Path(directory)
            (diagnostics_dir / "20260628-120000-location.txt").write_text(
                "RuntimeError: Example Street 1 should not be exposed\n",
                encoding="utf-8",
            )
            (diagnostics_dir / "20260628-120000-location.xml").write_text(
                "<hierarchy><node text='sensitive ui'/></hierarchy>",
                encoding="utf-8",
            )
            (diagnostics_dir / "20260628-120000-location.png").write_bytes(
                b"screenshot-bytes"
            )
            state = object.__new__(AppState)
            state.reader = SimpleNamespace(diagnostics_dir=diagnostics_dir)

            result = state.diagnostics_index()

            self.assertEqual(result["count"], 1)
            entry = result["entries"][0]
            self.assertEqual(entry["category"], "LOCATION")
            self.assertEqual(entry["errorType"], "RuntimeError")
            self.assertTrue(entry["artifacts"]["summary"])
            self.assertTrue(entry["artifacts"]["uiDump"])
            self.assertTrue(entry["artifacts"]["screenshot"])
            self.assertNotIn("Example Street", json.dumps(result))

    def test_http_quarantine_returns_409(self):
        state = Mock()
        state.action.side_effect = ActionQuarantined(
            "UNVERIFIED_APP_VERSION", "3.64.0", "3.63.2"
        )
        RequestHandler.state = state
        RequestHandler.api_key = "test-key"
        server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address)
            connection.request(
                "POST",
                "/action/charging/start",
                headers={"X-API-Key": "test-key"},
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 409)
            payload = json.loads(response.read())
            self.assertEqual(payload["reason"], "UNVERIFIED_APP_VERSION")
        finally:
            server.shutdown()
            server.server_close()

    def test_mqtt_discovery_and_retained_state_are_published_on_connect(self):
        mqtt = FakeMqttModule()
        environment = {
            "MQTT_HOST": "mqtt.example",
            "MQTT_USERNAME": "connector",
            "MQTT_PASSWORD": "secret",
        }
        state = VehicleData(status="B", soc=0, locked=True)
        with patch.dict("os.environ", environment, clear=True):
            publisher = MqttPublisher(lambda: {"charge": state}, mqtt)
            publisher.start()
            publisher._on_connect(mqtt.client, None, None, 0)

        topics = [call[0][0] for call in mqtt.client.published]
        self.assertIn("vw_app_connector/charge", topics)
        self.assertIn("vw_app_connector/availability", topics)
        self.assertIn(
            "homeassistant/sensor/vw-app-connector/soc/config", topics
        )
        self.assertIn(
            "homeassistant/device_tracker/vw-app-connector/location/config",
            topics,
        )
        self.assertIn(
            "homeassistant/sensor/vw-app-connector/connector_status/config",
            topics,
        )
        self.assertIn(
            "homeassistant/binary_sensor/vw-app-connector/action_available/config",
            topics,
        )
        self.assertIn(
            "homeassistant/binary_sensor/vw-app-connector/locked/config",
            topics,
        )
        self.assertIn(
            "homeassistant/binary_sensor/vw-app-connector/automatic_window_heating/config",
            topics,
        )
        self.assertIn(
            "homeassistant/binary_sensor/vw-app-connector/climate_zone_front_left/config",
            topics,
        )
        self.assertIn(
            "homeassistant/binary_sensor/vw-app-connector/climate_zone_front_right/config",
            topics,
        )
        state_call = next(
            call for call in mqtt.client.published
            if call[0][0] == "vw_app_connector/charge"
        )
        self.assertEqual(json.loads(state_call[0][1])["soc"], 0)
        self.assertTrue(state_call[1]["retain"])
        locked_config_call = next(
            call for call in mqtt.client.published
            if call[0][0] == "homeassistant/binary_sensor/vw-app-connector/locked/config"
        )
        locked_config = json.loads(locked_config_call[0][1])
        self.assertEqual(locked_config["name"], "Vehicle locked")
        self.assertEqual(locked_config["unique_id"], "vw-app-connector_locked")
        self.assertEqual(mqtt.client.username, "connector")
        self.assertEqual(mqtt.client.password, "secret")

        health_config_call = next(
            call for call in mqtt.client.published
            if call[0][0] == "homeassistant/sensor/vw-app-connector/connector_status/config"
        )
        health_config = json.loads(health_config_call[0][1])
        self.assertEqual(health_config["name"], "Connector health")
        self.assertEqual(health_config["state_topic"], "vw_app_connector/health")

        location_config_call = next(
            call for call in mqtt.client.published
            if call[0][0] == "homeassistant/device_tracker/vw-app-connector/location/config"
        )
        location_config = json.loads(location_config_call[0][1])
        self.assertNotIn("state_topic", location_config)
        self.assertNotIn("value_template", location_config)
        self.assertEqual(
            location_config["json_attributes_topic"], "vw_app_connector/location"
        )

    def test_mqtt_is_disabled_without_host(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(MqttPublisher.from_environment(lambda: {}))

    def test_background_cache_notifies_after_successful_update(self):
        updates = []
        with patch("threading.Thread.start"):
            cache = BackgroundCache(
                "charge",
                lambda: VehicleData(status="B", soc=60),
                lambda _: 60,
                VehicleData,
                on_update=lambda name, value: updates.append((name, value.soc)),
            )
        cache.refresh()
        self.assertEqual(updates, [("charge", 60)])

    def test_charge_refresh_interval_schedules_one_connected_follow_up(self):
        interval = ChargeRefreshInterval(300, 900)

        interval.observe(VehicleData(status="A", targetSoc=None))
        self.assertEqual(interval(VehicleData(status="A")), 900)

        connected = VehicleData(status="B", targetSoc=80)
        interval.observe(connected)
        self.assertEqual(interval(connected), 300)
        self.assertEqual(interval(connected), 300)

        interval.observe(connected)
        self.assertEqual(interval(connected), 900)

    def test_charge_refresh_interval_bounds_missing_target_retry(self):
        interval = ChargeRefreshInterval(300, 900)
        connected = VehicleData(status="B", targetSoc=None)

        interval.observe(connected)
        self.assertEqual(interval(connected), 300)

        interval.observe(connected)
        self.assertEqual(interval(connected), 900)

    def test_charge_refresh_interval_does_not_rearm_restored_connected_cache(self):
        interval = ChargeRefreshInterval(300, 900)
        restored = VehicleData(status="B", targetSoc=None)

        self.assertEqual(interval(restored), 900)
        interval.observe(restored)
        self.assertEqual(interval(restored), 900)

    def test_charge_refresh_interval_keeps_charging_poll_rate(self):
        interval = ChargeRefreshInterval(300, 900)
        charging = VehicleData(status="C", targetSoc=100)

        interval.observe(charging)
        self.assertEqual(interval(charging), 300)

    def test_charge_refresh_interval_schedules_one_post_charging_follow_up(self):
        interval = ChargeRefreshInterval(300, 900)
        charging = VehicleData(status="C", targetSoc=80)
        connected = VehicleData(status="B", targetSoc=80)

        interval.observe(charging)
        interval.observe(connected)
        self.assertEqual(interval(connected), 300)

        interval.observe(connected)
        self.assertEqual(interval(connected), 900)

    def test_mqtt_failure_does_not_escape_cache_update(self):
        state = object.__new__(AppState)
        state.mqtt = Mock()
        state.mqtt.publish_state.side_effect = RuntimeError("broker unavailable")
        with self.assertLogs("vw-app-connector", level="ERROR"):
            state._cache_updated("charge", VehicleData(status="B"))

    def test_every_mqtt_cache_update_also_publishes_health(self):
        state = object.__new__(AppState)
        state.mqtt = Mock()
        state.health = Mock(return_value=HealthData(status="ok"))
        details = DetailData()

        state._cache_updated("details", details)

        self.assertEqual(state.mqtt.publish_state.call_count, 2)
        state.mqtt.publish_state.assert_any_call("details", details)
        state.mqtt.publish_state.assert_any_call("health", state.health.return_value)

    def test_phone_health_redacts_adb_identifiers(self):
        reader = object.__new__(VolkswagenReader)
        reader.adb_mode = "auto"
        reader.adb_transport = "usb"
        reader.serial = "USB-SERIAL-SECRET"
        reader.usb_serial = "USB-SERIAL-SECRET"
        reader.wifi_address = "192.0.2.44:5555"
        reader.adb_last_connect_error = "cannot connect to 192.0.2.44:5555"
        reader.adb = Mock(
            side_effect=RuntimeError(
                "ADB failed (1): device 'USB-SERIAL-SECRET' not found"
            )
        )

        health = reader.phone_health()

        public_error = f"{health.adbState} {health.adbLastConnectError}"
        self.assertNotIn("USB-SERIAL-SECRET", public_error)
        self.assertNotIn("192.0.2.44:5555", public_error)
        self.assertIn("<redacted>", public_error)

    def test_phone_health_redacts_previous_adb_error_after_recovery(self):
        reader = object.__new__(VolkswagenReader)
        reader.adb_mode = "auto"
        reader.adb_transport = "usb"
        reader.serial = "USB-SERIAL-SECRET"
        reader.usb_serial = "USB-SERIAL-SECRET"
        reader.wifi_address = ""
        reader.adb_last_connect_error = "device USB-SERIAL-SECRET not found"
        reader.package = "com.volkswagen.weconnect"
        reader.adb = Mock(return_value="device\n")
        reader.shell = Mock(
            side_effect=(
                "level: 50\ntemperature: 250\nUSB powered: true\nstatus: 2\n",
                "versionName=4.1.1\n",
            )
        )

        health = reader.phone_health()

        self.assertEqual(health.status, "ok")
        self.assertNotIn("USB-SERIAL-SECRET", health.adbLastConnectError)
        self.assertIn("<redacted>", health.adbLastConnectError)

    def test_adb_timeout_redacts_command_device_identifier(self):
        reader = object.__new__(VolkswagenReader)
        reader.serial = "USB-SERIAL-SECRET"
        reader.usb_serial = "USB-SERIAL-SECRET"
        reader.wifi_address = ""
        reader.select_serial = Mock(return_value=reader.serial)

        with (
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    ["adb", "-s", reader.serial, "get-state"], 5
                ),
            ),
            self.assertRaises(TimeoutError) as caught,
        ):
            reader.adb("get-state", timeout=5)

        self.assertNotIn(reader.serial, str(caught.exception))
        self.assertIn("<redacted>", str(caught.exception))

    def test_adb_recovers_once_after_device_not_found(self):
        reader = object.__new__(VolkswagenReader)
        reader.serial = "USB-SERIAL-SECRET"
        reader.usb_serial = "USB-SERIAL-SECRET"
        reader.wifi_address = ""
        reader.select_serial = Mock(return_value=reader.serial)
        reader.recover_adb_transport = Mock()
        missing = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="adb: device USB-SERIAL-SECRET not found",
        )
        recovered = SimpleNamespace(returncode=0, stdout="device\n", stderr="")

        with patch("subprocess.run", side_effect=(missing, recovered)) as run:
            result = reader.adb("get-state", timeout=5)

        self.assertEqual(result, "device\n")
        self.assertEqual(run.call_count, 2)
        reader.recover_adb_transport.assert_called_once_with()

    def test_adb_does_not_retry_non_transport_error(self):
        reader = object.__new__(VolkswagenReader)
        reader.serial = "USB-SERIAL-SECRET"
        reader.usb_serial = "USB-SERIAL-SECRET"
        reader.wifi_address = ""
        reader.select_serial = Mock(return_value=reader.serial)
        reader.recover_adb_transport = Mock()
        denied = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="remote permission denied",
        )

        with (
            patch("subprocess.run", return_value=denied) as run,
            self.assertRaisesRegex(RuntimeError, "permission denied"),
        ):
            reader.adb("shell", "id")

        run.assert_called_once()
        reader.recover_adb_transport.assert_not_called()

    def test_adb_recovery_restarts_server_when_reconnect_is_insufficient(self):
        reader = object.__new__(VolkswagenReader)
        reader.adb_recovery_lock = threading.Lock()
        reader.adb_transport_ready = Mock(side_effect=(False, False))
        reader.run_adb = Mock(return_value=SimpleNamespace(returncode=0))

        with self.assertLogs("vw-app-connector", level="WARNING"):
            reader.recover_adb_transport()

        self.assertEqual(
            reader.run_adb.call_args_list,
            [
                call("reconnect", "offline", timeout=10),
                call("kill-server", timeout=10),
                call("start-server", timeout=10),
            ],
        )

    def test_charging_setting_values_are_sorted_and_localization_independent(self):
        root = ET.fromstring(
            """<hierarchy>
            <node resource-id="com.volkswagen.weconnect:id/value" text="80%"
                bounds="[900,700][1030,760]"/>
            <node resource-id="com.volkswagen.weconnect:id/value" text="30 %"
                bounds="[900,300][1030,360]"/>
            </hierarchy>"""
        )
        self.assertEqual(
            [value for _node, value in VolkswagenReader.setting_values(root)],
            [30, 80],
        )

    def test_resource_node_center_accepts_compose_resource_suffix(self):
        root = ET.fromstring(
            '<hierarchy><node resource-id="settingsTile" '
            'bounds="[50,100][450,500]"/></hierarchy>'
        )
        self.assertEqual(
            VolkswagenReader.resource_node_center(root, "settingsTile"),
            (250, 300),
        )

    def test_overview_element_scrolls_until_lazy_item_is_visible(self):
        initial = ET.fromstring(
            '<hierarchy><node bounds="[0,0][1080,2340]"/></hierarchy>'
        )
        visible = ET.fromstring(
            '<hierarchy><node content-desc="Einstellungen. Details öffnen" '
            'bounds="[50,1000][1030,1300]"/></hierarchy>'
        )
        reader = object.__new__(VolkswagenReader)
        reader.shell = Mock()
        reader.dump_ui = Mock(return_value=visible)
        with patch("time.sleep"):
            root, center = reader.find_overview_element(
                initial, ("Einstellungen.",), "settingsTile"
            )
        self.assertIs(root, visible)
        self.assertEqual(center, (540, 1150))

    def test_charging_settings_parse_named_switches(self):
        root = ET.fromstring(
            """<hierarchy>
            <node resource-id="com.volkswagen.weconnect:id/value" text="80%"
                bounds="[900,300][1030,360]"/>
            <node text="Battery Care" bounds="[50,500][600,560]"/>
            <node checkable="true" clickable="true" checked="true"
                bounds="[880,470][1030,590]"/>
            <node text="Reduced AC current" bounds="[50,700][600,760]"/>
            <node checkable="true" clickable="true" checked="false"
                bounds="[880,670][1030,790]"/>
            <node text="Automatically release AC connector" bounds="[50,900][700,960]"/>
            <node checkable="true" clickable="true" checked="true"
                bounds="[880,870][1030,990]"/>
            </hierarchy>"""
        )
        reader = object.__new__(VolkswagenReader)
        self.assertEqual(
            reader.read_charging_settings(root),
            ChargingSettingsData(
                targetSoc=80,
                batteryCare=True,
                reducedAc=False,
                autoReleaseAcConnector=True,
            ),
        )

    def test_gte_charging_settings_accept_missing_battery_care(self):
        root = ET.fromstring(
            """<hierarchy>
            <node resource-id="com.volkswagen.weconnect:id/value" text="80%"
                bounds="[900,300][1030,360]"/>
            <node text="Reduced AC charging current" bounds="[50,500][700,560]"/>
            <node checkable="true" clickable="true" checked="false"
                bounds="[880,470][1030,590]"/>
            <node text="Automatically release AC connector" bounds="[50,700][760,760]"/>
            <node checkable="true" clickable="true" checked="true"
                bounds="[880,670][1030,790]"/>
            </hierarchy>"""
        )
        reader = object.__new__(VolkswagenReader)
        self.assertEqual(
            reader.read_charging_settings(root),
            ChargingSettingsData(
                targetSoc=80,
                batteryCare=None,
                reducedAc=False,
                autoReleaseAcConnector=True,
            ),
        )

    def test_target_soc_action_patches_charge_cache(self):
        state = object.__new__(AppState)
        state.reader = Mock()
        state.reader.set_target_soc.return_value = ChargingSettingsData(
            targetSoc=90
        )
        state.charge = SimpleNamespace(
            lock=threading.Lock(),
            value=VehicleData(status="C", targetSoc=100),
            patch_value=Mock(),
        )

        result = state._action("charging/target-soc", {"value": ["90"]})

        self.assertEqual(result.targetSoc, 90)
        patched = state.charge.patch_value.call_args.args[0]
        self.assertEqual(patched.status, "C")
        self.assertEqual(patched.targetSoc, 90)

    def test_action_pending_stays_set_until_all_actions_finish(self):
        state = object.__new__(AppState)
        state.reader = SimpleNamespace(action_pending=threading.Event())
        state.action_pending_lock = threading.Lock()
        state.action_pending_count = 0

        state._begin_action()
        state._begin_action()
        state._end_action()

        self.assertTrue(state.reader.action_pending.is_set())
        self.assertEqual(state.action_pending_count, 1)
        state._end_action()
        self.assertFalse(state.reader.action_pending.is_set())
        self.assertEqual(state.action_pending_count, 0)

    def test_german_charging_location_settings(self):
        root = ET.fromstring(
            """<hierarchy>
            <node resource-id="com.volkswagen.weconnect:id/value" text="30%"
                bounds="[900,300][1030,360]"/>
            <node resource-id="com.volkswagen.weconnect:id/value" text="80%"
                bounds="[900,500][1030,560]"/>
            <node text="Reduzierter AC-Ladestrom" bounds="[50,700][650,760]"/>
            <node checkable="true" clickable="true" checked="true"
                bounds="[880,670][1030,790]"/>
            <node text="Automatisch entriegeln" bounds="[50,900][650,960]"/>
            <node checkable="true" clickable="true" checked="false"
                bounds="[880,870][1030,990]"/>
            </hierarchy>"""
        )
        reader = object.__new__(VolkswagenReader)
        self.assertEqual(
            reader.read_charging_location_settings("Zuhause", root),
            ChargingLocationSettingsData(
                name="Zuhause", directSoc=30, targetSoc=80,
                reducedAc=True, autoUnlock=False,
            ),
        )

    def test_charging_location_list_uses_semantic_name_resources(self):
        root = ET.fromstring(
            """<hierarchy>
            <node resource-id="com.volkswagen.weconnect:id/name" text="Zuhause"
                bounds="[50,300][500,360]"/>
            <node resource-id="com.volkswagen.weconnect:id/name" text="Arbeit"
                bounds="[50,500][500,560]"/>
            </hierarchy>"""
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                overview = ET.fromstring(
                    '<hierarchy><node text="Abfahrtszeiten." '
                    'bounds="[10,20][110,120]"/></hierarchy>'
                )
                with (
                    patch.object(reader, "screen_session", nullcontext),
                    patch.object(reader, "launch"),
                    patch.object(reader, "open_overview", return_value=overview),
                    patch.object(reader, "dump_ui", return_value=root),
                    patch.object(reader, "shell"),
                ):
                    self.assertEqual(
                        reader.list_charging_locations(),
                        ChargingLocationsData(locations=["Zuhause", "Arbeit"]),
                    )

    def test_strings_and_range_tile(self):
        root = ET.fromstring(
            """<hierarchy><node text="" content-desc="Übersicht Reichweite. Batteriereichweite: 126 Kilometer. Details öffnen" bounds="[55,1044][507,1496]"/>
            <node text="Entriegelt" content-desc="" bounds="[0,0][1,1]"/></hierarchy>"""
        )
        self.assertEqual(
            VolkswagenReader.strings(root),
            [
                "Übersicht Reichweite. Batteriereichweite: 126 Kilometer. Details öffnen",
                "Entriegelt",
            ],
        )
        self.assertEqual(VolkswagenReader.range_tile_center(root), (281, 1270))

    def test_english_range_tile(self):
        root = ET.fromstring(
            """<hierarchy><node content-desc="Overview range. Battery range: 126 kilometres. Open details" bounds="[55,1044][507,1496]"/></hierarchy>"""
        )
        self.assertEqual(VolkswagenReader.range_tile_center(root), (281, 1270))

    def test_english_range_tile_without_colon(self):
        root = ET.fromstring(
            """<hierarchy><node content-desc="Battery range 126 km. Open details" bounds="[55,1044][507,1496]"/></hierarchy>"""
        )
        self.assertEqual(VolkswagenReader.range_tile_center(root), (281, 1270))

    def test_selector_matches_label_without_trailing_punctuation(self):
        root = ET.fromstring(
            '<hierarchy><node content-desc="Climate control" '
            'bounds="[55,1044][507,1496]"/></hierarchy>'
        )
        self.assertEqual(
            VolkswagenReader.described_node_center(root, "Climate control."),
            (281, 1270),
        )

    def test_lock_state_parsing_is_case_insensitive(self):
        self.assertFalse(VolkswagenReader.parse_locked("Fahrzeug. Wird entriegelt."))
        self.assertTrue(VolkswagenReader.parse_locked("Fahrzeug. Wird verriegelt."))
        self.assertFalse(VolkswagenReader.parse_locked("ENTRIEGELT"))
        self.assertFalse(VolkswagenReader.parse_locked("Vehicle. Unlocking."))
        self.assertTrue(VolkswagenReader.parse_locked("Vehicle. Locked."))
        self.assertIsNone(VolkswagenReader.parse_locked("Fahrzeugstatus unbekannt"))

    def test_english_overview_values(self):
        self.assertEqual(VolkswagenReader.parse_sync_age("Synced 2 hours 5 minutes"), 125)
        self.assertEqual(VolkswagenReader.parse_sync_age("Just synced"), 0)
        self.assertEqual(
            VolkswagenReader.parse_sync_age("Synchronised just now"), 0
        )
        self.assertFalse(VolkswagenReader.parse_climater("Air conditioning. Off."))
        self.assertFalse(VolkswagenReader.parse_climater("Climate control. Off."))
        self.assertTrue(VolkswagenReader.parse_climater("Air conditioning. On."))

    def test_english_charging_station_status_is_not_active_charging(self):
        self.assertFalse(
            VolkswagenReader.text_reports_active_charging(
                "Battery 91 % • Charging station shows current status"
            )
        )
        self.assertTrue(
            VolkswagenReader.text_reports_active_charging(
                "Battery 91 % • Charging"
            )
        )

    def test_explicitly_disconnected_cable_text_is_recognized(self):
        self.assertTrue(
            VolkswagenReader.text_reports_disconnected(
                "Battery 81 % - Connect charging cable"
            )
        )
        self.assertTrue(
            VolkswagenReader.text_reports_disconnected(
                "Batterie 81 % - Ladekabel anschließen"
            )
        )

    def test_current_german_sync_text(self):
        self.assertEqual(
            VolkswagenReader.parse_sync_age(
                "Ihr Fahrzeug: ID.7 Tourer Pro. Gerade synchronisiert. "
                "Synchronisiert gerade"
            ),
            0,
        )

    def test_real_english_state_of_charge(self):
        self.assertEqual(
            VolkswagenReader.parse_soc(
                "Charging status. Battery charge level: 45 per cent. "
                "Currently charging"
            ),
            45,
        )

    def test_compact_charging_state_of_charge(self):
        self.assertEqual(
            VolkswagenReader.parse_soc("89 km\n70% · Charging\nImmediate charging"),
            70,
        )

    def test_zero_state_of_charge_in_both_languages(self):
        for text in ("Battery 0%", "Batterie 0 %"):
            with self.subTest(text=text):
                self.assertEqual(VolkswagenReader.parse_soc(text), 0)

    def test_empty_phev_charge_detail_is_ready_by_resource_anchor(self):
        for label in ("Battery", "Batterie"):
            with self.subTest(label=label):
                root = ET.fromstring(
                    f"""<hierarchy>
                    <node resource-id="com.volkswagen.weconnect:id/rangeArcBatterySoc"
                          text="--"/>
                    <node text="{label}"/>
                    <node text="Start"/>
                    </hierarchy>"""
                )
                self.assertTrue(VolkswagenReader.is_charge_detail_page(root))

    def test_charging_details(self):
        result = VehicleData()
        VolkswagenReader.parse_charging_details(
            "Ladedetails. 3 Stunden und. 55 Minuten Ladezeit verbleibend. "
            "Ladegeschwindigkeit: 53 Kilometer pro Stunde. "
            "Ladeleistung: 10 Kilowatt. Zielladestand: 80 Prozent\n"
            "Ladeverfahren. Sofortladen. Ladeverfahren ändern",
            result,
        )
        self.assertEqual(result.remainingChargeMinutes, 235)
        self.assertEqual(result.chargeRateKmH, 53)
        self.assertEqual(result.chargePowerKw, 10)
        self.assertEqual(result.targetSoc, 80)
        self.assertEqual(result.chargingMode, "Sofortladen")

    def test_charging_details_preserves_decimal_power_with_german_separator(self):
        result = VehicleData()
        VolkswagenReader.parse_charging_details(
            "Ladedetails. 2 Stunden und. 10 Minuten Ladezeit verbleibend. "
            "Ladegeschwindigkeit: 11 Kilometer pro Stunde. "
            "Ladeleistung: 2,30 kW. Zielladestand: 80 Prozent",
            result,
        )
        self.assertEqual(result.remainingChargeMinutes, 130)
        self.assertEqual(result.chargeRateKmH, 11)
        self.assertEqual(result.chargePowerKw, 2.3)
        self.assertEqual(result.targetSoc, 80)

    def test_charging_details_accepts_zero_hour_word(self):
        result = VehicleData()
        VolkswagenReader.parse_charging_details(
            "Ladedetails. Null Stunden und. 50 Minuten Ladezeit verbleibend. "
            "Ladegeschwindigkeit: 71 Kilometer pro Stunde. "
            "Ladeleistung: 10 Kilowatt. Zielladestand: 100 Prozent\n"
            "Ladeverfahren. Sofortladen. Ladeverfahren ändern",
            result,
        )
        self.assertEqual(result.remainingChargeMinutes, 50)
        self.assertEqual(result.chargeRateKmH, 71)
        self.assertEqual(result.chargePowerKw, 10)
        self.assertEqual(result.targetSoc, 100)
        self.assertEqual(result.chargingMode, "Sofortladen")

    def test_target_soc_without_active_charging(self):
        result = VehicleData()
        VolkswagenReader.parse_charging_details(
            "Ladedetails. Zielladestand: 80 Prozent", result
        )
        self.assertEqual(result.targetSoc, 80)
        self.assertIsNone(result.remainingChargeMinutes)

    def test_compact_gte_charging_details(self):
        result = VehicleData()
        VolkswagenReader.parse_charging_details(
            "89 km\n70% · Charging\n1:10 h\n12 km per h\n2 kW\n"
            "Upper charge limit\n100%\nImmediate charging\nStop",
            result,
        )
        self.assertEqual(result.remainingChargeMinutes, 70)
        self.assertEqual(result.chargeRateKmH, 12)
        self.assertEqual(result.chargePowerKw, 2)
        self.assertEqual(result.targetSoc, 100)
        self.assertEqual(result.chargingMode, "Immediate charging")

    def test_compact_gte_charge_read_sets_charging_status(self):
        overview = ET.fromstring(
            """<hierarchy>
            <node content-desc="Battery range: 89 km" bounds="[100,500][900,760]"/>
            <node content-desc="Fuel range: 560 km"/>
            <node content-desc="Vehicle. Locked."/>
            </hierarchy>"""
        )
        detail = ET.fromstring(
            """<hierarchy>
            <node text="89 km"/>
            <node text="70% · Charging"/>
            <node text="1:10 h"/>
            <node text="12 km per h"/>
            <node text="2 kW"/>
            <node text="Upper charge limit"/>
            <node text="100%"/>
            <node text="Immediate charging"/>
            <node text="Stop"/>
            </hierarchy>"""
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "launch"),
                    patch.object(reader, "open_overview", return_value=overview),
                    patch.object(reader, "wait_for_charge_detail", return_value=detail),
                    patch.object(reader, "shell"),
                    patch("time.sleep"),
                ):
                    result = reader._read()
        self.assertEqual(result.status, "C")
        self.assertEqual(result.soc, 70)
        self.assertEqual(result.remainingChargeMinutes, 70)
        self.assertEqual(result.chargeRateKmH, 12)
        self.assertEqual(result.chargePowerKw, 2)
        self.assertEqual(result.targetSoc, 100)
        self.assertEqual(result.chargingMode, "Immediate charging")

    def test_empty_phev_detail_uses_zero_soc_from_overview(self):
        cases = (
            (
                "Battery range: 0 km. Battery 0%. Open details",
                "Battery",
            ),
            (
                "Batteriereichweite: 0 Kilometer. Batterie 0 %. Details öffnen",
                "Batterie",
            ),
        )
        for description, label in cases:
            with self.subTest(description=description):
                overview = ET.fromstring(
                    f'<hierarchy><node content-desc="{description}" '
                    'bounds="[100,500][900,760]"/></hierarchy>'
                )
                detail = ET.fromstring(
                    f"""<hierarchy>
                    <node resource-id="com.volkswagen.weconnect:id/rangeArcBatterySoc"
                          text="--"/>
                    <node text="{label}"/>
                    <node text="Start"/>
                    </hierarchy>"""
                )
                with TemporaryDirectory() as directory:
                    with patch.dict(
                        "os.environ",
                        {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                        clear=False,
                    ):
                        reader = VolkswagenReader()
                        with (
                            patch.object(reader, "launch"),
                            patch.object(
                                reader, "open_overview", return_value=overview
                            ),
                            patch.object(
                                reader,
                                "wait_for_charge_detail",
                                return_value=detail,
                            ),
                            patch.object(reader, "shell"),
                            patch("time.sleep"),
                        ):
                            result = reader._read()
                self.assertEqual(result.soc, 0)
                self.assertEqual(result.range, 0)
                self.assertEqual(result.status, "A")

    def test_numeric_detail_soc_remains_authoritative(self):
        overview = ET.fromstring(
            '<hierarchy><node content-desc="Battery range: 0 km. Battery 0%. '
            'Open details" bounds="[100,500][900,760]"/></hierarchy>'
        )
        detail = ET.fromstring(
            '<hierarchy><node text="Battery 1%"/></hierarchy>'
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "launch"),
                    patch.object(reader, "open_overview", return_value=overview),
                    patch.object(
                        reader, "wait_for_charge_detail", return_value=detail
                    ),
                    patch.object(reader, "shell"),
                    patch("time.sleep"),
                ):
                    result = reader._read()
        self.assertEqual(result.soc, 1)

    def test_charge_health_warning_is_dismissed_before_detail_parse(self):
        notice = ET.fromstring(
            """<hierarchy>
            <node text="Warning"/>
            <node text="To protect vehicle health, commands may be executed with a delay"/>
            <node text="OK" bounds="[420,1780][660,1880]"/>
            </hierarchy>"""
        )
        ready = ET.fromstring(
            """<hierarchy>
            <node text="Charging status. Battery charge level: 80 per cent."/>
            <node text="Start charging" bounds="[100,1700][800,1850]"/>
            </hierarchy>"""
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "shell") as shell,
                    patch.object(
                        reader,
                        "dump_ui_with_overlay_recovery",
                        side_effect=(notice, ready),
                    ),
                    patch("time.sleep"),
                ):
                    self.assertIs(reader.wait_for_charge_detail("vw-detail.xml"), ready)
                shell.assert_called_once_with("input", "tap", "540", "1830")

    def test_start_charging_fails_clearly_when_target_soc_already_reached(self):
        overview = ET.fromstring(
            """<hierarchy>
            <node content-desc="Battery range: 120 km" bounds="[100,500][900,760]"/>
            </hierarchy>"""
        )
        detail = ET.fromstring(
            """<hierarchy>
            <node text="Charging status. Battery charge level: 80 per cent."/>
            <node text="Target charge level: 80 per cent"/>
            <node text="Start charging" bounds="[100,1700][800,1850]"/>
            </hierarchy>"""
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "screen_session", return_value=nullcontext()),
                    patch.object(reader, "launch"),
                    patch.object(reader, "open_overview", return_value=overview),
                    patch.object(reader, "wait_for_charge_detail", return_value=detail),
                    patch.object(
                        reader,
                        "with_retries",
                        return_value=VehicleData(status="B", soc=80, targetSoc=80),
                    ),
                    patch.object(reader, "shell"),
                    patch("time.sleep"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "at or above"):
                        reader.set_charging(True)

    def test_start_charging_rechecks_state_immediately_before_tap(self):
        overview = ET.fromstring(
            """<hierarchy>
            <node content-desc="Battery range: 120 km" bounds="[100,500][900,760]"/>
            </hierarchy>"""
        )
        stopped = ET.fromstring(
            """<hierarchy>
            <node text="Charging status. Battery charge level: 40 per cent."/>
            <node text="Start charging" bounds="[100,1700][800,1850]"/>
            </hierarchy>"""
        )
        started = ET.fromstring(
            """<hierarchy>
            <node text="Charging. Battery charge level: 40 per cent."/>
            <node text="Stop charging" bounds="[100,1700][800,1850]"/>
            </hierarchy>"""
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "screen_session", return_value=nullcontext()),
                    patch.object(reader, "launch"),
                    patch.object(reader, "open_overview", return_value=overview),
                    patch.object(
                        reader,
                        "wait_for_charge_detail",
                        side_effect=(stopped, started),
                    ) as wait_for_detail,
                    patch.object(
                        reader,
                        "with_retries",
                        return_value=VehicleData(status="C", soc=40, targetSoc=80),
                    ),
                    patch.object(reader, "shell") as shell,
                    patch("time.sleep"),
                ):
                    result = reader.set_charging(True)

        self.assertEqual(result.status, "C")
        self.assertEqual(wait_for_detail.call_count, 2)
        shell.assert_called_once_with("input", "tap", "500", "630")

    def test_english_charging_details(self):
        result = VehicleData()
        VolkswagenReader.parse_charging_details(
            "Charging details. 27 hours and. 20 minutes of charging time left. "
            "Charging speed: 6 kilometres per hour. "
            "Charging capacity: 1 kilowatt. Target charge level: 80 per cent. "
            "Charging method. Immediate charging. Change charging method",
            result,
        )
        self.assertEqual(result.remainingChargeMinutes, 1640)
        self.assertEqual(result.chargeRateKmH, 6)
        self.assertEqual(result.chargePowerKw, 1)
        self.assertEqual(result.targetSoc, 80)
        self.assertEqual(result.chargingMode, "Immediate charging")

    def test_english_charging_details_preserves_decimal_power(self):
        result = VehicleData()
        VolkswagenReader.parse_charging_details(
            "Charging details. 2 hours and. 10 minutes of charging time left. "
            "Charging speed: 11 kilometres per hour. "
            "Charging power: 2.30 kW. Target charge level: 80 per cent. "
            "Charging method. Immediate charging. Change charging method",
            result,
        )
        self.assertEqual(result.remainingChargeMinutes, 130)
        self.assertEqual(result.chargeRateKmH, 11)
        self.assertEqual(result.chargePowerKw, 2.3)
        self.assertEqual(result.targetSoc, 80)
        self.assertEqual(result.chargingMode, "Immediate charging")

    def test_target_temperature_uses_center_value(self):
        root = ET.fromstring(
            """<hierarchy>
            <node text="20" bounds="[0,1028][138,1189]"/>
            <node text="20.5" bounds="[340,1001][692,1216]"/>
            <node text="21" bounds="[906,1028][1062,1189]"/>
            </hierarchy>"""
        )
        self.assertEqual(VolkswagenReader.parse_target_temperature(root), 20.5)

    def test_target_temperature_taps_visible_value_on_pixel_geometry(self):
        initial = ET.fromstring(
            """<hierarchy>
            <node text="20" bounds="[0,1111][135,1264]"/>
            <node text="20.5" bounds="[349,1085][685,1290]"/>
            <node text="21" bounds="[911,1111][1059,1264]"/>
            </hierarchy>"""
        )
        updated = ET.fromstring(
            """<hierarchy>
            <node text="20.5" bounds="[0,1111][187,1264]"/>
            <node text="21" bounds="[418,1085][616,1290]"/>
            <node text="21.5" bounds="[859,1111][1080,1264]"/>
            </hierarchy>"""
        )
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "screen_session", nullcontext),
                    patch.object(reader, "launch"),
                    patch.object(reader, "open_climate", return_value=initial),
                    patch.object(reader, "dump_ui", side_effect=(updated, updated)),
                    patch.object(reader, "shell") as shell,
                    patch("time.sleep"),
                ):
                    self.assertEqual(reader.set_target_temperature(21), 21)
                shell.assert_called_once_with("input", "tap", "985", "1187")

    def test_target_temperature_waits_for_delayed_ui_update(self):
        initial = ET.fromstring(
            '<hierarchy><node text="20.5" bounds="[300,1000][700,1200]"/>'
            '<node text="21" bounds="[900,1020][1080,1180]"/></hierarchy>'
        )
        updated = ET.fromstring(
            '<hierarchy><node text="20.5" bounds="[0,1020][180,1180]"/>'
            '<node text="21" bounds="[300,1000][700,1200]"/></hierarchy>'
        )
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
                "UI_UPDATE_TIMEOUT_SECONDS": "1",
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "screen_session", nullcontext),
                    patch.object(reader, "launch"),
                    patch.object(reader, "open_climate", return_value=initial),
                    patch.object(
                        reader, "dump_ui", side_effect=(initial, updated)
                    ),
                    patch.object(reader, "shell"),
                    patch("time.sleep") as sleep,
                ):
                    self.assertEqual(reader.set_target_temperature(21), 21)
                sleep.assert_called_once_with(0.5)

    def test_target_temperature_decreases_multiple_steps(self):
        initial = ET.fromstring(
            '<hierarchy><node text="21" bounds="[0,1020][180,1180]"/>'
            '<node text="21.5" bounds="[300,1000][700,1200]"/></hierarchy>'
        )
        middle = ET.fromstring(
            '<hierarchy><node text="20.5" bounds="[0,1020][180,1180]"/>'
            '<node text="21" bounds="[300,1000][700,1200]"/></hierarchy>'
        )
        final = ET.fromstring(
            '<hierarchy><node text="20.5" bounds="[300,1000][700,1200]"/>'
            '<node text="21" bounds="[900,1020][1080,1180]"/></hierarchy>'
        )
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "screen_session", nullcontext),
                    patch.object(reader, "launch"),
                    patch.object(reader, "open_climate", return_value=initial),
                    patch.object(reader, "dump_ui", side_effect=(middle, final)),
                    patch.object(reader, "shell") as shell,
                    patch("time.sleep"),
                ):
                    self.assertEqual(reader.set_target_temperature(20.5), 20.5)
                taps = [entry.args for entry in shell.call_args_list]
                self.assertEqual(
                    taps,
                    [
                        ("input", "tap", "90", "1100"),
                        ("input", "tap", "90", "1100"),
                    ],
                )

    def test_target_temperature_accepts_decimal_comma(self):
        root = ET.fromstring(
            '<hierarchy><node text="20,5" bounds="[300,1000][700,1200]"/>'
            '<node text="21" bounds="[900,1020][1080,1180]"/></hierarchy>'
        )
        self.assertEqual(VolkswagenReader.parse_target_temperature(root), 20.5)
        self.assertEqual(
            VolkswagenReader.temperature_value_center(root, 20.5),
            (500, 1100),
        )

    def test_target_temperature_accepts_lo_hi_boundaries(self):
        root = ET.fromstring(
            '<hierarchy><node text="29.5" bounds="[0,1020][180,1180]"/>'
            '<node text="HI" bounds="[300,1000][700,1200]"/>'
            '<node content-desc="LO" bounds="[900,1020][1080,1180]"/></hierarchy>'
        )
        self.assertEqual(VolkswagenReader.parse_target_temperature(root), 30.0)
        self.assertEqual(
            VolkswagenReader.temperature_value_center(root, 30.0),
            (500, 1100),
        )
        self.assertEqual(
            VolkswagenReader.temperature_value_center(root, 15.5),
            (990, 1100),
        )

    def test_target_temperature_can_step_to_hi(self):
        initial = ET.fromstring(
            '<hierarchy><node text="29.5" bounds="[300,1000][700,1200]"/>'
            '<node text="HI" bounds="[900,1020][1080,1180]"/></hierarchy>'
        )
        updated = ET.fromstring(
            '<hierarchy><node text="29.5" bounds="[0,1020][180,1180]"/>'
            '<node text="HI" bounds="[300,1000][700,1200]"/></hierarchy>'
        )
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "screen_session", nullcontext),
                    patch.object(reader, "launch"),
                    patch.object(reader, "open_climate", return_value=initial),
                    patch.object(reader, "dump_ui", side_effect=(updated, updated)),
                    patch.object(reader, "shell") as shell,
                    patch("time.sleep"),
                ):
                    self.assertEqual(reader.set_target_temperature(30), 30)
                shell.assert_called_once_with("input", "tap", "990", "1100")

    def test_target_temperature_can_step_to_lo(self):
        initial = ET.fromstring(
            '<hierarchy><node content-desc="LO" bounds="[0,1020][180,1180]"/>'
            '<node text="16" bounds="[300,1000][700,1200]"/></hierarchy>'
        )
        updated = ET.fromstring(
            '<hierarchy><node content-desc="LO" bounds="[300,1000][700,1200]"/>'
            '<node text="16" bounds="[900,1020][1080,1180]"/></hierarchy>'
        )
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "screen_session", nullcontext),
                    patch.object(reader, "launch"),
                    patch.object(reader, "open_climate", return_value=initial),
                    patch.object(reader, "dump_ui", side_effect=(updated, updated)),
                    patch.object(reader, "shell") as shell,
                    patch("time.sleep"),
                ):
                    self.assertEqual(reader.set_target_temperature(15.5), 15.5)
                shell.assert_called_once_with("input", "tap", "90", "1100")

    def test_open_overview_waits_for_range_tile(self):
        loading = ET.fromstring('<hierarchy><node text="Loading"/></hierarchy>')
        ready = ET.fromstring(
            '<hierarchy><node content-desc="Battery range: 100 kilometres" '
            'bounds="[10,20][110,120]"/></hierarchy>'
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(
                        reader,
                        "dump_ui_with_compose_fallback",
                        side_effect=(loading, ready),
                    ),
                    patch.object(reader, "app_in_foreground", return_value=True),
                    patch.object(reader, "shell") as shell,
                    patch("time.sleep") as sleep,
                ):
                    self.assertIs(reader.open_overview(), ready)
                shell.assert_not_called()
                sleep.assert_called_once_with(0.5)

    def test_open_overview_recovers_when_system_overlay_takes_focus(self):
        launcher = ET.fromstring('<hierarchy><node text="Launcher"/></hierarchy>')
        ready = ET.fromstring(
            '<hierarchy><node content-desc="Battery range 100 km" '
            'bounds="[10,20][110,120]"/></hierarchy>'
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(
                        reader,
                        "dump_ui_with_compose_fallback",
                        side_effect=(launcher, ready),
                    ),
                    patch.object(
                        reader,
                        "app_in_foreground",
                        side_effect=(False, False, True),
                    ),
                    patch.object(reader, "close_system_overlays") as close,
                    patch.object(reader, "launch") as launch,
                    patch("time.sleep"),
                ):
                    self.assertIs(reader.open_overview(), ready)
                close.assert_called_once()
                launch.assert_called_once()

    def test_open_overview_waits_for_required_overview_tile(self):
        banner = ET.fromstring(
            '<hierarchy>'
            '<node bounds="[0,0][1080,2400]"/>'
            '<node content-desc="Battery range: 100 kilometres" '
            'bounds="[10,20][110,120]"/>'
            '<node text="Discover Volkswagen" bounds="[40,1700][1040,1900]"/>'
            '</hierarchy>'
        )
        ready = ET.fromstring(
            '<hierarchy>'
            '<node content-desc="Battery range: 100 kilometres" '
            'bounds="[10,20][110,120]"/>'
            '<node content-desc="Vehicle health report. Open" '
            'bounds="[20,500][300,700]"/>'
            '</hierarchy>'
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(
                        reader,
                        "dump_ui_with_compose_fallback",
                        side_effect=(banner, ready),
                    ),
                    patch.object(reader, "app_in_foreground", return_value=True),
                    patch.object(reader, "shell") as shell,
                    patch("time.sleep") as sleep,
                ):
                    self.assertIs(
                        reader.open_overview(("Vehicle health report.",)),
                        ready,
                    )
                shell.assert_called_once_with(
                    "input", "swipe", "540", "1872", "540", "1152", "300"
                )
                sleep.assert_called_once_with(1)

    def test_pin_input_and_viewport_are_semantic(self):
        root = ET.fromstring(
            '<hierarchy><node bounds="[0,0][1080,2424]"/>'
            '<node class="android.widget.EditText" '
            'bounds="[55,1059][1025,1363]"/></hierarchy>'
        )
        self.assertEqual(VolkswagenReader.viewport_size(root), (1080, 2424))
        self.assertEqual(
            VolkswagenReader.editable_node_center(root), (540, 1211)
        )

    def test_lock_swipe_uses_physical_display_size(self):
        states = (
            (False, True, ("540", "2040", "540", "1512")),
            (True, False, ("540", "1512", "540", "2040")),
        )
        for current, desired, coordinates in states:
            with self.subTest(desired=desired), TemporaryDirectory() as directory:
                environment = {
                    "ADB_SERIAL": "usb-serial",
                    "DIAGNOSTICS_DIR": directory,
                    "VW_SPIN": "1234",
                }
                overview = ET.fromstring(
                    '<hierarchy><node content-desc="Fahrzeug. '
                    + ("Verriegelt." if current else "Entriegelt.")
                    + '" bounds="[100,200][300,400]"/></hierarchy>'
                )
                pin = ET.fromstring(
                    '<hierarchy><node text="S-PIN"/>'
                    '<node class="android.widget.EditText" '
                    'bounds="[100,500][500,700]"/></hierarchy>'
                )
                with patch.dict("os.environ", environment, clear=False):
                    reader = VolkswagenReader()
                    with (
                        patch.object(reader, "screen_session", return_value=nullcontext()),
                        patch.object(reader, "launch"),
                        patch.object(reader, "open_overview", return_value=overview),
                        patch.object(reader, "wait_for_lock_control", return_value=overview),
                        patch.object(reader, "display_size", return_value=(1080, 2400)),
                        patch.object(reader, "wait_for_pin_dialog", return_value=pin),
                        patch.object(reader, "with_retries", return_value=VehicleData()) as retries,
                        patch.object(reader, "shell") as shell,
                        patch("time.sleep"),
                    ):
                        reader.set_locked(desired)
                self.assertIn(
                    ("input", "swipe", *coordinates, "900"),
                    [value.args for value in shell.call_args_list],
                )
                self.assertIn(
                    ("input", "text", "1234"),
                    [value.args for value in shell.call_args_list],
                )
                retries.assert_called_once()

    def test_pin_dialog_waits_for_editable_field(self):
        loading = ET.fromstring('<hierarchy><node text="S-PIN"/></hierarchy>')
        ready = ET.fromstring(
            '<hierarchy><node text="S-PIN"/>'
            '<node class="android.widget.EditText" '
            'bounds="[100,500][500,700]"/></hierarchy>'
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb-serial", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "dump_ui", side_effect=(loading, ready)),
                    patch("time.sleep") as sleep,
                ):
                    self.assertIs(reader.wait_for_pin_dialog(), ready)
                sleep.assert_called_once_with(0.5)

    def test_unlock_failure_saves_diagnostics(self):
        overview = ET.fromstring(
            '<hierarchy><node content-desc="Fahrzeug. Verriegelt." '
            'bounds="[100,200][300,400]"/></hierarchy>'
        )
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
                "VW_SPIN": "1234",
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                error = RuntimeError("Volkswagen S-PIN dialog not found")
                with (
                    patch.object(reader, "screen_session", return_value=nullcontext()),
                    patch.object(reader, "launch"),
                    patch.object(reader, "open_overview", return_value=overview),
                    patch.object(reader, "wait_for_lock_control", return_value=overview),
                    patch.object(reader, "display_size", return_value=(1080, 2400)),
                    patch.object(reader, "wait_for_pin_dialog", side_effect=error),
                    patch.object(reader, "save_diagnostics") as diagnostics,
                    patch.object(reader, "shell"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "S-PIN dialog"):
                        reader.set_locked(False)
                diagnostics.assert_called_once_with("UNLOCK", error)

    def test_climate_switch_is_selected_by_nearby_label(self):
        root = ET.fromstring(
            """<hierarchy>
            <node text="Unrelated" bounds="[55,300][400,360]"/>
            <node checkable="true" clickable="true" checked="false"
                bounds="[882,280][1025,412]"/>
            <node text="Automatische Scheibenheizung"
                bounds="[55,679][741,740]"/>
            <node checkable="true" clickable="true" checked="true"
                bounds="[882,643][1025,775]"/>
            </hierarchy>"""
        )
        node = VolkswagenReader.checked_node_near_labels(
            root,
            ("Automatische Scheibenheizung", "Automatic window heating"),
        )
        self.assertEqual(node.attrib["checked"], "true")

    def test_open_climate_waits_for_english_temperature_page(self):
        overview = ET.fromstring(
            '<hierarchy><node content-desc="Climate control. Off." '
            'bounds="[10,20][110,120]"/></hierarchy>'
        )
        loading = ET.fromstring('<hierarchy><node text="Loading"/></hierarchy>')
        ready = ET.fromstring(
            '<hierarchy><node text="20.5" '
            'bounds="[300,1000][700,1200]"/></hierarchy>'
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "open_overview", return_value=overview),
                    patch.object(reader, "dump_ui", side_effect=(loading, ready)),
                    patch.object(reader, "shell") as shell,
                    patch("time.sleep") as sleep,
                ):
                    self.assertIs(reader.open_climate(), ready)
                shell.assert_called_once_with("input", "tap", "60", "70")
                sleep.assert_called_once_with(0.5)

    def test_english_automatic_window_heating_selector(self):
        climate = ET.fromstring(
            '<hierarchy><node text="Settings" '
            'bounds="[10,20][110,120]"/></hierarchy>'
        )
        settings = ET.fromstring(
            """<hierarchy>
            <node text="Unrelated option" bounds="[55,300][400,360]"/>
            <node checkable="true" clickable="true" checked="true"
                bounds="[882,280][1025,412]"/>
            <node text="Automatic window heating"
                bounds="[55,679][741,740]"/>
            <node checkable="true" clickable="true" checked="false"
                bounds="[882,643][1025,775]"/>
            </hierarchy>"""
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "screen_session", nullcontext),
                    patch.object(reader, "launch"),
                    patch.object(reader, "open_climate", return_value=climate),
                    patch.object(reader, "dump_ui", return_value=settings),
                    patch.object(reader, "shell") as shell,
                    patch("time.sleep"),
                ):
                    self.assertFalse(
                        reader.set_climate_option(
                            "automatic-window-heating", False
                        )
                    )
                shell.assert_called_once_with("input", "tap", "60", "70")

    def test_english_front_right_selector(self):
        root = ET.fromstring(
            """<hierarchy>
            <node text="Front left" bounds="[55,1015][296,1076]"/>
            <node checkable="true" clickable="true" checked="true"
                bounds="[882,979][1025,1111]"/>
            <node text="Front right" bounds="[55,1194][332,1255]"/>
            <node checkable="true" clickable="true" checked="false"
                bounds="[882,1158][1025,1290]"/>
            </hierarchy>"""
        )
        node = VolkswagenReader.checked_node_near_labels(
            root, ("Vorne rechts", "Front right")
        )
        self.assertEqual(node.attrib["checked"], "false")

    def test_climate_option_waits_for_complete_page(self):
        loading = ET.fromstring(
            '<hierarchy><node text="Front left" '
            'bounds="[55,1015][296,1076]"/></hierarchy>'
        )
        ready = ET.fromstring(
            '<hierarchy><node text="Front right" '
            'bounds="[55,1194][332,1255]"/>'
            '<node checkable="true" clickable="true" checked="false" '
            'bounds="[882,1158][1025,1290]"/></hierarchy>'
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "dump_ui", side_effect=(loading, ready)),
                    patch("time.sleep") as sleep,
                ):
                    _root, node = reader.wait_for_checked_option(
                        "zones.xml", ("Vorne rechts", "Front right")
                    )
                self.assertEqual(node.attrib["checked"], "false")
                sleep.assert_called_once_with(0.5)

    def test_navigation_coordinates(self):
        activity = (
            "intent={act=android.intent.action.VIEW "
            "dat=google.navigation:q=48.114598%2C11.480513&mode=w}"
        )
        self.assertEqual(
            VolkswagenReader.parse_navigation_coordinates(activity),
            (48.114598, 11.480513),
        )

    def test_navigation_coordinates_use_latest_activity_intent(self):
        activity = (
            "Hist #1: intent={act=android.intent.action.VIEW "
            "dat=google.navigation:q=48.114598%2C11.480513&mode=w}\n"
            "Hist #0: intent={act=android.intent.action.VIEW "
            "dat=google.navigation:q=48.120000%2C11.520000&mode=w}"
        )
        self.assertEqual(
            VolkswagenReader.parse_navigation_coordinates(activity),
            (48.12, 11.52),
        )

    def test_map_view_center_uses_visible_texture_view(self):
        root = ET.fromstring(
            """<hierarchy>
            <node resource-id="com.volkswagen.weconnect:id/catNavMapFragment"
                class="androidx.compose.ui.platform.ComposeView"
                bounds="[0,0][1080,2148]"/>
            <node class="android.view.TextureView" bounds="[0,0][1080,2148]"/>
            </hierarchy>"""
        )
        self.assertEqual(VolkswagenReader.map_view_center(root), (540, 1074))

    def test_map_view_center_falls_back_to_map_container(self):
        root = ET.fromstring(
            """<hierarchy>
            <node resource-id="com.volkswagen.weconnect:id/catNavMapFragment"
                class="androidx.compose.ui.platform.ComposeView"
                bounds="[0,100][1080,2100]"/>
            </hierarchy>"""
        )
        self.assertEqual(VolkswagenReader.map_view_center(root), (540, 1100))

    def test_map_view_center_keeps_legacy_coordinate_fallback(self):
        root = ET.fromstring("<hierarchy />")
        self.assertEqual(VolkswagenReader.map_view_center(root), (540, 786))

    def test_vehicle_marker_label_is_above_centered_map_pin(self):
        root = ET.fromstring(
            """<hierarchy>
            <node class="android.view.TextureView" bounds="[0,0][1080,1984]"/>
            <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]"/>
            </hierarchy>"""
        )
        self.assertEqual(
            VolkswagenReader.vehicle_marker_label_center(root),
            (540, 812),
        )

    def test_vehicle_marker_tap_centers_fall_back_to_centered_pin(self):
        root = ET.fromstring(
            """<hierarchy>
            <node class="android.view.TextureView" bounds="[0,0][1080,1984]"/>
            <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]"/>
            </hierarchy>"""
        )
        self.assertEqual(
            VolkswagenReader.vehicle_marker_tap_centers(root),
            ((540, 812), (540, 992)),
        )

    def test_vehicle_name_is_read_from_german_and_english_overview(self):
        for description in (
            "Ihr Fahrzeug: ID.7 Tourer Pro. Gerade synchronisiert.",
            "Your vehicle: ID.7 Tourer Pro. Just synced.",
            "Your vehicle: Golf GTE OPF 8.5. Synchronised 3 hours 14 minutes ago",
        ):
            with self.subTest(description=description):
                root = ET.fromstring(
                    f'<hierarchy><node content-desc="{description}"/></hierarchy>'
                )
                expected = (
                    "Golf GTE OPF 8.5"
                    if "Golf GTE" in description
                    else "ID.7 Tourer Pro"
                )
                self.assertEqual(VolkswagenReader.parse_vehicle_name(root), expected)

    def test_stale_overview_state_uses_transient_retry_in_both_languages(self):
        for description in (
            "Your vehicle: Example. Synchronised: Data no longer up-to-date",
            "Ihr Fahrzeug: Beispiel. Synchronisiert: Daten nicht mehr aktuell",
        ):
            with self.subTest(description=description):
                root = ET.fromstring(
                    f'<hierarchy><node content-desc="{description}"/></hierarchy>'
                )
                with self.assertRaises(TransientVolkswagenState) as raised:
                    VolkswagenReader.raise_for_lockout_state(root)
                self.assertEqual(raised.exception.reason, "APP_DATA_STALE")
                self.assertEqual(
                    VolkswagenReader.error_category(raised.exception),
                    "APP_DATA_STALE",
                )

    def test_explicit_too_many_requests_state_is_rate_limited(self):
        reader = object.__new__(VolkswagenReader)
        reader.ui_update_timeout = 0
        reader.dump_ui_with_compose_fallback = Mock(
            return_value=ET.fromstring(
                '<hierarchy><node text="Too many requests"/></hierarchy>'
            )
        )
        reader.dismiss_overview_notice = Mock(side_effect=lambda root, _: root)
        reader.app_in_foreground = Mock(return_value=True)

        with self.assertRaises(VolkswagenRateLimit) as raised:
            reader.open_overview()

        self.assertEqual(raised.exception.reason, "TOO_MANY_REQUESTS")
        self.assertEqual(
            VolkswagenReader.error_category(raised.exception), "RATE_LIMIT"
        )

    def test_charge_retry_log_identifies_read_operation(self):
        reader = object.__new__(VolkswagenReader)
        reader.save_diagnostics = Mock()
        reader.launch = Mock()
        operation = Mock(side_effect=RuntimeError("example failure"))

        with (
            self.assertLogs("vw-app-connector", level="WARNING") as logs,
            self.assertRaises(RuntimeError),
        ):
            reader.with_retries(operation, "CHARGE")

        self.assertIn("CHARGE read attempt 1 failed", "\n".join(logs.output))

    def test_semantic_volkswagen_states_are_not_retried_immediately(self):
        reader = object.__new__(VolkswagenReader)
        reader.save_diagnostics = Mock()
        reader.launch = Mock()
        for error in (
            TransientVolkswagenState("APP_DATA_STALE", "stale"),
            VolkswagenRateLimit("TOO_MANY_REQUESTS", "limited"),
        ):
            with self.subTest(error=type(error).__name__):
                operation = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    reader.with_retries(operation, "CHARGE")
                operation.assert_called_once_with()
        reader.save_diagnostics.assert_not_called()
        reader.launch.assert_not_called()

    def test_unavailable_report_uses_transient_retry_in_both_languages(self):
        for message in (
            "Currently unavailable. Please try again later.",
            "Derzeit nicht verfügbar. Bitte versuche es später erneut.",
        ):
            with self.subTest(message=message):
                root = ET.fromstring(
                    f'<hierarchy><node text="{message}"/></hierarchy>'
                )
                with self.assertRaises(TransientVolkswagenState) as raised:
                    VolkswagenReader.raise_for_lockout_state(root)
                self.assertEqual(raised.exception.reason, "APP_UNAVAILABLE")

    def test_gte_overview_parses_electric_and_fuel_range(self):
        text = (
            "Range overview. Battery range: 84 kilometres. "
            "Fuel range: 560 kilometres. Open details"
        )
        self.assertEqual(
            VolkswagenReader.parse_range_value(
                text, ("Batteriereichweite", "Battery range", "Electric range")
            ),
            84,
        )
        self.assertEqual(
            VolkswagenReader.parse_range_value(
                text, ("Kraftstoffreichweite", "Fuel range")
            ),
            560,
        )

    def test_gte_vehicle_report_parses_separate_label_value_nodes(self):
        root = ET.fromstring(
            """<hierarchy>
            <node text="Vehicle Health Report"/>
            <node text="Total distance"/>
            <node text="12,345 km"/>
            <node text="Next service"/>
            <node text="151 days / 12,300 km"/>
            <node text="Next oil service"/>
            <node text="No issues found"/>
            <node text="Synchronised: 3 h 42 min ago"/>
            </hierarchy>"""
        )
        result = DetailData()
        VolkswagenReader.parse_vehicle_report(root, result)
        self.assertEqual(result.odometerKm, 12345)
        self.assertEqual(result.serviceDays, 151)
        self.assertEqual(result.warningStatus, "Keine Meldungen")
        self.assertEqual(result.reportSyncAge, "3 h 42 min ago")

    def test_vehicle_report_parses_space_grouped_odometer_values(self):
        for distance in ("27 886 km", "27\u00a0886 km", "27\u202f886 km"):
            with self.subTest(distance=distance):
                root = ET.fromstring(
                    f"""<hierarchy>
                    <node text="Vehicle Health Report"/>
                    <node text="Total distance"/>
                    <node text="{distance}"/>
                    </hierarchy>"""
                )
                result = DetailData()
                VolkswagenReader.parse_vehicle_report(root, result)
                self.assertEqual(result.odometerKm, 27886)

    def test_gte_vehicle_report_tile_matches_vehicle_health_label(self):
        root = ET.fromstring(
            """<hierarchy>
            <node content-desc="Vehicle Health. Open" bounds="[10,20][110,220]"/>
            </hierarchy>"""
        )
        self.assertEqual(VolkswagenReader.vehicle_report_center(root), (60, 120))

    def test_vehicle_report_tile_does_not_use_generic_vehicle_card(self):
        root = ET.fromstring(
            """<hierarchy>
            <node content-desc="Vehicle. Locked. Open details" bounds="[10,20][110,220]"/>
            </hierarchy>"""
        )
        with self.assertRaisesRegex(RuntimeError, "Volkswagen UI element not found"):
            VolkswagenReader.vehicle_report_center(root)

    def test_vehicle_report_parser_rejects_wrong_page(self):
        root = ET.fromstring(
            """<hierarchy>
            <node content-desc="Vehicle. Locked. Open details"/>
            <node text="Charging settings"/>
            </hierarchy>"""
        )
        with self.assertRaisesRegex(RuntimeError, "vehicle health report"):
            VolkswagenReader.parse_vehicle_report(root, DetailData())

    def test_location_details_parse_combined_address_and_parked_duration(self):
        root = ET.fromstring(
            """<hierarchy>
            <node text="Example Street 1&#10;Geparkt seit 2 Std." bounds="[55,1565][1025,1631]"/>
            <node text="Route" bounds="[502,1987][622,2042]"/>
            </hierarchy>"""
        )
        self.assertEqual(
            VolkswagenReader.parse_location_details(root),
            ("Example Street 1", "Geparkt seit 2 Std."),
        )

    def test_location_details_parse_vw_421_location_card(self):
        """VW 4.2.1 exposes the address and parking time in one card."""
        root = ET.fromstring(
            """<hierarchy>
            <node class="android.widget.TextView"
                text="Västmanlandsgatan 16, SE-262 43&#10;Parked since 4h 48 mins"
                bounds="[30,1676][352,1732]"/>
            <node class="android.widget.TextView" text="Route"
                bounds="[30,1807][176,1855]"/>
            </hierarchy>"""
        )
        self.assertEqual(
            VolkswagenReader.parse_location_details(root),
            ("Västmanlandsgatan 16, SE-262 43", "Parked since 4h 48 mins"),
        )

    def test_location_details_parse_separate_address_text_view(self):
        root = ET.fromstring(
            """<hierarchy>
            <node class="android.widget.TextView" text="ID.7" bounds="[55,1445][196,1510]"/>
            <node class="android.widget.TextView" text="Example Street 1, Example City"
                bounds="[55,1565][1025,1631]"/>
            <node class="android.widget.TextView" text="Route" bounds="[502,1987][622,2042]"/>
            </hierarchy>"""
        )
        self.assertEqual(
            VolkswagenReader.parse_location_details(root),
            ("Example Street 1, Example City", ""),
        )

    def test_location_details_prefers_address_below_vehicle_name(self):
        root = ET.fromstring(
            """<hierarchy>
            <node class="android.widget.TextView" text="11 m" bounds="[56,1630][153,1687]"/>
            <node class="android.widget.TextView" text="Golf GTE OPF 8.5"
                bounds="[56,1721][451,1783]"/>
            <node class="android.widget.TextView" text="[REDACTED_ADDRESS]"
                bounds="[56,1789][728,1894]"/>
            <node class="android.widget.TextView" text="Route" bounds="[169,2000][273,2048]"/>
            <node class="android.widget.TextView" text="Share" bounds="[465,2000][563,2048]"/>
            </hierarchy>"""
        )
        self.assertEqual(
            VolkswagenReader.parse_location_details(root),
            ("[REDACTED_ADDRESS]", ""),
        )

    def test_english_location_parses_separate_parked_duration(self):
        root = ET.fromstring(
            """<hierarchy>
            <node class="android.widget.TextView"
                text="Example Street 1, Example City"
                bounds="[55,1565][1025,1631]"/>
            <node class="android.widget.TextView" text="Parked for 2 hours"
                bounds="[55,1660][1025,1725]"/>
            <node class="android.widget.TextView" text="Route"
                bounds="[502,1987][622,2042]"/>
            </hierarchy>"""
        )
        self.assertEqual(
            VolkswagenReader.parse_location_details(root),
            ("Example Street 1, Example City", "Parked for 2 hours"),
        )

    def test_usage_limiter_persists_and_enforces_daily_budget(self):
        with TemporaryDirectory() as directory:
            environment = {
                "USAGE_STATE_FILE": f"{directory}/usage.json",
                "BACKGROUND_DAILY_LIMIT": "3",
                "BACKGROUND_MIN_INTERVAL_SECONDS": "0",
                "ACTION_DAILY_LIMIT": "1",
                "ACTION_MIN_INTERVAL_SECONDS": "0",
            }
            with patch.dict("os.environ", environment):
                limiter = UsageLimiter()
                limiter.acquire_background(3)
                limiter.acquire_action()
                snapshot = UsageLimiter().snapshot()
                self.assertEqual(snapshot["backgroundUsed"], 3)
                self.assertEqual(snapshot["actionsUsed"], 1)
                with self.assertRaises(UsageLimit):
                    limiter.acquire_background(1)
                with self.assertRaises(UsageLimit):
                    limiter.acquire_action()

    def test_rate_limit_reason_and_successful_probe_are_persisted(self):
        with TemporaryDirectory() as directory:
            environment = {
                "USAGE_STATE_FILE": f"{directory}/usage.json",
                "BACKGROUND_DAILY_LIMIT": "3",
                "BACKGROUND_MIN_INTERVAL_SECONDS": "0",
                "RATE_LIMIT_COOLDOWN_SECONDS": "43200",
                "COOLDOWN_PROBE_MIN_INTERVAL_SECONDS": "900",
            }
            with patch.dict("os.environ", environment):
                limiter = UsageLimiter()
                limiter.record_rate_limit("TOO_MANY_REQUESTS")
                snapshot = UsageLimiter().snapshot()
                self.assertGreater(snapshot["cooldownSeconds"], 43190)
                self.assertEqual(
                    snapshot["cooldownReason"], "TOO_MANY_REQUESTS"
                )
                self.assertTrue(snapshot["cooldownUntil"])
                expected_until = limiter.begin_cooldown_probe()
                limiter.acquire_background(1, bypass_cooldown=True)
                with self.assertRaises(CooldownProbeRejected) as raised:
                    limiter.begin_cooldown_probe()
                self.assertEqual(raised.exception.reason, "PROBE_MIN_INTERVAL")
                self.assertTrue(limiter.clear_rate_limit(expected_until))
                cleared = UsageLimiter().snapshot()
                self.assertEqual(cleared["cooldownSeconds"], 0)
                self.assertEqual(cleared["cooldownReason"], "")
                self.assertEqual(cleared["backgroundUsed"], 1)

    def test_active_cooldown_survives_daily_counter_rollover(self):
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "usage.json"
            state_path.write_text(
                json.dumps(
                    {
                        "day": "1900-01-01",
                        "backgroundUsed": 99,
                        "actionsUsed": 9,
                        "lastBackgroundAt": 0,
                        "lastActionAt": 0,
                        "cooldownUntil": time.time() + 3600,
                        "cooldownReason": "TOO_MANY_REQUESTS",
                        "lastCooldownProbeAt": 0,
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"USAGE_STATE_FILE": str(state_path)}):
                snapshot = UsageLimiter().snapshot()
            self.assertEqual(snapshot["backgroundUsed"], 0)
            self.assertEqual(snapshot["actionsUsed"], 0)
            self.assertEqual(snapshot["cooldownReason"], "TOO_MANY_REQUESTS")
            self.assertGreater(snapshot["cooldownSeconds"], 3590)

    def test_cooldown_probe_clears_only_after_successful_read(self):
        state = object.__new__(AppState)
        state.verified_app_version = "4.0.3"
        state.reader = Mock()
        state.reader.context = SimpleNamespace(background=False)
        state.reader.phone_health.return_value = HealthData(
            adbState="device", appVersion="4.0.3"
        )
        state.reader.read.return_value = VehicleData(status="B", soc=55)
        state.usage = Mock()
        state.usage.begin_cooldown_probe.return_value = 123.0
        state.usage.clear_rate_limit.return_value = True
        state.usage.snapshot.return_value = {"cooldownSeconds": 0}
        state.charge = Mock()

        result = state.probe_cooldown()

        state.usage.acquire_background.assert_called_once_with(
            1, bypass_cooldown=True
        )
        state.usage.clear_rate_limit.assert_called_once_with(123.0)
        state.charge.set_value.assert_called_once()
        self.assertTrue(result["cooldownCleared"])
        self.assertFalse(state.reader.context.background)

    def test_failed_cooldown_probe_preserves_original_expiry(self):
        state = object.__new__(AppState)
        state.verified_app_version = "4.0.3"
        state.reader = Mock()
        state.reader.context = SimpleNamespace(background=False)
        state.reader.phone_health.return_value = HealthData(
            adbState="device", appVersion="4.0.3"
        )
        state.reader.read.side_effect = VolkswagenRateLimit(
            "TOO_MANY_REQUESTS", "Volkswagen app reports too many requests"
        )
        state.usage = Mock()
        state.usage.begin_cooldown_probe.return_value = 123.0
        state.charge = Mock()

        with self.assertRaises(VolkswagenRateLimit):
            state.probe_cooldown()

        state.usage.clear_rate_limit.assert_not_called()
        state.charge.set_value.assert_not_called()
        self.assertFalse(state.reader.context.background)

    def test_background_refresh_yields_to_priority_work(self):
        with TemporaryDirectory() as directory:
            environment = {
                "USAGE_STATE_FILE": f"{directory}/usage.json",
                "BACKGROUND_DAILY_LIMIT": "3",
                "BACKGROUND_MIN_INTERVAL_SECONDS": "0",
            }
            pending = iter((True, False))
            with (
                patch.dict("os.environ", environment),
                patch("time.sleep") as sleep,
            ):
                limiter = UsageLimiter()
                limiter.acquire_background(1, yield_to=lambda: next(pending))
                sleep.assert_called_once_with(0.25)
                self.assertEqual(limiter.snapshot()["backgroundUsed"], 1)

    def test_failed_cache_refresh_sets_retry_backoff(self):
        with patch("threading.Thread.start"):
            cache = BackgroundCache(
                "test",
                lambda: (_ for _ in ()).throw(RuntimeError("failed")),
                lambda _: 60,
                VehicleData,
                error_retry_interval=900,
            )
        before = __import__("time").monotonic()
        result = cache.refresh()
        self.assertEqual(result.error, "failed")
        self.assertGreaterEqual(cache.next_attempt_monotonic, before + 899)

    def test_adb_preflight_defers_all_caches_without_consuming_budget(self):
        state = object.__new__(AppState)
        state.priority_lock = threading.Lock()
        state.priority_waiters = 0
        state.reader = Mock()
        state.reader.action_pending = threading.Event()
        state.reader.context = SimpleNamespace(background=False)
        state.reader.require_background_adb_transport.side_effect = (
            TransientTransportState(
                "ADB_UNAVAILABLE",
                "ADB transport unavailable before Volkswagen app access",
            )
        )
        state.usage = Mock()
        state.usage.background_min_interval = 0

        with TemporaryDirectory() as directory, patch("threading.Thread.start"):
            backoff = BackgroundTransientBackoff(
                Path(directory) / "background-backoff.json",
                base_seconds=900,
                max_seconds=7200,
                jitter_ratio=0,
            )
            state.background_backoff = backoff
            state.background_preflight_lock = threading.Lock()
            charge_loader = state.background_loader(Mock(), 1)
            details_loader = Mock(return_value=DetailData())
            charge = BackgroundCache(
                "charge",
                charge_loader,
                lambda _: 900,
                VehicleData,
                shared_backoff=backoff,
            )
            details = BackgroundCache(
                "details",
                details_loader,
                lambda _: 43200,
                DetailData,
                shared_backoff=backoff,
            )

            failed = charge.refresh()
            deferred = details.refresh()

        self.assertEqual(failed.errorCategory, "ADB_UNAVAILABLE")
        self.assertEqual(deferred.errorCategory, "ADB_UNAVAILABLE")
        self.assertEqual(backoff.snapshot()["failureCount"], 1)
        state.usage.acquire_background.assert_not_called()
        details_loader.assert_not_called()

    def test_successful_cache_refresh_clears_adb_backoff(self):
        with TemporaryDirectory() as directory, patch("threading.Thread.start"):
            backoff = BackgroundTransientBackoff(
                Path(directory) / "background-backoff.json",
                base_seconds=900,
                max_seconds=7200,
                jitter_ratio=0,
            )
            backoff.record_failure("ADB_UNAVAILABLE")
            with backoff.lock:
                backoff.state["nextAttemptAt"] = time.time() - 1
            cache = BackgroundCache(
                "details",
                DetailData,
                lambda _: 43200,
                DetailData,
                shared_backoff=backoff,
            )
            cache.refresh()

        self.assertEqual(backoff.snapshot()["failureCount"], 0)

    def test_background_adb_preflight_raises_after_bounded_recovery(self):
        reader = object.__new__(VolkswagenReader)
        with (
            patch.object(reader, "adb_transport_ready", side_effect=(False, False)),
            patch.object(reader, "recover_adb_transport") as recover,
        ):
            with self.assertRaises(TransientTransportState) as raised:
                reader.require_background_adb_transport()
        self.assertEqual(raised.exception.reason, "ADB_UNAVAILABLE")
        recover.assert_called_once_with()

    def test_transient_background_backoff_escalates_and_persists(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "background-backoff.json"
            with patch("random.uniform", return_value=1.0):
                backoff = BackgroundTransientBackoff(
                    path, base_seconds=900, max_seconds=7200
                )
                backoff.record_failure("APP_DATA_STALE")
                first = backoff.snapshot()
                backoff.record_failure("APP_UNAVAILABLE")
                second = backoff.snapshot()
            self.assertGreaterEqual(first["seconds"], 899)
            self.assertGreaterEqual(second["seconds"], 1799)
            self.assertEqual(second["failureCount"], 2)
            self.assertEqual(second["reason"], "APP_UNAVAILABLE")

            restored = BackgroundTransientBackoff(path, 900, 7200, jitter_ratio=0)
            self.assertEqual(restored.snapshot()["failureCount"], 2)
            restored.clear()
            self.assertEqual(restored.snapshot()["seconds"], 0)
            self.assertEqual(restored.snapshot()["failureCount"], 0)

    def test_transient_background_backoff_jitter_respects_maximum(self):
        with TemporaryDirectory() as directory:
            with patch("random.uniform", return_value=1.1):
                backoff = BackgroundTransientBackoff(
                    Path(directory) / "backoff.json",
                    base_seconds=60,
                    max_seconds=60,
                )
                before = time.time()
                backoff.record_failure("APP_UNAVAILABLE")

            self.assertLessEqual(
                float(backoff.state["nextAttemptAt"]) - before,
                60.1,
            )

    def test_shared_backoff_defers_other_background_caches(self):
        with TemporaryDirectory() as directory:
            backoff = BackgroundTransientBackoff(
                Path(directory) / "backoff.json", 900, 7200, jitter_ratio=0
            )
            loader = Mock(return_value=VehicleData(soc=50))
            with patch("threading.Thread.start"):
                cache = BackgroundCache(
                    "charge",
                    loader,
                    lambda _: 60,
                    VehicleData,
                    shared_backoff=backoff,
                )
            cache.set_value(VehicleData(soc=40))
            backoff.record_failure("APP_DATA_STALE")
            result = cache.refresh()
            loader.assert_not_called()
            self.assertEqual(result.soc, 40)
            self.assertTrue(result.stale)
            self.assertEqual(result.errorCategory, "APP_DATA_STALE")

    def test_successful_but_source_stale_read_starts_shared_backoff(self):
        with TemporaryDirectory() as directory:
            backoff = BackgroundTransientBackoff(
                Path(directory) / "backoff.json", 900, 7200, jitter_ratio=0
            )
            with patch("threading.Thread.start"):
                cache = BackgroundCache(
                    "charge",
                    lambda: VehicleData(sourceStale=True),
                    lambda _: 60,
                    VehicleData,
                    shared_backoff=backoff,
                    shared_backoff_reason=lambda value: (
                        "SOURCE_DATA_STALE" if value.sourceStale else ""
                    ),
                )
            cache.refresh()
            snapshot = backoff.snapshot()
            self.assertEqual(snapshot["reason"], "SOURCE_DATA_STALE")
            self.assertGreaterEqual(snapshot["seconds"], 899)

    def test_cache_update_observes_new_shared_backoff_state(self):
        with TemporaryDirectory() as directory:
            backoff = BackgroundTransientBackoff(
                Path(directory) / "backoff.json", 900, 7200, jitter_ratio=0
            )
            observed = []
            with patch("threading.Thread.start"):
                cache = BackgroundCache(
                    "charge",
                    lambda: VehicleData(sourceStale=True),
                    lambda _: 60,
                    VehicleData,
                    shared_backoff=backoff,
                    shared_backoff_reason=lambda value: (
                        "SOURCE_DATA_STALE" if value.sourceStale else ""
                    ),
                    on_update=lambda _name, _value: observed.append(
                        backoff.snapshot()["reason"]
                    ),
                )

            cache.refresh()

            self.assertEqual(observed, ["SOURCE_DATA_STALE"])

    def test_failed_transient_cache_refresh_is_published(self):
        with TemporaryDirectory() as directory:
            backoff = BackgroundTransientBackoff(
                Path(directory) / "backoff.json", 900, 7200, jitter_ratio=0
            )
            updates = []
            with patch("threading.Thread.start"):
                cache = BackgroundCache(
                    "charge",
                    lambda: (_ for _ in ()).throw(
                        TransientVolkswagenState(
                            "APP_UNAVAILABLE", "Volkswagen app unavailable"
                        )
                    ),
                    lambda _: 60,
                    VehicleData,
                    shared_backoff=backoff,
                    on_update=lambda name, value: updates.append(
                        (name, value.errorCategory, backoff.snapshot()["reason"])
                    ),
                )

            cache.refresh()

            self.assertEqual(
                updates,
                [("charge", "APP_UNAVAILABLE", "APP_UNAVAILABLE")],
            )

    def test_cache_patch_preserves_success_timestamp_and_shared_backoff(self):
        with TemporaryDirectory() as directory:
            backoff = BackgroundTransientBackoff(
                Path(directory) / "backoff.json", 900, 7200, jitter_ratio=0
            )
            updates = []
            with patch("threading.Thread.start"):
                cache = BackgroundCache(
                    "charge",
                    VehicleData,
                    lambda _: 60,
                    VehicleData,
                    shared_backoff=backoff,
                    clears_shared_backoff=lambda _value: True,
                    on_update=lambda _name, value: updates.append(value.targetSoc),
                )
            cache.set_value(VehicleData(status="B", targetSoc=80))
            previous_timestamp = cache.last_success_at
            previous_monotonic = cache.last_success_monotonic
            backoff.record_failure("APP_DATA_STALE")

            cache.patch_value(VehicleData(status="B", targetSoc=90))

            self.assertEqual(cache.value.targetSoc, 90)
            self.assertEqual(cache.last_success_at, previous_timestamp)
            self.assertEqual(cache.last_success_monotonic, previous_monotonic)
            self.assertEqual(cache.value.lastSuccessfulAt, previous_timestamp)
            self.assertEqual(backoff.snapshot()["reason"], "APP_DATA_STALE")
            self.assertEqual(updates, [80, 90])

    def test_vehicle_source_freshness_is_additive_and_stateful(self):
        previous = VehicleData(
            sourceStale=True,
            consecutiveSourceStaleReads=2,
            lastFreshVehicleDataAt="2026-07-20T09:00:00+02:00",
            vehicleEnergyProtectionLastSeenAt="2026-07-20T08:00:00+02:00",
        )
        stale = AppState.annotate_vehicle_source(
            VehicleData(
                syncAgeMinutes=75,
                observedAt="2026-07-20T12:00:00+02:00",
            ),
            previous,
            60,
        )
        self.assertTrue(stale.sourceStale)
        self.assertEqual(stale.sourceAgeMinutes, 75)
        self.assertEqual(stale.sourceObservedAt, "2026-07-20T10:45:00+02:00")
        self.assertEqual(stale.consecutiveSourceStaleReads, 3)
        self.assertEqual(
            stale.lastFreshVehicleDataAt, "2026-07-20T09:00:00+02:00"
        )
        self.assertEqual(
            stale.vehicleEnergyProtectionLastSeenAt,
            "2026-07-20T08:00:00+02:00",
        )

        fresh = AppState.annotate_vehicle_source(
            VehicleData(
                syncAgeMinutes=5,
                observedAt="2026-07-20T12:10:00+02:00",
            ),
            stale,
            60,
            "2026-07-20T12:05:00+02:00",
        )
        self.assertFalse(fresh.sourceStale)
        self.assertEqual(fresh.consecutiveSourceStaleReads, 0)
        self.assertEqual(fresh.lastFreshVehicleDataAt, "2026-07-20T12:05:00+02:00")
        self.assertEqual(
            fresh.vehicleEnergyProtectionLastSeenAt,
            "2026-07-20T12:05:00+02:00",
        )

        unknown = AppState.annotate_vehicle_source(
            VehicleData(observedAt="2026-07-20T12:20:00+02:00"),
            fresh,
            60,
        )
        self.assertFalse(unknown.sourceFreshnessKnown)
        self.assertFalse(unknown.sourceStale)
        self.assertEqual(
            unknown.lastFreshVehicleDataAt, fresh.lastFreshVehicleDataAt
        )

    def test_usage_limit_cache_refresh_logs_without_traceback(self):
        with patch("threading.Thread.start"):
            cache = BackgroundCache(
                "charge",
                lambda: (_ for _ in ()).throw(
                    UsageLimit("Volkswagen rate-limit cooldown active for 43197 seconds")
                ),
                lambda _: 60,
                VehicleData,
                error_retry_interval=900,
            )
        with self.assertLogs("vw-app-connector", level="WARNING") as logs:
            result = cache.refresh()
        output = "\n".join(logs.output)
        self.assertEqual(
            result.error,
            "Volkswagen rate-limit cooldown active for 43197 seconds",
        )
        self.assertIn("charge refresh skipped", output)
        self.assertNotIn("Traceback", output)

    def test_background_cache_restores_last_success_after_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "charge.json"
            with patch("threading.Thread.start"):
                first = BackgroundCache(
                    "charge",
                    VehicleData,
                    lambda _: 900,
                    VehicleData,
                    state_path=path,
                )
                first.set_value(VehicleData(status="B", soc=55))
                restored = BackgroundCache(
                    "charge",
                    VehicleData,
                    lambda _: 900,
                    VehicleData,
                    state_path=path,
                )
            value = restored.get()
            self.assertEqual(value.status, "B")
            self.assertEqual(value.soc, 55)
            self.assertTrue(value.lastSuccessfulAt)
            self.assertEqual(value.error, "")

    def test_location_read_uses_recovery_retries(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "screen_session"),
                    patch.object(reader, "with_retries", return_value=LocationData())
                    as retries,
                ):
                    reader.read_location()
                self.assertEqual(retries.call_args.args[1], "LOCATION")

    def test_location_read_restarts_google_maps_before_route_intent(self):
        start = ET.fromstring(
            """<hierarchy>
            <node content-desc="Navigation Tab" bounds="[10,10][110,110]"/>
            </hierarchy>"""
        )
        map_root = ET.fromstring(
            """<hierarchy>
            <node content-desc="Find vehicle" bounds="[1116,1370][1152,1406]"/>
            </hierarchy>"""
        )
        centered = ET.fromstring(
            """<hierarchy>
            <node class="android.view.TextureView" bounds="[0,0][1080,2148]"/>
            </hierarchy>"""
        )
        details = ET.fromstring(
            """<hierarchy>
            <node text="Example Street 1&#10;Geparkt seit 2 Std."
                bounds="[55,1565][1025,1631]"/>
            <node text="Route" bounds="[502,1987][622,2042]"/>
            </hierarchy>"""
        )
        calls: list[tuple[str, ...]] = []

        def shell(*args: str, **_kwargs: object) -> str:
            calls.append(args)
            if args[:3] == ("dumpsys", "activity", "activities"):
                return "dat=google.navigation:q=48.114598%2C11.480513&mode=w"
            if args[:3] == ("dumpsys", "window", "windows"):
                return "mCurrentFocus=com.volkswagen.weconnect/.SingleActivity"
            return ""

        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "launch"),
                    patch.object(
                        reader,
                        "dump_ui_with_overlay_recovery",
                        side_effect=(start, map_root),
                    ),
                    patch.object(
                        reader,
                        "dump_ui",
                        side_effect=(centered, details),
                    ),
                    patch.object(reader, "shell", side_effect=shell),
                    patch("time.sleep"),
                ):
                    result = reader._read_location()

        self.assertEqual((result.latitude, result.longitude), (48.114598, 11.480513))
        maps_stop = ("am", "force-stop", "com.google.android.apps.maps")
        route_tap = ("input", "tap", "562", "2014")
        find_vehicle_tap = ("input", "tap", "1134", "1388")
        self.assertIn(maps_stop, calls)
        self.assertIn(route_tap, calls)
        self.assertIn(find_vehicle_tap, calls)
        self.assertLess(calls.index(maps_stop), calls.index(route_tap))

    def test_location_retries_vehicle_marker_at_centered_pin(self):
        start = ET.fromstring(
            """<hierarchy>
            <node content-desc="Your vehicle: Example Vehicle. Just synced."/>
            <node content-desc="Navigation Tab" bounds="[10,10][110,110]"/>
            </hierarchy>"""
        )
        map_root = ET.fromstring(
            """<hierarchy>
            <node content-desc="Car Locate Button" bounds="[20,20][120,120]"/>
            </hierarchy>"""
        )
        centered = ET.fromstring(
            """<hierarchy>
            <node class="android.view.TextureView" bounds="[0,0][1080,2148]"/>
            </hierarchy>"""
        )
        other_marker = ET.fromstring(
            """<hierarchy>
            <node text="Public charger"/>
            <node text="Route" bounds="[502,1987][622,2042]"/>
            </hierarchy>"""
        )
        details = ET.fromstring(
            """<hierarchy>
            <node text="Example Vehicle"/>
            <node text="Example Street 1&#10;Parked since 2 hours"
                bounds="[55,1565][1025,1631]"/>
            <node text="Route" bounds="[502,1987][622,2042]"/>
            </hierarchy>"""
        )
        calls: list[tuple[str, ...]] = []

        def shell(*args: str, **_kwargs: object) -> str:
            calls.append(args)
            if args[:3] == ("dumpsys", "activity", "activities"):
                return "dat=google.navigation:q=48.114598%2C11.480513&mode=w"
            if args[:3] == ("dumpsys", "window", "windows"):
                return "mCurrentFocus=com.volkswagen.weconnect/.SingleActivity"
            return ""

        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb-serial", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "launch"),
                    patch.object(
                        reader,
                        "dump_ui_with_overlay_recovery",
                        side_effect=(start, map_root, map_root),
                    ),
                    patch.object(
                        reader,
                        "dump_ui",
                        side_effect=(centered, other_marker, centered, details),
                    ),
                    patch.object(reader, "shell", side_effect=shell),
                    patch("time.sleep"),
                ):
                    result = reader._read_location()

        self.assertEqual((result.latitude, result.longitude), (48.114598, 11.480513))
        self.assertIn(("input", "tap", "540", "913"), calls)
        self.assertIn(("input", "tap", "540", "1074"), calls)
        self.assertIn(("input", "keyevent", "KEYCODE_BACK"), calls)

    def test_location_map_notice_is_dismissed(self):
        notice = ET.fromstring(
            """<hierarchy>
            <node text="This map uses Google Maps"/>
            <node text="Agree" bounds="[400,1800][700,1900]"/>
            </hierarchy>"""
        )
        ready = ET.fromstring(
            """<hierarchy>
            <node content-desc="Car Locate Button" bounds="[20,20][120,120]"/>
            </hierarchy>"""
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "shell") as shell,
                    patch.object(
                        reader,
                        "dump_ui_with_overlay_recovery",
                        return_value=ready,
                    ),
                    patch("time.sleep") as sleep,
                ):
                    self.assertIs(reader.dismiss_map_notice(notice), ready)
                shell.assert_called_once_with("input", "tap", "550", "1850")
                sleep.assert_called_once_with(1)

    def test_app_rating_notice_is_dismissed(self):
        notice = ET.fromstring(
            """<hierarchy>
            <node text="Enjoying Volkswagen?"/>
            <node text="Not now" bounds="[320,1800][760,1900]"/>
            </hierarchy>"""
        )
        ready = ET.fromstring(
            """<hierarchy>
            <node content-desc="Navigation Tab" bounds="[10,10][110,110]"/>
            </hierarchy>"""
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "shell") as shell,
                    patch.object(
                        reader,
                        "dump_ui_with_overlay_recovery",
                        return_value=ready,
                    ),
                    patch("time.sleep") as sleep,
                ):
                    self.assertIs(
                        reader.dismiss_app_notice(
                            notice,
                            "vw-location-start-notice-dismissed.xml",
                        ),
                        ready,
                    )
                shell.assert_called_once_with("input", "tap", "540", "1850")
                sleep.assert_called_once_with(1)

    def test_german_intelligent_power_saving_notice_is_dismissed(self):
        notice = ET.fromstring(
            """<hierarchy>
            <node text="Intelligentes Stromsparen"/>
            <node text="Ihr Fahrzeug nutzt Intelligentes Stromsparen, um die Batterie zu schonen."/>
            <node text="Alles klar" bounds="[75,1820][1175,1910]"/>
            </hierarchy>"""
        )
        ready = ET.fromstring(
            """<hierarchy>
            <node content-desc="Batteriereichweite: 108 km" bounds="[40,330][600,650]"/>
            </hierarchy>"""
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "shell") as shell,
                    patch.object(
                        reader,
                        "dump_ui_with_compose_fallback",
                        return_value=ready,
                    ),
                    patch("time.sleep") as sleep,
                ):
                    self.assertIs(
                        reader.dismiss_overview_notice(
                            notice, "vw-overview-notice-dismissed.xml"
                        ),
                        ready,
                    )
                shell.assert_called_once_with("input", "tap", "625", "1865")
                sleep.assert_called_once_with(1)
                notice_at, notice_count = reader.energy_protection_telemetry()
                self.assertTrue(notice_at)
                self.assertEqual(notice_count, 1)

    def test_english_intelligent_power_saving_notice_is_dismissed(self):
        notice = ET.fromstring(
            """<hierarchy>
            <node text="Intelligent power saving"/>
            <node text="Your vehicle uses intelligent power saving to protect the battery."/>
            <node text="Got it" bounds="[100,1700][980,1820]"/>
            </hierarchy>"""
        )
        ready = ET.fromstring("<hierarchy/>")
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "shell") as shell,
                    patch.object(
                        reader,
                        "dump_ui_with_compose_fallback",
                        return_value=ready,
                    ),
                    patch("time.sleep"),
                ):
                    self.assertIs(
                        reader.dismiss_overview_notice(
                            notice, "vw-overview-notice-dismissed.xml"
                        ),
                        ready,
                    )
                shell.assert_called_once_with("input", "tap", "540", "1760")
                notice_at, notice_count = reader.energy_protection_telemetry()
                self.assertTrue(notice_at)
                self.assertEqual(notice_count, 1)

    def test_unrelated_overview_confirmation_is_not_dismissed(self):
        notice = ET.fromstring(
            """<hierarchy>
            <node text="A different notice"/>
            <node text="Alles klar" bounds="[100,1700][980,1820]"/>
            </hierarchy>"""
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with patch.object(reader, "shell") as shell:
                    self.assertIs(
                        reader.dismiss_overview_notice(
                            notice, "vw-overview-notice-dismissed.xml"
                        ),
                        notice,
                    )
                shell.assert_not_called()

    def test_location_limited_services_fails_with_app_state_error(self):
        limited = ET.fromstring(
            """<hierarchy>
            <node text="Limited Services"/>
            <node text="You are currently not logged into the vehicle"/>
            </hierarchy>"""
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "launch"),
                    patch.object(
                        reader,
                        "dump_ui_with_overlay_recovery",
                        return_value=limited,
                    ),
                ):
                    with self.assertRaises(TransientVolkswagenState) as raised:
                        reader._read_location()
        self.assertEqual(raised.exception.reason, "APP_UNAVAILABLE")
        self.assertIn("limited services", str(raised.exception))

    def test_location_wait_dismisses_map_notice_before_car_locate(self):
        notice = ET.fromstring(
            """<hierarchy>
            <node text="This map uses Google Maps"/>
            <node text="Agree" bounds="[400,1800][700,1900]"/>
            </hierarchy>"""
        )
        ready = ET.fromstring(
            """<hierarchy>
            <node content-desc="Car Locate Button" bounds="[20,20][120,120]"/>
            </hierarchy>"""
        )
        with TemporaryDirectory() as directory:
            with patch.dict(
                "os.environ",
                {"ADB_SERIAL": "usb", "DIAGNOSTICS_DIR": directory},
                clear=False,
            ):
                reader = VolkswagenReader()
                with (
                    patch.object(
                        reader,
                        "dump_ui_with_overlay_recovery",
                        side_effect=(notice, ready),
                    ),
                    patch.object(reader, "shell"),
                    patch("time.sleep"),
                ):
                    self.assertIs(
                        reader.wait_for_car_locate_button("vw-location-map.xml"),
                        ready,
                    )

    def test_miui_obscuring_window_counts_as_foreground(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with patch.object(
                    reader,
                    "shell",
                    return_value=(
                        "mObscuringWindow=Window{123 u0 "
                        "com.volkswagen.weconnect/.SingleActivity}"
                    ),
                ):
                    self.assertTrue(reader.app_in_foreground())

    def test_android_16_window_dump_counts_as_foreground(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with patch.object(
                    reader,
                    "shell",
                    return_value=(
                        "mCurrentFocus=Window{12 u0 "
                        "com.volkswagen.weconnect/com.volkswagen.weconnect.SingleActivity}\n"
                        "mFocusedApp=ActivityRecord{34 u0 "
                        "com.volkswagen.weconnect/.SingleActivity}"
                    ),
                ) as shell:
                    self.assertTrue(reader.app_in_foreground())
                shell.assert_called_once_with("dumpsys", "window", timeout=20)

    def test_resumed_activity_counts_as_foreground_without_current_focus(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with patch.object(
                    reader,
                    "shell",
                    side_effect=(
                        "mCurrentFocus=null",
                        "topResumedActivity=ActivityRecord{12 u0 "
                        "com.volkswagen.weconnect/.SingleActivity t971}",
                    ),
                ):
                    self.assertTrue(reader.app_in_foreground())

    def test_focused_app_does_not_override_notification_shade(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with patch.object(
                    reader,
                    "shell",
                    return_value=(
                        "mCurrentFocus=Window{a266eae u0 NotificationShade}\n"
                        "mFocusedApp=ActivityRecord{233671720 u0 "
                        "com.volkswagen.weconnect/.SingleActivity t971}\n"
                        "mObscuringWindow=Window{5932d67 u0 "
                        "com.android.systemui.wallpapers.ImageWallpaper}"
                    ),
                ):
                    self.assertFalse(reader.app_in_foreground())

    def test_focused_app_does_not_override_launcher_focus(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with patch.object(
                    reader,
                    "shell",
                    return_value=(
                        "mCurrentFocus=Window{12 u0 com.microsoft.launcher/.Launcher}\n"
                        "mFocusedApp=ActivityRecord{34 u0 "
                        "com.volkswagen.weconnect/.SingleActivity}"
                    ),
                ):
                    self.assertFalse(reader.app_in_foreground())

    def test_launch_uses_direct_activity_start_first(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            calls: list[tuple[str, ...]] = []
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with (
                    patch.object(
                        reader,
                        "shell",
                        side_effect=lambda *args, **_kwargs: calls.append(args) or "",
                    ),
                    patch.object(reader, "app_in_foreground", return_value=True),
                    patch("time.sleep"),
                ):
                    reader.launch()
            self.assertEqual(
                calls,
                [
                    ("cmd", "statusbar", "collapse"),
                    ("am", "force-stop", "com.volkswagen.weconnect"),
                    (
                        "am",
                        "start",
                        "-n",
                        "com.volkswagen.weconnect/.SingleActivity",
                    ),
                    ("cmd", "statusbar", "collapse"),
                ],
            )

    def test_launch_falls_back_to_monkey_when_activity_start_misses(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            calls: list[tuple[str, ...]] = []
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with (
                    patch.object(
                        reader,
                        "shell",
                        side_effect=lambda *args, **_kwargs: calls.append(args) or "",
                    ),
                    patch.object(
                        reader,
                        "app_in_foreground",
                        side_effect=(False, True),
                    ),
                    patch("time.sleep"),
                ):
                    reader.launch()
            self.assertEqual(calls[0], ("cmd", "statusbar", "collapse"))
            self.assertEqual(calls[1], ("am", "force-stop", "com.volkswagen.weconnect"))
            self.assertEqual(
                calls[2],
                (
                    "am",
                    "start",
                    "-n",
                    "com.volkswagen.weconnect/.SingleActivity",
                ),
            )
            self.assertEqual(
                calls[5],
                (
                    "monkey",
                    "-p",
                    "com.volkswagen.weconnect",
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                ),
            )

    def test_xiaomi_proximity_overlay_is_detected(self):
        root = ET.fromstring(
            '<hierarchy><node text="Den Kopfhörerbereich nicht abdecken" /></hierarchy>'
        )
        self.assertTrue(VolkswagenReader.is_proximity_overlay(root))
        self.assertFalse(
            VolkswagenReader.is_proximity_overlay(
                ET.fromstring(
                    '<hierarchy><node content-desc="Fahrzeug. Verriegelt." /></hierarchy>'
                )
            )
        )

    def test_empty_ui_dump_triggers_overlay_recovery(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            empty = ET.fromstring("<hierarchy />")
            overview = ET.fromstring(
                '<hierarchy><node content-desc="Batteriereichweite: 329 Kilometer" /></hierarchy>'
            )
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "dump_ui", side_effect=(empty, overview)),
                    patch.object(reader, "shell") as shell,
                    patch("time.sleep"),
                ):
                    result = reader.dump_ui_with_overlay_recovery("overview.xml")
                self.assertIs(result, overview)
                shell.assert_called_once_with(
                    "input", "keyevent", "KEYCODE_VOLUME_UP"
                )

    def test_ui_dump_uses_compressed_fallback_for_launcher_tree(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            launcher = ET.fromstring(
                '<hierarchy><node resource-id="com.microsoft.launcher:id/workspace" text="Search" /></hierarchy>'
            )
            app = ET.fromstring(
                '<hierarchy><node resource-id="com.volkswagen.weconnect:id/rangeTile" /></hierarchy>'
            )
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with (
                    patch.object(reader, "dump_ui", side_effect=(launcher, app)) as dump,
                    patch.object(reader, "app_in_foreground", return_value=True),
                ):
                    result = reader.dump_ui_with_compose_fallback("overview.xml")
            self.assertIs(result, app)
            self.assertEqual(
                dump.call_args_list[1].kwargs,
                {"compressed": True},
            )

    def test_ui_dump_falls_back_to_explicit_emulated_storage_path(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()

            def fake_shell(*args, timeout=20):
                if args == ("uiautomator", "dump", "/sdcard/overview.xml"):
                    return "UI hierarchy dumped"
                if args == ("cat", "/sdcard/overview.xml"):
                    raise RuntimeError("No such file or directory")
                if args == ("cat", "/storage/emulated/0/overview.xml"):
                    return '<hierarchy><node content-desc="Fahrzeug" /></hierarchy>'
                raise AssertionError(args)

            with patch.object(reader, "shell", side_effect=fake_shell):
                root = reader.dump_ui("overview.xml")

            self.assertEqual(VolkswagenReader.strings(root), ["Fahrzeug"])

    def test_ui_dump_retries_transient_adb_failure(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
            attempts = 0

            def fake_shell(*args, timeout=20):
                nonlocal attempts
                if args == ("uiautomator", "dump", "/sdcard/overview.xml"):
                    attempts += 1
                    if attempts == 1:
                        raise RuntimeError("ADB failed (137)")
                    return "UI hierarchy dumped"
                if args == ("cat", "/sdcard/overview.xml"):
                    return '<hierarchy><node text="Ready" /></hierarchy>'
                raise AssertionError(args)

            with (
                patch.object(reader, "shell", side_effect=fake_shell),
                patch("time.sleep") as sleep,
            ):
                root = reader.dump_ui("overview.xml")
            self.assertEqual(VolkswagenReader.strings(root), ["Ready"])
            self.assertEqual(attempts, 2)
            sleep.assert_called_once_with(1)

    def test_wake_screen_uses_power_key_when_wakeup_is_ignored(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with (
                    patch.object(
                        reader,
                        "shell",
                        side_effect=(
                            "",
                            "mWakefulness=Asleep",
                            "",
                            "",
                            "",
                            "",
                            "isKeyguardShowing=false",
                        ),
                    ) as shell,
                    patch("time.sleep"),
                ):
                    reader.wake_screen()
                self.assertIn(
                    ("input", "keyevent", "KEYCODE_POWER"),
                    [call.args for call in shell.call_args_list],
                )
                self.assertIn(
                    ("svc", "power", "stayon", "true"),
                    [call.args for call in shell.call_args_list],
                )

    def test_wake_screen_swipes_nonsecure_keyguard(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with (
                    patch.object(
                        reader,
                        "shell",
                        side_effect=(
                            "",
                            "mWakefulness=Awake",
                            "",
                            "",
                            "",
                            "isKeyguardShowing=true",
                            "Physical size: 1080x2400",
                            "",
                            "isKeyguardShowing=false",
                        ),
                    ) as shell,
                    patch("time.sleep"),
                ):
                    reader.wake_screen()
                self.assertIn(
                    ("input", "swipe", "540", "1920", "540", "480", "700"),
                    [call.args for call in shell.call_args_list],
                )

    def test_auto_adb_prefers_usb_and_falls_back_to_wifi(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "ADB_MODE": "auto",
                "ADB_WIFI_ADDRESS": "192.0.2.10:37123",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with patch.object(reader, "adb_state", return_value="device"):
                    self.assertEqual(reader.select_serial(), "usb-serial")
                    self.assertEqual(reader.adb_transport, "usb")

                def state(serial):
                    return "device" if serial == "192.0.2.10:37123" else ""

                with patch.object(reader, "adb_state", side_effect=state):
                    self.assertEqual(
                        reader.select_serial(), "192.0.2.10:37123"
                    )
                    self.assertEqual(reader.adb_transport, "wifi")

    def test_wifi_adb_reconnects(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "ADB_MODE": "wifi",
                "ADB_WIFI_ADDRESS": "192.0.2.10:37123",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                states = iter(["", "device"])
                with (
                    patch.object(reader, "adb_state", side_effect=lambda _: next(states)),
                    patch.object(
                        reader,
                        "run_adb",
                        return_value=SimpleNamespace(
                            returncode=0,
                            stdout="connected",
                            stderr="",
                        ),
                    ),
                ):
                    self.assertEqual(
                        reader.select_serial(), "192.0.2.10:37123"
                    )
                    self.assertEqual(reader.adb_transport, "wifi")

    def test_auto_without_wifi_configuration_stays_usb_only(self):
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "usb-serial",
                "ADB_MODE": "auto",
                "ADB_WIFI_ADDRESS": "",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with patch.object(reader, "adb_state", return_value="device"):
                    self.assertEqual(reader.select_serial(), "usb-serial")
                with patch.object(reader, "adb_state", return_value=""):
                    with self.assertRaisesRegex(RuntimeError, "Neither USB"):
                        reader.select_serial()

    def test_adb_serial_auto_selects_single_usb_device(self):
        output = """List of devices attached
usb-serial device usb:1-1 product:phone model:phone
192.0.2.10:37123 device product:phone model:phone
offline-serial offline
"""
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "auto",
                "ADB_MODE": "usb",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with (
                    patch.object(
                        reader,
                        "run_adb",
                        return_value=SimpleNamespace(
                            returncode=0,
                            stdout=output,
                            stderr="",
                        ),
                    ),
                    patch.object(reader, "adb_state", return_value="device"),
                ):
                    self.assertEqual(reader.select_serial(), "usb-serial")
                    self.assertEqual(reader.adb_transport, "usb")

    def test_adb_serial_auto_requires_exactly_one_usb_device(self):
        output = """List of devices attached
first device usb:1-1
second device usb:1-2
"""
        with TemporaryDirectory() as directory:
            environment = {
                "ADB_SERIAL": "auto",
                "ADB_MODE": "usb",
                "DIAGNOSTICS_DIR": directory,
            }
            with patch.dict("os.environ", environment, clear=False):
                reader = VolkswagenReader()
                with patch.object(
                    reader,
                    "run_adb",
                    return_value=SimpleNamespace(
                        returncode=0,
                        stdout=output,
                        stderr="",
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "Multiple USB"):
                        reader.select_serial()


if __name__ == "__main__":
    unittest.main()
    HealthData,
    RequestHandler,
