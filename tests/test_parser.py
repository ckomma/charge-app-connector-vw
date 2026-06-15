import unittest
import xml.etree.ElementTree as ET
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from vw_app_connector import (
    BackgroundCache,
    LocationData,
    UsageLimit,
    UsageLimiter,
    VehicleData,
    VolkswagenReader,
)


class ParserTests(unittest.TestCase):
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

    def test_navigation_coordinates(self):
        activity = (
            "intent={act=android.intent.action.VIEW "
            "dat=google.navigation:q=48.114598%2C11.480513&mode=w}"
        )
        self.assertEqual(
            VolkswagenReader.parse_navigation_coordinates(activity),
            (48.114598, 11.480513),
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
                        side_effect=("", "mWakefulness=Asleep", "", "", "", ""),
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
