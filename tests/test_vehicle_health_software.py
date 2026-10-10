"""Sanitized battery-health and vehicle-software observations in both locales."""
from contextlib import nullcontext
import http.client
import json
import threading
import unittest
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from mqtt_publisher import MqttPublisher
from vw_app_connector import (
    AppState, BackgroundCache, DetailData, RequestHandler, SoftwareData,
    TransientEndpointState, TransientVolkswagenState, VehicleData, VolkswagenReader,
)


def ui(*labels):
    root = ET.Element("hierarchy")
    for label in labels:
        ET.SubElement(root, "node", {"text": label, "bounds": "[0,0][100,100]"})
    return root


class BatteryHealthTests(unittest.TestCase):
    def test_localized_percent_and_calculation_date(self):
        for labels, percent, date in (
            (("Batteriezustand", "94,5 %", "Ermittelt am 9. Oktober 2026"), 94.5, "9. Oktober 2026"),
            (("battery HEALTH", "93%", "Calculated on", "October 9, 2026"), 93, "October 9, 2026"),
            (("Battery health. 92%. Calculated on October 8, 2026",), 92, "October 8, 2026"),
            (("Batteriezustand", "Ermittelt am 9. Oktober 2026", "94,5 %"), 94.5, "9. Oktober 2026"),
            (("Battery health", "95%", "Calculated on October 9, 2026", "More information"), 95, "October 9, 2026"),
            (("Battery health. Calculated on October 9, 2026 95%",), 95, "October 9, 2026"),
        ):
            with self.subTest(labels=labels):
                result = DetailData()
                self.assertTrue(VolkswagenReader.parse_battery_health(ui(*labels), result))
                self.assertEqual(result.batteryHealthPercent, percent)
                self.assertEqual(result.batteryHealthCalculatedOn, date)
                self.assertTrue(result.capabilities["vehicle-report.battery-health"])

    def test_missing_or_invalid_health_is_never_charge_soc(self):
        for labels in (
            ("State of charge", "80%"),
            ("Battery health", "--", "State of charge", "80%"),
            ("Battery health", "101%"),
            ("Batteriezustand", "Weitere Informationen", "Ladezustand", "80%"),
        ):
            with self.subTest(labels=labels):
                result = DetailData()
                VolkswagenReader.parse_battery_health(ui(*labels), result)
                self.assertIsNone(result.batteryHealthPercent)

    def test_existing_report_fields_survive_optional_health(self):
        result = DetailData(odometerKm=12345, serviceDays=100)
        VolkswagenReader.parse_battery_health(ui("Batteriezustand", "95%"), result)
        self.assertEqual((result.odometerKm, result.serviceDays), (12345, 100))

    def test_scrolling_is_bounded_and_stops_on_unchanged_report(self):
        reader = object.__new__(VolkswagenReader)
        root = ui("Vehicle health report")
        reader.viewport_size = Mock(return_value=(1000, 2000))
        reader.shell = Mock()
        reader.dump_ui_with_overlay_recovery = Mock(return_value=root)
        with patch("vw_app_connector.time.sleep"):
            reader.collect_battery_health(root, DetailData())
        reader.shell.assert_called_once()
        reader.dump_ui_with_overlay_recovery.assert_called_once()


