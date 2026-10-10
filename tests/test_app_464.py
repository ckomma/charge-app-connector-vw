"""Sanitized Volkswagen 4.6.4 data-saving recovery regressions."""

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vw_app_connector import (
    AppState,
    BackgroundCache,
    BackgroundTransientBackoff,
    TransientVolkswagenState,
    VehicleData,
    VolkswagenReader,
)


def ui(*labels):
    root = ET.Element("hierarchy")
    for label in labels:
        ET.SubElement(root, "node", {"content-desc": label})
    return root


class DataSavingRecoveryTests(unittest.TestCase):
    def test_localized_recovery_headlines_and_accessibility_body(self):
        for root in (
            ui("Einen Augenblick, Ihre Daten werden geladen", "Datensparmodus"),
            ui("Just a moment, your data is loading", "Data saving mode"),
            ui("Ladedaten werden jetzt aktualisiert", "Datensparmodus"),
            ui("Charging data is now being updated", "Data saving mode"),
            ui("Datensparmodus aktiviert. Ihre Fahrzeugdaten wie etwa "
               "Ladedaten werden jetzt aktualisiert."),
            ui("Data saving mode has been activated. Vehicle information, "
               "such as charging data, is now being updated."),
        ):
            with self.subTest(labels=VolkswagenReader.strings(root)):
                with self.assertRaises(TransientVolkswagenState) as caught:
                    VolkswagenReader.raise_for_lockout_state(root)
                self.assertEqual(caught.exception.reason, "APP_DATA_UPDATING")
                self.assertEqual(
                    VolkswagenReader.error_category(caught.exception),
                    "APP_DATA_UPDATING",
                )
                # This state is not an explicit server request-limit response.
                VolkswagenReader.raise_for_rate_limit_state(root)

    def test_settings_help_and_unrelated_loading_do_not_block_reads(self):
        for root in (
            ui("Datensparmodus", "Nach 30 Tagen automatisch aktualisieren"),
            ui("Data saving mode", "Pause charging data after 30 days"),
            ui("Your data is loading"),
            ui("Ihre Daten werden geladen"),
            ui("Data saving mode", "Charging data for your vehicle"),
            ui("Intelligent power saving", "Your data is loading"),
        ):
            with self.subTest(labels=VolkswagenReader.strings(root)):
                VolkswagenReader.raise_for_lockout_state(root)

    def test_recovery_does_not_relaunch_tap_or_collect_diagnostics(self):
        reader = object.__new__(VolkswagenReader)
        reader.save_diagnostics = Mock()
        reader.launch = Mock()
        reader.shell = Mock()

        def loading():
            VolkswagenReader.raise_for_lockout_state(
                ui("Just a moment, your data is loading", "Data saving mode")
            )

        operation = Mock(side_effect=loading)
        with self.assertRaises(TransientVolkswagenState):
            reader.with_retries(operation, "CHARGE")
        operation.assert_called_once_with()
        reader.launch.assert_not_called()
        reader.shell.assert_not_called()
        reader.save_diagnostics.assert_not_called()

    def test_new_read_compatibility_does_not_authorize_untested_writes(self):
        self.assertEqual(
            AppState.version_policy("4.6.4", "4.3.2"),
            (False, "UNVERIFIED_APP_VERSION"),
        )

    def test_recovery_preserves_cache_and_persists_shared_backoff(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "backoff.json"
            backoff = BackgroundTransientBackoff(path, 900, 7200, jitter_ratio=0)

            def loading():
                VolkswagenReader.raise_for_lockout_state(
                    ui("Ihre Daten werden geladen", "Datensparmodus")
                )

            loader = Mock(side_effect=loading)
            with patch("threading.Thread.start"):
                cache = BackgroundCache(
                    "charge", loader, lambda _: 60, VehicleData,
                    shared_backoff=backoff,
                )
            cache.set_value(VehicleData(soc=40))
            result = cache.refresh()
            self.assertEqual(result.soc, 40)
            self.assertTrue(result.stale)
            self.assertEqual(result.errorCategory, "APP_DATA_UPDATING")
            loader.assert_called_once_with()
            restored = BackgroundTransientBackoff(path, 900, 7200, jitter_ratio=0)
            self.assertEqual(restored.snapshot()["reason"], "APP_DATA_UPDATING")
            self.assertGreater(restored.snapshot()["seconds"], 0)
            cache.refresh()
            loader.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
