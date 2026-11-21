import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vw_app_connector import (
    AppState, BackgroundCache, ChargeRefreshInterval, HealthData, TransientEndpointState,
    UsageLimit, UsageLimiter, VehicleData, VolkswagenReader,
)


class BackgroundBudgetTests(unittest.TestCase):
    def limiter(self, directory, limit=10):
        with patch.dict("os.environ", {
            "USAGE_STATE_FILE": str(Path(directory) / "usage.json"),
            "BACKGROUND_DAILY_LIMIT": str(limit),
            "BACKGROUND_MIN_INTERVAL_SECONDS": "0",
        }):
            return UsageLimiter()

    def test_optional_read_preserves_reserve_and_charge_can_use_it(self):
        with TemporaryDirectory() as directory:
            limiter = self.limiter(directory)
            limiter.acquire_background(5)
            with self.assertRaises(TransientEndpointState) as caught:
                limiter.acquire_background(1, reserve=lambda: 5)
            self.assertEqual(caught.exception.reason, "BACKGROUND_BUDGET_RESERVED")
            self.assertEqual(limiter.snapshot()["backgroundUsed"], 5)
            limiter.acquire_background(1)
            self.assertEqual(limiter.snapshot()["backgroundUsed"], 6)
            self.assertEqual(self.limiter(directory).snapshot()["backgroundUsed"], 6)

    def test_reserve_allows_exact_fit_and_atomic_concurrent_admission(self):
        with TemporaryDirectory() as directory:
            limiter = self.limiter(directory, limit=4)

            def optional():
                try:
                    limiter.acquire_background(2, reserve=lambda: 2)
                    return True
                except TransientEndpointState:
                    return False

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: optional(), range(2)))
            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(limiter.snapshot()["backgroundUsed"], 2)
            limiter.acquire_background(2)
            with self.assertRaises(UsageLimit):
                limiter.acquire_background(1)

    def test_cooldown_still_blocks_reserved_and_charge_reads(self):
        with TemporaryDirectory() as directory:
            limiter = self.limiter(directory)
            limiter.record_rate_limit("TOO_MANY_REQUESTS")
            for reserve in (None, lambda: 5):
                with self.subTest(reserve=reserve), self.assertRaises(UsageLimit):
                    limiter.acquire_background(1, reserve=reserve)
            self.assertEqual(limiter.snapshot()["backgroundUsed"], 0)

    def test_reservation_releases_at_day_rollover_without_resetting_counters(self):
        with TemporaryDirectory() as directory:
            with patch.object(UsageLimiter, "today", return_value="2026-11-20"):
                limiter = self.limiter(directory)
                limiter.acquire_background(9)
                with self.assertRaises(TransientEndpointState):
                    limiter.acquire_background(1, reserve=lambda: 2)
            with patch.object(UsageLimiter, "today", return_value="2026-11-21"):
                limiter.acquire_background(1, reserve=lambda: 2)
                self.assertEqual(limiter.snapshot()["backgroundUsed"], 1)

    def test_reserve_tracks_remaining_day_and_current_charge_interval(self):
        state = AppState.__new__(AppState)
        policy = ChargeRefreshInterval(300, 900)
        state.background_intervals = {"charge": (1, 900)}
        state.charge = SimpleNamespace(
            lock=threading.Lock(), value=VehicleData(status="A"), interval=policy,
        )
        for hour, minute, status, expected in (
            (18, 0, "A", 2), (18, 0, "C", 2),
            (23, 59, "A", 1), (0, 0, "A", 2),
        ):
            with self.subTest(hour=hour, minute=minute, status=status):
                state.charge.value = VehicleData(status=status)
                policy.observe(state.charge.value)
                with patch(
                    "time.time",
                    return_value=datetime(2026, 11, 20, hour, minute).astimezone().timestamp(),
                ):
                    self.assertEqual(state.charge_budget_reserve(), expected)

    def test_health_exposes_reserve_as_warning_without_connector_failure(self):
        state = AppState.__new__(AppState)
        state.verified_app_version = "4.8.1"
        state.reader = Mock()
        state.reader.phone_health.return_value = HealthData(
            adbState="device", appVersion="4.8.1",
        )
        state.charge = SimpleNamespace(
            last_success_at="now", refreshing=False, value=VehicleData(status="A"),
            last_error="", age=lambda: 1,
        )
        state.details = SimpleNamespace(
            last_success_at="now", age=lambda: 1,
            last_error_category="BACKGROUND_BUDGET_RESERVED",
        )
        state.location = SimpleNamespace(last_success_at="now", age=lambda: 1)
        state.usage = Mock()
        state.usage.snapshot.return_value = {
            "backgroundUsed": 150, "backgroundLimit": 180,
            "actionsUsed": 0, "actionsLimit": 20, "cooldownSeconds": 0,
        }
        health = state.health()
        self.assertEqual(health.status, "ok")
        self.assertEqual(health.statusReasons, ())
        self.assertEqual(health.dataWarnings, ("BACKGROUND_BUDGET_RESERVED",))
        state.details.last_error_category = "VEHICLE_REPORT_NOT_READY"
        health = state.health()
        self.assertEqual(health.status, "degraded")
        self.assertIn("VEHICLE_REPORT_NOT_READY", health.statusReasons)

        for name, attribute, interval in (
            ("TRIP", "latest_trip", 3600), ("DETAIL", "details", 43200),
            ("LOCATION", "location", 14400),
        ):
            with self.subTest(name=name):
                state.details.last_error_category = ""
                cache = BackgroundCache(
                    attribute, Mock(), lambda _, seconds=interval: seconds,
                    VehicleData, auto_refresh=False,
                )
                cache.set_value(VehicleData())
                cache.last_success_monotonic = time.monotonic() - 2 * interval - 1
                setattr(state, attribute, cache)
                health = state.health()
                self.assertEqual(health.status, "degraded")
                self.assertIn(name + "_DATA_STALE", health.statusReasons)

    def test_background_loader_skips_ui_and_preserves_cache_when_reserved(self):
        with TemporaryDirectory() as directory:
            state = AppState.__new__(AppState)
            state.priority_lock = threading.Lock()
            state.priority_waiters = 0
            state.background_preflight_lock = threading.Lock()
            state.background_backoff = Mock()
            state.background_backoff.snapshot.return_value = {"seconds": 0}
            state.usage = self.limiter(directory)
            state.usage.acquire_background(5)
            state.charge_budget_reserve = Mock(return_value=5)
            state.reader = Mock()
            state.reader.action_pending = threading.Event()
            loader = Mock(return_value=VehicleData(soc=10))
            with patch("threading.Thread.start"):
                cache = BackgroundCache(
                    "details", state.background_loader(
                        loader, 1, priority=True, reserve_charge_budget=True,
                    ), lambda _: 900, VehicleData,
                )
            cache.set_value(VehicleData(soc=60))
            result = cache.refresh()
            loader.assert_not_called()
            self.assertEqual(result.soc, 60)
            self.assertTrue(result.stale)
            self.assertEqual(result.errorCategory, "BACKGROUND_BUDGET_RESERVED")
            self.assertEqual(state.usage.snapshot()["backgroundUsed"], 5)
            self.assertEqual(state.priority_waiters, 0)
            state.usage.acquire_background(1)

    def test_short_charge_buffer_leaves_room_for_trip_with_reported_morning_usage(self):
        with TemporaryDirectory() as directory:
            limiter = self.limiter(directory, limit=180)
            state = AppState.__new__(AppState)
            state.background_intervals = {"charge": (1, 900)}
            state.charge = SimpleNamespace(
                lock=threading.Lock(), value=VehicleData(status="A"), interval=lambda _: 900,
            )
            limiter.acquire_background(111)
            limiter.acquire_background(2, reserve=state.charge_budget_reserve)
            self.assertEqual(limiter.snapshot()["backgroundUsed"], 113)

    def test_trip_retry_policy_and_reserve_are_wired_into_app_state(self):
        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {
                "CACHE_STATE_DIR": str(Path(directory) / "cache"),
                "USAGE_STATE_FILE": str(Path(directory) / "usage.json"),
                "BACKGROUND_DAILY_LIMIT": "180",
                "BACKGROUND_MIN_INTERVAL_SECONDS": "0",
                "TRIP_POLL_ENABLED": "true",
                "TRIP_INTERVAL_SECONDS": "3600",
                "BACKGROUND_ERROR_RETRY_SECONDS": "900",
            }),
            patch("threading.Thread.start"),
            patch("vw_app_connector.VolkswagenReader") as reader_class,
            patch("vw_app_connector.MqttPublisher.from_environment", return_value=None),
        ):
            reader = reader_class.return_value
            reader_class.error_category.side_effect = VolkswagenReader.error_category
            reader.action_pending = threading.Event()
            state = AppState()
            for category in (
                "TRIP_NOT_UPDATED", "TRIP_NOT_AVAILABLE",
                "TRIP_ODOMETER_UNAVAILABLE", "TRIP_ODOMETER_REGRESSED",
                "VEHICLE_REPORT_NOT_READY",
            ):
                with self.subTest(category=category):
                    reader.read_latest_trip.side_effect = TransientEndpointState(category, "Waiting")
                    before = time.monotonic()
                    result = state.latest_trip.refresh()
                    self.assertEqual(result.errorCategory, category)
                    self.assertGreaterEqual(state.latest_trip.next_attempt_monotonic, before + 3600)
            # Early overview changes may queue work, but cannot bypass this retry.
            state.latest_trip.request_early_refresh()
            state.latest_trip.wakeup.wait = Mock(side_effect=RuntimeError("stop"))
            reader.read_latest_trip.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "stop"):
                state.latest_trip._worker()
            reader.read_latest_trip.assert_not_called()
            self.assertGreater(state.latest_trip.wakeup.wait.call_args.args[0], 3590)

            state.latest_trip.next_attempt_monotonic = 0
            state.latest_trip.early_refresh_requested = True
            before = state.usage.snapshot()["backgroundUsed"]
            self.assertEqual(state.details.refresh().errorCategory, "TRIP_REFRESH_PENDING")
            self.assertEqual(state.usage.snapshot()["backgroundUsed"], before)
            state.latest_trip.early_refresh_requested = False

            # Optional trip, details and location reads all respect the reserve.
            with state.usage.lock:
                state.usage.state["backgroundUsed"] = 175
                state.usage._save()
            state.charge_budget_reserve = Mock(return_value=6)
            state.usage.state["backgroundLastByName"] = {}
            for cache in (state.latest_trip, state.details, state.location):
                with self.subTest(cache=cache.name):
                    self.assertEqual(cache.refresh().errorCategory, "BACKGROUND_BUDGET_RESERVED")
            self.assertEqual(state.usage.snapshot()["backgroundUsed"], 175)

    def test_pacing_survives_restart_and_does_not_consume_budget_when_deferred(self):
        with TemporaryDirectory() as directory:
            limiter = self.limiter(directory, limit=180)
            limiter.acquire_background(111)
            schedule = lambda: {
                "charge": (1, 900), "latest-trip": (2, 3600),
                "details": (3, 43200), "location": (1, 14400),
            }
            limiter.acquire_background(2, budget_name="latest-trip", schedule=schedule)
            restored = self.limiter(directory, limit=180)
            with self.assertRaises(TransientEndpointState) as caught:
                restored.acquire_background(2, budget_name="latest-trip", schedule=schedule)
            self.assertEqual(caught.exception.reason, "BACKGROUND_BUDGET_PACED")
            self.assertGreater(caught.exception.retry_after_seconds, 0)
            self.assertEqual(restored.snapshot()["backgroundUsed"], 113)

    def test_pacing_retains_ui_failure_and_early_request_until_success(self):
        loader = Mock(side_effect=[
            TransientEndpointState("VEHICLE_REPORT_NOT_READY", "Empty report"),
            TransientEndpointState("BACKGROUND_BUDGET_PACED", "Wait", retry_after_seconds=123),
            VehicleData(soc=50),
        ])
        cache = BackgroundCache(
            "details", loader, lambda _: 900, VehicleData, auto_refresh=False,
        )
        cache.set_value(VehicleData(soc=60))
        cache.refresh()
        cache.request_early_refresh()
        before = time.monotonic()
        deferred = cache.refresh()
        self.assertEqual(deferred.soc, 60)
        self.assertEqual(cache.last_read_error_category, "VEHICLE_REPORT_NOT_READY")
        self.assertEqual(cache.last_error_category, "BACKGROUND_BUDGET_PACED")
        self.assertGreaterEqual(cache.next_attempt_monotonic, before + 123)
        self.assertTrue(cache.early_refresh_requested)
        cache.refresh()
        self.assertEqual(cache.last_read_error_category, "")
        self.assertEqual(cache.last_error_category, "")
        self.assertFalse(cache.early_refresh_requested)


if __name__ == "__main__":
    unittest.main()