class ClimateActionVerificationTests(unittest.TestCase):
    def control(self, label):
        root = ui(label)
        root[0].attrib.update({"class": "android.widget.Button", "clickable": "true"})
        return root

    def reader(self, values):
        reader = object.__new__(VolkswagenReader)
        reader.screen_session = Mock()
        reader.screen_session.return_value.__enter__ = Mock()
        reader.screen_session.return_value.__exit__ = Mock(return_value=False)
        reader.launch = Mock()
        reader.open_overview = Mock(return_value=ui("Vorklimatisierung. Aus."))
        reader.shell = Mock()
        reader.detail_wait = 0
        reader.ui_update_timeout = 90
        reader.dump_ui_with_overlay_recovery = Mock(side_effect=[self.control("Starten"), self.control("Stoppen")])
        reader.with_retries = Mock(side_effect=values)
        return reader

    def test_delayed_acknowledgement_reads_without_repeating_the_command(self):
        reader = self.reader([VehicleData(climater=False), VehicleData(climater=True)])
        with patch("vw_app_connector.time.sleep"):
            self.assertTrue(reader.set_climater(True).climater)
        self.assertEqual(reader.shell.call_count, 2)
        self.assertEqual(reader.with_retries.call_count, 2)

    def test_unconfirmed_command_never_returns_success(self):
        reader = self.reader([VehicleData(climater=False)] * 3)
        with patch("vw_app_connector.time.sleep"), self.assertRaisesRegex(RuntimeError, "did not confirm"):
            reader.set_climater(True)
        self.assertEqual(reader.shell.call_count, 2)

    def test_idempotent_stop_still_checks_observed_state(self):
        reader = self.reader([VehicleData(climater=False)])
        self.assertFalse(reader.set_climater(False).climater)
        reader.shell.assert_not_called()

    def test_pending_command_stays_on_action_screen_until_button_changes(self):
        reader = self.reader([VehicleData(climater=True)])
        reader.dump_ui_with_overlay_recovery.side_effect = [self.control("Start"), ui("Stopp"), self.control("Stopp")]
        with patch("vw_app_connector.time.sleep"):
            self.assertTrue(reader.set_climater(True).climater)
        self.assertEqual(reader.dump_ui_with_overlay_recovery.call_count, 3)
        reader.with_retries.assert_called_once()

    def test_climate_page_heading_does_not_confirm_active_state(self):
        reader = self.reader([VehicleData(climater=True)])
        reader.dump_ui_with_overlay_recovery.side_effect = [self.control("Start"), ui("Air conditioning"), self.control("Stop")]
        with patch("vw_app_connector.time.sleep"):
            self.assertTrue(reader.set_climater(True).climater)
        self.assertEqual(reader.dump_ui_with_overlay_recovery.call_count, 3)

    def test_command_can_return_to_localized_overview(self):
        for label in ("Vorklimatisierung. An.", "Air conditioning. On."):
            with self.subTest(label=label):
                reader = self.reader([VehicleData(climater=True)])
                reader.dump_ui_with_overlay_recovery.side_effect = [self.control("Start"), ui(label, "Vehicle locked")]
                with patch("vw_app_connector.time.sleep"):
                    self.assertTrue(reader.set_climater(True).climater)
                reader.with_retries.assert_called_once()

    def test_unchanged_action_button_reports_endpoint_failure_without_relaunch(self):
        reader = self.reader([])
        reader.dump_ui_with_overlay_recovery.side_effect = [self.control("Start"), self.control("Start")]
        with patch("vw_app_connector.time.sleep"), patch("vw_app_connector.time.monotonic", side_effect=[0, 100]):
            with self.assertRaises(TransientEndpointState) as caught:
                reader.set_climater(True)
        self.assertEqual(caught.exception.reason, "CLIMATE_ACTION_UNCONFIRMED")
        reader.with_retries.assert_not_called()


class LockCompletionTests(unittest.TestCase):
    def test_pending_labels_are_unknown_in_both_languages(self):
        for label in ("Wird entriegelt", "Wird verriegelt", "Is being unlocked", "Is being locked", "Unlocking ...", "Locking ...", "Entriegeln ...", "Verriegeln ..."):
            with self.subTest(label=label):
                self.assertIsNone(VolkswagenReader.parse_locked(label))
        for label, expected in (("Entriegelt", False), ("Unlocked", False), ("Verriegelt", True), ("Locked", True)):
            self.assertIs(VolkswagenReader.parse_locked(label), expected)

    def test_waits_for_completed_state_without_restarting_or_retapping(self):
        reader = object.__new__(VolkswagenReader)
        reader.ui_update_timeout = 8
        reader.dump_ui_with_overlay_recovery = Mock(side_effect=[ui("Wird entriegelt"), ui("Entriegelt")])
        reader.shell = Mock()
        with patch("vw_app_connector.time.sleep"):
            reader.wait_for_lock_action_result(False)
        self.assertEqual(reader.dump_ui_with_overlay_recovery.call_count, 2)
        reader.shell.assert_not_called()

    def test_changed_readback_never_returns_idempotent_success(self):
        reader = object.__new__(VolkswagenReader)
        reader.spin = "test-pin"
        reader.screen_session = Mock(return_value=nullcontext())
        reader.launch = Mock()
        reader.open_overview = Mock(return_value=ui("Vehicle. Unlocked."))
        reader.with_retries = Mock(return_value=VehicleData(locked=True))
        with self.assertRaises(TransientEndpointState) as caught:
            reader.set_locked(False)
        self.assertEqual(caught.exception.reason, "LOCK_ACTION_UNCONFIRMED")

    def test_backend_failure_is_not_success(self):
        reader = object.__new__(VolkswagenReader)
        reader.ui_update_timeout = 8
        reader.dump_ui_with_overlay_recovery = Mock(return_value=ui("The connection to your vehicle is currently unstable. Please go directly to the vehicle."))
        with self.assertRaises(TransientEndpointState) as caught:
            reader.wait_for_lock_action_result(False)
        self.assertEqual(caught.exception.reason, "LOCK_ACTION_FAILED")

    def test_pending_timeout_is_not_success(self):
        reader = object.__new__(VolkswagenReader)
        reader.ui_update_timeout = 8
        reader.dump_ui_with_overlay_recovery = Mock(return_value=ui("Is being unlocked"))
        with patch("vw_app_connector.time.monotonic", side_effect=[0, 100]):
            with self.assertRaises(TransientEndpointState) as caught:
                reader.wait_for_lock_action_result(False)
        self.assertEqual(caught.exception.reason, "LOCK_ACTION_UNCONFIRMED")


