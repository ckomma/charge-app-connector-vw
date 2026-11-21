import os
import unittest
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from vw_app_connector import TransientEndpointState, VolkswagenReader


class VehicleReportReadyTests(unittest.TestCase):
    def reader(self, pages):
        reader = VolkswagenReader.__new__(VolkswagenReader)
        reader.detail_wait = 0
        reader.ui_update_timeout = 2
        reader.launch = Mock()
        reader.open_overview = Mock(return_value=ET.fromstring(
            '<hierarchy><node content-desc="Vehicle Health Report" bounds="[0,0][100,100]"/></hierarchy>'
        ))
        reader.shell = Mock()
        reader.dump_ui = Mock(side_effect=pages)
        reader.screen_session = lambda: nullcontext()
        return reader

    def assert_empty_report(self, root):
        reader = self.reader([root] * 10)
        clock = [1000.0]
        with (
            patch("time.monotonic", side_effect=lambda: clock[0]),
            patch("time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
        ):
            with self.assertRaises(TransientEndpointState) as caught:
                # The meaningful state must escape without immediate relaunch.
                reader.with_retries(reader.open_vehicle_report, "TRIPS")
        self.assertEqual(caught.exception.reason, "VEHICLE_REPORT_NOT_READY")
        reader.launch.assert_called_once()
        reader.shell.assert_called_once()
        self.assertGreater(reader.dump_ui.call_count, 1)
        self.assertLessEqual(clock[0], 1002)

    def test_empty_report_is_bounded_and_localized(self):
        for title in ("Vehicle Health Report", "Fahrzeugzustandsbericht"):
            with self.subTest(title=title):
                self.assert_empty_report(ET.fromstring(
                    f'<hierarchy><node content-desc="{title}"/></hierarchy>'
                ))

    def test_waits_for_content_without_retapping(self):
        for title, distance in (
            ("Vehicle Health Report", "Total distance"),
            ("Fahrzeugzustandsbericht", "Gesamtstrecke"),
        ):
            shell = ET.fromstring(f'<hierarchy><node content-desc="{title}"/></hierarchy>')
            ready = ET.fromstring(f'<hierarchy><node text="{distance}"/><node text="12345 km"/></hierarchy>')
            reader = self.reader([shell, ready])
            with patch("time.sleep"):
                self.assertIs(reader.open_vehicle_report(), ready)
            reader.shell.assert_called_once()
            self.assertEqual(reader.dump_ui.call_count, 2)

    def test_wrong_page_remains_a_navigation_error(self):
        reader = self.reader([ET.fromstring('<hierarchy><node text="Home"/></hierarchy>')])
        with patch("time.sleep"), self.assertRaisesRegex(RuntimeError, "did not open"):
            reader.open_vehicle_report()
        reader.dump_ui.assert_called_once()

    def test_rate_limit_and_feedback_states_are_not_masked_by_waiting(self):
        for reason in ("APP_INTERACTION_REQUIRED", "APP_DATA_STALE"):
            reader = self.reader([])
            reader.dump_ui.side_effect = TransientEndpointState(reason, "Manual interaction needed")
            with patch("time.sleep"), self.assertRaises(TransientEndpointState) as caught:
                reader.open_vehicle_report()
            self.assertEqual(caught.exception.reason, reason)
            reader.dump_ui.assert_called_once()

    @unittest.skipUnless(os.getenv("REPORT_DIAGNOSTICS_DIR"), "External diagnostics not supplied")
    def test_issue_diagnostics_are_empty_reports_not_failed_navigation(self):
        # Raw UI dumps remain outside Git. Set this directory for attachment replay.
        files = list(Path(os.environ["REPORT_DIAGNOSTICS_DIR"]).glob("*.xml"))
        self.assertGreaterEqual(len(files), 3)
        for path in files:
            with self.subTest(file=path.name):
                self.assert_empty_report(ET.parse(path).getroot())


if __name__ == "__main__":
    unittest.main()
