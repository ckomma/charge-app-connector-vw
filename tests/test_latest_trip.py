import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from vw_app_connector import (
    BackgroundCache,
    AppState,
    RequestHandler,
    TripData,
    UsageLimit,
    VolkswagenReader,
)


def trip_screen(*labels):
    root = ET.Element("hierarchy")
    for label in labels:
        ET.SubElement(root, "node", text=label)
    return root


class LatestTripTests(unittest.TestCase):
    def test_endpoint_is_cache_only_and_serves_stale_trip(self):
        handler = RequestHandler.__new__(RequestHandler)
        handler.path = "/trips/latest"
        handler.send_json = Mock()
        with tempfile.TemporaryDirectory() as directory:
            loader = Mock(return_value=TripData(tripEndedAtLocal="2026-08-05T09:00"))
            cache = BackgroundCache(
                "trips", loader, lambda _: float("inf"), TripData, auto_refresh=False,
            )
            handler.state = AppState.__new__(AppState)
            handler.state.latest_trip = cache
            handler.do_GET()
            self.assertEqual(handler.send_json.call_args.args[1], 503)
            loader.assert_not_called()
            cache.set_value(loader.return_value)
            cache.loader = Mock(side_effect=RuntimeError("Test unavailable"))
            cache.refresh()
            handler.do_GET()
            body, status = handler.send_json.call_args.args
            self.assertEqual(status, 200)
            self.assertTrue(body["stale"])
            self.assertEqual(body["tripEndedAtLocal"], "2026-08-05T09:00")
            loader.assert_not_called()

    def test_english_single_trip_card(self):
        root = trip_screen(
            "Driving data", "Last single trip", "05.08.2026", "18:40",
            "Distance travelled", "12 km", "Consumption", "20.5 kWh/100 km",
            "0 l/100 km", "Average speed", "45 km/h", "Driving time", "16 min",
            "All single trips", "Last long-haul trip", "400 km",
        )
        trip = VolkswagenReader.parse_latest_trip(root)
        self.assertEqual(trip.tripEndedAtLocal, "2026-08-05T18:40")
        self.assertEqual((trip.distanceKm, trip.drivingMinutes, trip.averageSpeedKmh), (12, 16, 45))
        self.assertEqual(trip.electricConsumption, {"value": 20.5, "unit": "kWh/100 km"})
        self.assertEqual(trip.fuelConsumption, {"value": 0, "unit": "l/100 km"})

    def test_german_merged_description(self):
        root = ET.Element("hierarchy")
        root.append(ET.Element("node", {
            "content-desc": "Letzte Einzelfahrt, 05.08.2026, 08:12, "
            "Zurückgelegte Strecke: 42,7 km, Verbrauch: 16,3 kWh/100 km, "
            "Durchschnittsgeschwindigkeit: 40 km/h, Fahrzeit: 1 Std. 4 Min., "
            "Alle Einzelfahrten",
        }))
        trip = VolkswagenReader.parse_latest_trip(root)
        self.assertEqual((trip.distanceKm, trip.drivingMinutes), (42.7, 64))
        self.assertEqual(trip.electricConsumption["value"], 16.3)
        self.assertIsNone(trip.fuelConsumption)

    def test_partial_single_trip_does_not_take_fields_from_neighbor(self):
        root = trip_screen(
            "Last single trip", "05.08.2026", "08:12", "Distance travelled", "—.—",
            "Consumption", "20 kWh/100 km", "Driving time", "7 min",
            "Last trip from charging or refuelling", "Distance travelled", "342 km",
        )
        self.assertIsNone(VolkswagenReader.parse_latest_trip(root))

    def test_imperial_distance_and_speed_and_twelve_hour_clock(self):
        root = trip_screen(
            "Last single trip", "05.08.2026", "8:12 PM",
            "Distance travelled", "10 mi", "Average speed", "20 mph",
            "Driving time", "30 min", "All single trips",
        )
        trip = VolkswagenReader.parse_latest_trip(root)
        self.assertEqual((trip.distanceKm, trip.averageSpeedKmh), (16.093, 32.187))
        self.assertEqual(trip.tripEndedAtLocal, "2026-08-05T20:12")

    def test_semantic_driving_data_tile(self):
        root = ET.Element("hierarchy")
        ET.SubElement(root, "node", text="Einwilligung zur Nutzung deiner Fahrdaten", bounds="[0,0][20,20]")
        ET.SubElement(root, "node", {"content-desc": "Fahrdaten. Öffnen", "bounds": "[30,50][60,90]"})
        self.assertEqual(VolkswagenReader.driving_data_tile_center(root), (45, 70))

    def test_missing_tile_stops_without_affecting_other_reads(self):
        with patch.dict(os.environ, {"ADB_SERIAL": "auto"}):
            reader = VolkswagenReader()
        overview = ET.Element("hierarchy", bounds="[0,0][100,100]")
        with (
            patch.object(reader, "launch"),
            patch.object(reader, "open_overview", return_value=overview),
            patch.object(reader, "dump_ui_with_compose_fallback", return_value=overview),
            patch.object(reader, "shell") as shell,
            patch("vw_app_connector.time.sleep"),
        ):
            with self.assertRaisesRegex(UsageLimit, "tile is unavailable"):
                reader._read_latest_trip()
        self.assertEqual(shell.call_count, 3)
        self.assertTrue(all(call.args[:2] == ("input", "swipe") for call in shell.call_args_list))

    def test_pair_is_retained_until_odometer_and_trip_have_both_advanced(self):
        with tempfile.TemporaryDirectory() as directory:
            cached = Path(directory) / "latest.json"
            reader = VolkswagenReader.__new__(VolkswagenReader)
            reader.screen_session = nullcontext
            reader.open_vehicle_report = Mock(
                return_value=trip_screen("Total distance", "100 km")
            )
            reader._read_latest_trip = Mock(
                return_value=TripData(tripEndedAtLocal="2026-08-05T09:00")
            )

            def make_cache():
                cache = BackgroundCache(
                    "trips", lambda: reader.read_latest_trip(cache.value), lambda _: 3600,
                    TripData, state_path=cached, auto_refresh=False,
                )
                return cache

            cache = make_cache()
            self.assertEqual(cache.refresh().odometerKmAtObservation, 100)
            self.assertEqual(cache.refresh().odometerKmAtObservation, 100)
            reader._read_latest_trip.assert_called_once_with()

            reader.open_vehicle_report.return_value = trip_screen("Total distance", "101 km")
            waiting = cache.refresh()
            self.assertEqual(waiting.errorCategory, "TRIP_NOT_UPDATED")
            self.assertTrue(waiting.stale)
            self.assertEqual(waiting.odometerKmAtObservation, 100)
            self.assertEqual(waiting.tripEndedAtLocal, "2026-08-05T09:00")

            cache = make_cache()
            self.assertEqual(cache.get().odometerKmAtObservation, 100)
            reader._read_latest_trip.return_value = TripData(tripEndedAtLocal="2026-08-05T09:10")
            updated = cache.refresh()
            self.assertEqual(updated.odometerKmAtObservation, 101)
            self.assertEqual(updated.tripEndedAtLocal, "2026-08-05T09:10")
            self.assertFalse(updated.stale)
            self.assertEqual(reader._read_latest_trip.call_count, 3)

    def test_latest_trip_swipes_inside_carousel_and_ignores_neighboring_card(self):
        def screen(*cards):
            root = ET.Element("hierarchy")
            parent = ET.SubElement(root, "node", scrollable="true", bounds="[0,0][720,1200]")
            carousel = ET.SubElement(parent, "node", scrollable="true", bounds="[0,170][720,690]")
            carousel.extend(cards)
            return root

        first = screen(trip_screen("Last long-haul trip"), trip_screen("Last single trip"))
        single = trip_screen(
            "Last single trip", "05.08.2026", "18:40", "Distance travelled", "12 km",
            "Consumption", "20 kWh/100 km", "0 l/100 km", "Average speed", "35 km/h",
            "Driving time", "1 h 27 min",
        )
        single[1].set("content-desc", "05/08/2026")
        second = screen(
            single, trip_screen("From charging or refuelling", "Distance travelled", "600 km")
        )
        reader = VolkswagenReader.__new__(VolkswagenReader)
        reader.ui_update_timeout = 5
        reader.launch = Mock()
        reader.open_overview = Mock(
            return_value=ET.fromstring('<hierarchy><node text="Driving Data" bounds="[0,0][60,80]"/></hierarchy>')
        )
        reader.dump_ui_with_compose_fallback = Mock(side_effect=[first, second])
        reader.shell = Mock()
        with patch("vw_app_connector.time.sleep"):
            trip = reader._read_latest_trip()
        self.assertEqual((trip.distanceKm, trip.drivingMinutes), (12, 87))
        reader.shell.assert_any_call("input", "swipe", "590", "430", "130", "430", "350")


if __name__ == "__main__":
    unittest.main()