class SoftwareTests(unittest.TestCase):
    def test_localized_current_and_available(self):
        for labels, version, available, notes in (
            (("Software-Updates", "Aktuelle Software:", "5.4.0", "Fahrzeug ist up to date"), "5.4.0", False, False),
            (("Software Updates", "Current software: ID. Software 5.4", "Update available", "Release notes"), "ID. Software 5.4", True, True),
            (("Aktuelle Software: 4.0", "Update verfügbar", "Versionshinweise"), "4.0", True, True),
        ):
            with self.subTest(labels=labels):
                result = VolkswagenReader.parse_software(ui(*labels))
                self.assertEqual(result.installedVersion, version)
                self.assertEqual(result.updateAvailable, available)
                self.assertEqual(result.releaseNotesAvailable, notes)

    def test_version_only_keeps_update_status_unknown(self):
        result = VolkswagenReader.parse_software(ui("Current software:", "5.4"))
        self.assertIsNone(result.updateAvailable)
        self.assertEqual(result.updateStatus, "unknown")

    def test_contradictory_statuses_remain_unknown(self):
        result = VolkswagenReader.parse_software(ui("Update available", "Vehicle is up-to-date"))
        self.assertIsNone(result.updateAvailable)

    def test_backend_errors_are_not_up_to_date(self):
        for label in ("Updates nicht abrufbar", "Unable to fetch updates"):
            with self.assertRaises(TransientEndpointState) as caught:
                VolkswagenReader.parse_software(ui(label))
            self.assertEqual(caught.exception.reason, "SOFTWARE_SERVICES_UNAVAILABLE")

    def test_heading_only_waits_for_loading(self):
        with self.assertRaises(RuntimeError):
            VolkswagenReader.parse_software(ui("Software Updates"))

    def test_refresh_is_read_only_despite_write_quarantine(self):
        state = object.__new__(AppState)
        state.reader = Mock()
        self.assertTrue(state.supports_action("software/refresh"))
        self.assertFalse(state.is_write_action("software/refresh"))
        state.ensure_action_allowed("software/refresh")
        state.reader.phone_health.assert_not_called()

    def test_absent_row_is_bounded_without_vehicle_writes(self):
        reader = object.__new__(VolkswagenReader)
        reader.launch = Mock()
        reader.open_charging_settings = Mock(return_value=ui("Settings"))
        reader.viewport_size = Mock(return_value=(1000, 2000))
        reader.dump_ui_with_overlay_recovery = Mock(return_value=ui("Settings"))
        reader.shell = Mock()
        with patch("vw_app_connector.time.sleep"):
            result = reader._read_software()
        self.assertFalse(result.available)
        self.assertIsNone(result.updateAvailable)
        self.assertEqual(result.updateStatus, "not-exposed")
        self.assertEqual(reader.shell.call_count, 3)
        self.assertTrue(all(call.args[1] == "swipe" for call in reader.shell.call_args_list))

    def test_loading_settings_is_not_missing_feature(self):
        reader = object.__new__(VolkswagenReader)
        reader.launch = Mock()
        reader.open_charging_settings = Mock(return_value=ui("Datensparmodus", "Ihre Daten werden geladen"))
        with self.assertRaises(TransientVolkswagenState):
            reader._read_software()

    def test_software_settings_support_vehicles_without_charging_controls(self):
        reader = object.__new__(VolkswagenReader)
        reader.ui_update_timeout = 1
        reader.open_overview = Mock(return_value=ui("Overview"))
        reader.find_overview_element = Mock(return_value=(ui("Overview"), (10, 20)))
        reader.shell = Mock()
        reader.wait_for_settings_values = Mock()
        reader.dump_ui_with_overlay_recovery = Mock(return_value=ui("Settings", "Vehicle name"))
        result = reader.open_charging_settings(require_charging_values=False)
        self.assertIn("Vehicle name", reader.strings(result))
        reader.wait_for_settings_values.assert_not_called()


