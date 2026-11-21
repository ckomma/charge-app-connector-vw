import threading
import unittest
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vw_app_connector import (
    AppState, DetailData, LocationData, TransientEndpointState, TripData,
    UsageLimiter, VehicleData, VolkswagenReader,
)


class BackgroundDayTests(unittest.TestCase):
    def run_day(self, start_hour=0, start_minute=0, used=0, charging=False,
                empty_report=False, stale_window=False, restart=False, rollover=False):
        start = datetime(2026, 11, 20, start_hour, start_minute).astimezone()
        end = datetime(2026, 11, 21, 1 if rollover else 0).astimezone()
        clock = [start.timestamp()]
        reads = defaultdict(list)
        successes = defaultdict(list)
        exhaustion = []
        def local():
            return datetime.fromtimestamp(clock[0]).astimezone()

        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {
                "CACHE_STATE_DIR": str(Path(directory) / "cache"),
                "USAGE_STATE_FILE": str(Path(directory) / "usage.json"),
                "BACKGROUND_DAILY_LIMIT": "180",
                "BACKGROUND_MIN_INTERVAL_SECONDS": "300",
                "TRIP_POLL_ENABLED": "true",
                "TRIP_INTERVAL_SECONDS": "3600",
                "CHARGING_INTERVAL_SECONDS": "300",
                "IDLE_INTERVAL_SECONDS": "900",
            }),
            patch("threading.Thread.start"),
            patch("time.time", side_effect=lambda: clock[0]),
            patch("time.monotonic", side_effect=lambda: clock[0]),
            patch("time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
            patch("vw_app_connector.datetime", wraps=datetime) as dates,
            patch("vw_app_connector.VolkswagenReader") as reader_class,
            patch("vw_app_connector.MqttPublisher.from_environment", return_value=None),
        ):
            dates.now.side_effect = local
            reader_class.error_category.side_effect = VolkswagenReader.error_category
            reader = reader_class.return_value
            reader.context = threading.local()
            reader.action_pending = threading.Event()
            reader.energy_protection_telemetry.return_value = ("", 0)

            def read_charge(**kwargs):
                current = local()
                reads["charge"].append(clock[0])
                age = 180 if stale_window and 11 <= current.hour < 14 else 0
                marker = "evening" if current.hour >= 17 else "morning"
                callback = kwargs.get("on_driving_data")
                if callback:
                    callback(marker, age)
                successes["charge"].append(clock[0])
                return VehicleData(
                    soc=55 if current.hour < 17 else 30, status=(
                        "C" if charging and 12 <= current.hour < 14 else "A"
                    ), syncAgeMinutes=age, observedAt=current.isoformat(),
                )

            def report(name):
                reads[name].append(clock[0])
                if empty_report and local().hour < 9:
                    raise TransientEndpointState(
                        "VEHICLE_REPORT_NOT_READY", "Report open but empty",
                    )
                successes[name].append(clock[0])
                if name == "latest-trip":
                    return TripData(
                        observedAt=local().isoformat(), odometerKmAtObservation=(
                            1010 if local().hour < 17 else 1020
                        ), tripEndedAtLocal=local().strftime("%Y-%m-%dT%H:%M"),
                    )
                return DetailData(observedAt=local().isoformat(), odometerKm=1010)

            reader.read.side_effect = read_charge
            reader.read_latest_trip.side_effect = lambda _previous: report("latest-trip")
            reader.read_details.side_effect = lambda: report("details")
            reader.read_location.side_effect = lambda: (
                reads["location"].append(clock[0]) or
                successes["location"].append(clock[0]) or LocationData()
            )
            state = AppState()
            with state.usage.lock:
                state.usage.state["backgroundUsed"] = used
                state.usage._save()
            state.observe_driving_data_summary("baseline")
            state.latest_trip.request_early_refresh()
            restarted = False
            maximum_used = used
            while clock[0] < end.timestamp():
                if restart and not restarted and local().hour >= 14:
                    before = state.usage.snapshot()["backgroundUsed"]
                    state = AppState()
                    self.assertEqual(state.usage.snapshot()["backgroundUsed"], before)
                    restarted = True
                caches = [state.charge, state.latest_trip, state.details, state.location]
                if state.latest_trip.early_refresh_requested:
                    caches[0], caches[1] = caches[1], caches[0]
                for cache in caches:
                    if (
                        cache.refresh_due()
                        and clock[0] >= cache.next_attempt_monotonic
                        and state.background_backoff.retry_in_seconds() <= 0
                    ):
                        value = cache.refresh()
                        if value.errorCategory == "RATE_LIMIT":
                            exhaustion.append(clock[0])
                maximum_used = max(maximum_used, state.usage.snapshot()["backgroundUsed"])
                clock[0] += 60
            final_used = state.usage.snapshot()["backgroundUsed"]

        self.assertLessEqual(maximum_used, 180)
        self.assertFalse(exhaustion, "Scheduling must not spend the last unit hours before midnight")
        self.assertGreater(len(successes["charge"]), 5)
        self.assertGreater(len(successes["latest-trip"]), 2)
        self.assertGreater(len(successes["details"]), 0, "Details must recover after a temporary empty report")
        # Both services must still run late in the day; protecting SoC alone is
        # insufficient. These assertions reproduce the 0.1.36 starvation.
        cutoff = datetime(2026, 11, 20, 20).astimezone().timestamp()
        self.assertGreater(successes["charge"][-1], cutoff)
        self.assertGreater(successes["latest-trip"][-1], cutoff)
        self.assertTrue(any(
            datetime.fromtimestamp(t).hour < 11 for t in successes["latest-trip"]
        ), "The morning trip must not be deferred until evening")
        self.assertTrue(any(
            17 <= datetime.fromtimestamp(t).hour < 20 for t in successes["latest-trip"]
        ), "The evening trip must be observed before the late-night checks")
        for name in ("charge", "latest-trip"):
            gaps = [b - a for a, b in zip(successes[name], successes[name][1:])]
            self.assertLess(max(gaps), 4 * 3600, name)
        if empty_report:
            self.assertLessEqual(sum(
                datetime.fromtimestamp(t).hour < 9 for t in reads["details"]
            ), 2)
        if rollover:
            self.assertLess(final_used, maximum_used)
        return {
            "reads": {name: len(values) for name, values in successes.items()},
            "maximumDailyUsage": maximum_used,
            "finalUsage": final_used,
            "maxSocGapMinutes": round(max(
                b - a for a, b in zip(successes["charge"], successes["charge"][1:])
            ) / 60),
        }

    def test_normal_day_with_morning_and_evening_trip_updates(self):
        self.run_day()

    def test_reported_morning_deficit_keeps_both_services_running(self):
        self.run_day(7, 50, used=111)

    def test_early_heavy_usage_then_empty_report_and_source_stale(self):
        self.run_day(7, used=106, empty_report=True, stale_window=True)

    def test_charging_period_and_restart_preserve_budget_and_trip_liveness(self):
        self.run_day(7, used=106, charging=True, restart=True)

    def test_local_midnight_resets_budget_without_manual_counter_reset(self):
        self.run_day(7, used=106, rollover=True)


if __name__ == "__main__":
    unittest.main()
