import http.client
import json
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vw_app_connector import (
    ActionJobManager,
    ActionQuarantined,
    AppState,
    BackgroundCache,
    ChargingLocationSettingsData,
    ChargingLocationsData,
    ChargingSettingsData,
    HealthData,
    IdempotencyConflict,
    LocationData,
    RequestHandler,
    UsageLimit,
    UsageLimiter,
    VehicleData,
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
        state = VehicleData(status="B", soc=55, locked=True)
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
        self.assertEqual(json.loads(state_call[0][1])["soc"], 55)
        self.assertTrue(state_call[1]["retain"])
        self.assertEqual(mqtt.client.username, "connector")
        self.assertEqual(mqtt.client.password, "secret")

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

    def test_mqtt_failure_does_not_escape_cache_update(self):
        state = object.__new__(AppState)
        state.mqtt = Mock()
        state.mqtt.publish_state.side_effect = RuntimeError("broker unavailable")
        with self.assertLogs("vw-app-connector", level="ERROR"):
            state._cache_updated("charge", VehicleData(status="B"))

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
            </hierarchy>"""
        )
        reader = object.__new__(VolkswagenReader)
        self.assertEqual(
            reader.read_charging_settings(root),
            ChargingSettingsData(targetSoc=80, batteryCare=True, reducedAc=False),
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

    def test_target_soc_without_active_charging(self):
        result = VehicleData()
        VolkswagenReader.parse_charging_details(
            "Ladedetails. Zielladestand: 80 Prozent", result
        )
        self.assertEqual(result.targetSoc, 80)
        self.assertIsNone(result.remainingChargeMinutes)

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
                        "dump_ui_with_overlay_recovery",
                        side_effect=(loading, ready),
                    ),
                    patch.object(reader, "shell") as shell,
                    patch("time.sleep") as sleep,
                ):
                    self.assertIs(reader.open_overview(), ready)
                shell.assert_not_called()
                sleep.assert_called_once_with(0.5)

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

    def test_vehicle_name_is_read_from_german_and_english_overview(self):
        for description in (
            "Ihr Fahrzeug: ID.7 Tourer Pro. Gerade synchronisiert.",
            "Your vehicle: ID.7 Tourer Pro. Just synced.",
        ):
            with self.subTest(description=description):
                root = ET.fromstring(
                    f'<hierarchy><node content-desc="{description}"/></hierarchy>'
                )
                self.assertEqual(
                    VolkswagenReader.parse_vehicle_name(root),
                    "ID.7 Tourer Pro",
                )

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


if __name__ == "__main__":
    unittest.main()
    HealthData,
    RequestHandler,
