"""Sanitized Volkswagen 4.8.1 map accessibility regressions."""
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

from vw_app_connector import VolkswagenReader


class MapAccessibilityTests(unittest.TestCase):
    def test_localized_and_legacy_vehicle_location_controls_are_ready(self):
        for label in ("Fahrzeugstandort", "vehicle location", "FAHRZEUGSTANDORT", "Car Locate Button", "Find vehicle"):
            with self.subTest(label=label):
                root = ET.fromstring(
                    f'<hierarchy><node content-desc="{label}" bounds="[800,900][1000,1100]"/></hierarchy>'
                )
                reader = object.__new__(VolkswagenReader)
                reader.ui_update_timeout = 0
                reader.dump_ui_with_overlay_recovery = Mock(return_value=root)
                reader.dismiss_map_notice = Mock(side_effect=lambda page: page)
                reader.raise_for_location_state = Mock()
                self.assertIs(reader.wait_for_car_locate_button("map.xml"), root)
                self.assertEqual(
                    reader.described_node_center_any(root, reader.CAR_LOCATE_LABELS),
                    (900, 1000),
                )

    def test_localized_map_controls_are_not_inferred_as_an_address(self):
        for label in ("Fahrzeugstandort", "Vehicle location", "VEHICLE LOCATION"):
            with self.subTest(label=label):
                root = ET.fromstring(
                    '<hierarchy>'
                    '<node class="android.widget.TextView" text="Example Road 1" bounds="[10,100][700,150]"/>'
                    f'<node class="android.widget.TextView" text="{label}" bounds="[10,200][700,250]"/>'
                    '<node content-desc="Route" bounds="[10,300][700,350]"/>'
                    '</hierarchy>'
                )
                self.assertEqual(VolkswagenReader.parse_location_details(root), ("Example Road 1", ""))

    def test_map_control_without_an_address_keeps_address_unknown(self):
        root = ET.fromstring(
            '<hierarchy>'
            '<node class="android.widget.TextView" text="Vehicle location" bounds="[10,200][700,250]"/>'
            '<node content-desc="Route" bounds="[10,300][700,350]"/>'
            '</hierarchy>'
        )
        self.assertEqual(VolkswagenReader.parse_location_details(root), ("", ""))