class ManualSoftwareCacheTests(unittest.TestCase):
    def test_cache_only_get_and_persistence_without_worker(self):
        with TemporaryDirectory() as directory, patch("threading.Thread.start") as start:
            path = Path(directory) / "software.json"
            loader = Mock(return_value=SoftwareData(available=True, installedVersion="5.4"))
            cache = BackgroundCache("software", loader, lambda _: 60, SoftwareData,
                                    state_path=path, auto_refresh=False)
            self.assertEqual(cache.get().errorCategory, "NOT_OBSERVED")
            cache.refresh(raise_errors=True)
            restored = BackgroundCache("software", loader, lambda _: 60, SoftwareData,
                                       state_path=path, auto_refresh=False)
            self.assertEqual(restored.get().installedVersion, "5.4")
            with patch.object(restored, "age", return_value=61):
                self.assertTrue(restored.get().stale)
            start.assert_not_called()
            loader.assert_called_once_with()

    def test_error_preserves_last_good_and_propagates_category(self):
        cache = BackgroundCache("software", Mock(side_effect=TransientEndpointState(
            "SOFTWARE_SERVICES_UNAVAILABLE", "Updates unavailable")), lambda _: 60,
            SoftwareData, auto_refresh=False)
        cache.set_value(SoftwareData(available=True, installedVersion="5.4", updateAvailable=True))
        with self.assertRaises(TransientEndpointState):
            cache.refresh(raise_errors=True)
        value = cache.get()
        self.assertEqual(value.installedVersion, "5.4")
        self.assertTrue(value.stale)
        self.assertEqual(value.errorCategory, "SOFTWARE_SERVICES_UNAVAILABLE")

    def test_http_cache_and_refresh_authentication(self):
        cache = BackgroundCache("software", Mock(), lambda _: 60, SoftwareData, auto_refresh=False)
        state = Mock(software=cache)
        state.action.return_value = SoftwareData(available=True, installedVersion="5.4")
        with patch.object(RequestHandler, "state", state, create=True), patch.object(RequestHandler, "api_key", "test-key", create=True):
            server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for method, headers, expected in (
                    ("GET", {}, 503), ("POST", {}, 401),
                    ("POST", {"X-API-Key": "test-key"}, 200),
                ):
                    connection = http.client.HTTPConnection(*server.server_address)
                    connection.request(method, "/software" if method == "GET" else "/action/software/refresh", headers=headers)
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                    connection.close()
                state.action.assert_called_once_with("software/refresh", {})
                cache.loader.assert_not_called()
                state.action.side_effect = TransientEndpointState("SOFTWARE_SERVICES_UNAVAILABLE", "Updates unavailable")
                connection = http.client.HTTPConnection(*server.server_address)
                connection.request("POST", "/action/software/refresh", headers={"X-API-Key": "test-key"})
                response = connection.getresponse()
                self.assertEqual(response.status, 503)
                self.assertEqual(json.loads(response.read())["errorCategory"], "SOFTWARE_SERVICES_UNAVAILABLE")
                connection.close()
            finally:
                server.shutdown()
                server.server_close()

    def test_mqtt_optional_values_use_field_availability(self):
        publisher = object.__new__(MqttPublisher)
        publisher.client_id = "test"
        publisher.topic_prefix = "test"
        publisher.discovery_prefix = "homeassistant"
        publisher.availability_topic = "test/availability"
        publisher.client = Mock()
        publisher._publish_discovery()
        configs = {json.loads(c.args[1])["unique_id"]: json.loads(c.args[1])
                   for c in publisher.client.publish.call_args_list}
        for name in ("battery_health", "battery_health_date", "vehicle_software", "software_update_available"):
            config = configs["test_" + name]
            self.assertEqual(config["availability_mode"], "all")
            self.assertIn("is not none", config["availability"][1]["value_template"])
        self.assertNotIn("device_class", configs["test_battery_health"])
        self.assertGreater(configs["test_vehicle_software"]["expire_after"], 0)
        self.assertIn("as_timestamp", configs["test_vehicle_software"]["availability"][1]["value_template"])


if __name__ == "__main__":
    unittest.main()
