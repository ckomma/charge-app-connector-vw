#!/usr/bin/env python3
"""Read Volkswagen app vehicle data through ADB UI automation.

Modified from janphkre/charge-app-connector for the Volkswagen Android app.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Generic, TypeVar
from urllib.parse import parse_qs, urlparse


LOG = logging.getLogger("vw-app-connector")
T = TypeVar("T")


class ActionPriority(RuntimeError):
    pass


class UsageLimit(RuntimeError):
    pass


class UsageLimiter:
    def __init__(self) -> None:
        self.path = Path(
            os.getenv(
                "USAGE_STATE_FILE", "/var/lib/vw-app-connector/usage.json"
            )
        )
        self.background_daily_limit = int(
            os.getenv("BACKGROUND_DAILY_LIMIT", "180")
        )
        self.action_daily_limit = int(os.getenv("ACTION_DAILY_LIMIT", "20"))
        self.background_min_interval = float(
            os.getenv("BACKGROUND_MIN_INTERVAL_SECONDS", "300")
        )
        self.action_min_interval = float(
            os.getenv("ACTION_MIN_INTERVAL_SECONDS", "60")
        )
        self.rate_limit_cooldown = float(
            os.getenv("RATE_LIMIT_COOLDOWN_SECONDS", "43200")
        )
        self.lock = threading.Lock()
        self.state = self._load()

    @staticmethod
    def today() -> str:
        return datetime.now().astimezone().date().isoformat()

    def _empty(self) -> dict[str, object]:
        return {
            "day": self.today(),
            "backgroundUsed": 0,
            "actionsUsed": 0,
            "lastBackgroundAt": 0.0,
            "lastActionAt": 0.0,
            "cooldownUntil": 0.0,
        }

    def _load(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if value.get("day") == self.today():
                return value
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return self._empty()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state), encoding="utf-8")
        temporary.replace(self.path)

    def _rollover(self) -> None:
        if self.state.get("day") != self.today():
            self.state = self._empty()

    def acquire_background(self, cost: int) -> None:
        while True:
            with self.lock:
                self._rollover()
                now = time.time()
                cooldown = float(self.state["cooldownUntil"])
                used = int(self.state["backgroundUsed"])
                if now < cooldown:
                    raise UsageLimit(
                        f"Volkswagen rate-limit cooldown active for "
                        f"{round(cooldown - now)} seconds"
                    )
                if used + cost > self.background_daily_limit:
                    raise UsageLimit(
                        "Volkswagen background daily budget exhausted"
                    )
                wait = self.background_min_interval - (
                    now - float(self.state["lastBackgroundAt"])
                )
                if wait <= 0:
                    self.state["backgroundUsed"] = used + cost
                    self.state["lastBackgroundAt"] = now
                    self._save()
                    return
            time.sleep(min(wait, 5))

    def acquire_action(self, cost: int = 1) -> None:
        while True:
            with self.lock:
                self._rollover()
                now = time.time()
                cooldown = float(self.state["cooldownUntil"])
                used = int(self.state["actionsUsed"])
                if now < cooldown:
                    raise UsageLimit(
                        f"Volkswagen rate-limit cooldown active for "
                        f"{round(cooldown - now)} seconds"
                    )
                if used + cost > self.action_daily_limit:
                    raise UsageLimit("Volkswagen action daily budget exhausted")
                wait = self.action_min_interval - (
                    now - float(self.state["lastActionAt"])
                )
                if wait <= 0:
                    self.state["actionsUsed"] = used + cost
                    self.state["lastActionAt"] = now
                    self._save()
                    return
            time.sleep(min(wait, 2))

    def record_rate_limit(self) -> None:
        with self.lock:
            self._rollover()
            self.state["cooldownUntil"] = time.time() + self.rate_limit_cooldown
            self._save()

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            self._rollover()
            now = time.time()
            return {
                "backgroundUsed": int(self.state["backgroundUsed"]),
                "backgroundLimit": self.background_daily_limit,
                "actionsUsed": int(self.state["actionsUsed"]),
                "actionsLimit": self.action_daily_limit,
                "cooldownSeconds": max(
                    0, round(float(self.state["cooldownUntil"]) - now)
                ),
            }


@dataclass
class VehicleData:
    status: str = "A"
    soc: int | None = None
    range: int | None = None
    remainingChargeMinutes: int | None = None
    chargeRateKmH: int | None = None
    chargePowerKw: int | None = None
    targetSoc: int | None = None
    chargingMode: str = ""
    climater: bool | None = None
    locked: bool | None = None
    syncAgeMinutes: int | None = None
    observedAt: str = ""
    error: str = ""
    errorCategory: str = ""
    stale: bool = False
    lastSuccessfulAt: str = ""
    refreshDurationSeconds: float | None = None


@dataclass
class LocationData:
    address: str = ""
    parkedDuration: str = ""
    latitude: float | None = None
    longitude: float | None = None
    observedAt: str = ""
    error: str = ""
    errorCategory: str = ""
    stale: bool = False
    lastSuccessfulAt: str = ""
    refreshDurationSeconds: float | None = None


@dataclass
class DetailData:
    targetTemperatureC: float | None = None
    automaticWindowHeating: bool | None = None
    climateZoneFrontLeft: bool | None = None
    climateZoneFrontRight: bool | None = None
    odometerKm: int | None = None
    serviceDays: int | None = None
    warningStatus: str = ""
    reportSyncAge: str = ""
    departureTimes: list[dict[str, object]] | None = None
    observedAt: str = ""
    error: str = ""
    errorCategory: str = ""
    stale: bool = False
    lastSuccessfulAt: str = ""
    refreshDurationSeconds: float | None = None


@dataclass
class HealthData:
    status: str = "ok"
    adbState: str = ""
    adbMode: str = ""
    adbTransport: str = ""
    adbWifiConfigured: bool = False
    adbLastConnectError: str = ""
    appVersion: str = ""
    phoneBatteryLevel: int | None = None
    phoneBatteryTemperatureC: float | None = None
    phoneBatteryStatus: str = ""
    phoneUsbPowered: bool | None = None
    phonePowered: bool | None = None
    phonePowerSource: str = ""
    chargeLastSuccessfulAt: str = ""
    chargeAgeSeconds: int | None = None
    chargeRefreshing: bool = False
    detailLastSuccessfulAt: str = ""
    detailAgeSeconds: int | None = None
    locationLastSuccessfulAt: str = ""
    locationAgeSeconds: int | None = None
    usageBackgroundUsed: int = 0
    usageBackgroundLimit: int = 0
    usageActionsUsed: int = 0
    usageActionsLimit: int = 0
    usageCooldownSeconds: int = 0


class VolkswagenReader:
    def __init__(self) -> None:
        self.usb_serial = required_env("ADB_SERIAL")
        self.adb_mode = os.getenv("ADB_MODE", "usb").casefold()
        if self.adb_mode not in ("usb", "wifi", "auto"):
            raise RuntimeError("ADB_MODE must be usb, wifi or auto")
        self.wifi_address = os.getenv("ADB_WIFI_ADDRESS", "").strip()
        if self.adb_mode == "wifi" and not self.wifi_address:
            raise RuntimeError("ADB_WIFI_ADDRESS is required for ADB_MODE=wifi")
        self.serial = self.usb_serial
        self.adb_transport = "usb"
        self.adb_last_connect_error = ""
        self.adb_connection_lock = threading.Lock()
        self.package = os.getenv("APP_PACKAGE", "com.volkswagen.weconnect")
        self.start_wait = float(os.getenv("APP_START_WAIT_SECONDS", "8"))
        self.detail_wait = float(os.getenv("DETAIL_WAIT_SECONDS", "3"))
        self.spin = os.getenv("VW_SPIN", "")
        self.sleep_after_operation = (
            os.getenv("SLEEP_AFTER_OPERATION", "true").casefold() == "true"
        )
        self.diagnostics_dir = Path(
            os.getenv(
                "DIAGNOSTICS_DIR", "/var/lib/vw-app-connector/diagnostics"
            )
        )
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        self.operation_lock = threading.RLock()
        self.action_pending = threading.Event()
        self.context = threading.local()

    @staticmethod
    def run_adb(*args: str, timeout: float = 20) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["adb", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    @classmethod
    def adb_state(cls, serial: str) -> str:
        result = cls.run_adb("-s", serial, "get-state", timeout=5)
        return result.stdout.strip() if result.returncode == 0 else ""

    def connect_wifi(self) -> bool:
        if not self.wifi_address:
            return False
        result = self.run_adb("connect", self.wifi_address, timeout=10)
        message = (result.stdout or result.stderr).strip()
        if result.returncode == 0 and self.adb_state(self.wifi_address) == "device":
            self.adb_last_connect_error = ""
            return True
        self.adb_last_connect_error = message or "ADB Wi-Fi connection failed"
        return False

    def select_serial(self) -> str:
        with self.adb_connection_lock:
            if self.adb_mode in ("usb", "auto"):
                if self.adb_state(self.usb_serial) == "device":
                    self.serial = self.usb_serial
                    self.adb_transport = "usb"
                    self.adb_last_connect_error = ""
                    return self.serial
                if self.adb_mode == "usb":
                    self.serial = self.usb_serial
                    self.adb_transport = "usb"
                    return self.serial

            if self.adb_mode in ("wifi", "auto") and self.wifi_address:
                if self.adb_state(self.wifi_address) == "device" or self.connect_wifi():
                    self.serial = self.wifi_address
                    self.adb_transport = "wifi"
                    self.adb_last_connect_error = ""
                    return self.serial

            if self.adb_mode == "auto":
                self.serial = self.usb_serial
                self.adb_transport = "unavailable"
                raise RuntimeError(
                    "Neither USB nor configured ADB Wi-Fi connection is available"
                )
            self.serial = self.wifi_address
            self.adb_transport = "wifi"
            raise RuntimeError(
                self.adb_last_connect_error or "ADB Wi-Fi connection is unavailable"
            )

    def adb(self, *args: str, timeout: float = 20) -> str:
        serial = self.select_serial()
        result = subprocess.run(
            ["adb", "-s", serial, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode:
            message = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"ADB failed ({result.returncode}): {message}")
        return result.stdout

    def shell(self, *args: str, timeout: float = 20) -> str:
        return self.adb("shell", *args, timeout=timeout)

    def adb_bytes(self, *args: str, timeout: float = 20) -> bytes:
        serial = self.select_serial()
        result = subprocess.run(
            ["adb", "-s", serial, *args],
            check=False,
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode:
            message = (result.stderr or result.stdout).decode(
                errors="replace"
            ).strip()
            raise RuntimeError(f"ADB failed ({result.returncode}): {message}")
        return result.stdout

    def dump_ui(self, remote_name: str) -> ET.Element:
        remote_path = f"/sdcard/{remote_name}"
        self.shell("uiautomator", "dump", remote_path, timeout=30)
        return ET.fromstring(self.shell("cat", remote_path, timeout=10))

    def save_diagnostics(self, category: str, error: Exception) -> None:
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        stem = self.diagnostics_dir / f"{stamp}-{category.casefold()}"
        try:
            self.shell("uiautomator", "dump", "/sdcard/vw-error.xml", timeout=30)
            xml = self.shell("cat", "/sdcard/vw-error.xml", timeout=10)
            stem.with_suffix(".xml").write_text(xml, encoding="utf-8")
            stem.with_suffix(".png").write_bytes(
                self.adb_bytes("exec-out", "screencap", "-p", timeout=20)
            )
            stem.with_suffix(".txt").write_text(
                f"{type(error).__name__}: {error}\n", encoding="utf-8"
            )
            files = sorted(self.diagnostics_dir.glob("*"), key=lambda p: p.stat().st_mtime)
            for old in files[:-60]:
                old.unlink(missing_ok=True)
        except Exception:
            LOG.exception("Could not save diagnostics")

    def open_overview(self) -> ET.Element:
        for attempt in range(4):
            overview = self.dump_ui("vw-overview.xml")
            overview_text = "\n".join(self.strings(overview)).casefold()
            if "too many requests" in overview_text or "zu viele anfragen" in overview_text:
                raise UsageLimit("Volkswagen app reports too many requests")
            try:
                self.range_tile_center(overview)
                return overview
            except RuntimeError:
                if attempt == 3:
                    raise
                self.shell("input", "keyevent", "KEYCODE_BACK")
                time.sleep(2)
        raise RuntimeError("Volkswagen overview not found")

    @staticmethod
    def strings(root: ET.Element) -> list[str]:
        values: list[str] = []
        for node in root.iter():
            for key in ("text", "content-desc"):
                value = node.attrib.get(key, "").strip()
                if value and value not in values:
                    values.append(value)
        return values

    @staticmethod
    def node_center(node: ET.Element) -> tuple[int, int] | None:
        match = re.fullmatch(
            r"\[(\d+),(\d+)]\[(\d+),(\d+)]", node.attrib.get("bounds", "")
        )
        if not match:
            return None
        left, top, right, bottom = map(int, match.groups())
        return ((left + right) // 2, (top + bottom) // 2)

    @classmethod
    def described_node_center(cls, root: ET.Element, prefix: str) -> tuple[int, int]:
        return cls.described_node_center_any(root, (prefix,))

    @classmethod
    def described_node_center_any(
        cls, root: ET.Element, prefixes: tuple[str, ...]
    ) -> tuple[int, int]:
        for node in root.iter():
            description = node.attrib.get("content-desc", "")
            text = node.attrib.get("text", "")
            if not any(
                prefix.casefold() in description.casefold()
                or prefix.casefold() in text.casefold()
                for prefix in prefixes
            ):
                continue
            center = cls.node_center(node)
            if center:
                return center
        raise RuntimeError(
            f"Volkswagen UI element not found: {' / '.join(prefixes)}"
        )

    @classmethod
    def range_tile_center(cls, root: ET.Element) -> tuple[int, int]:
        return cls.described_node_center_any(
            root, ("Batteriereichweite:", "Battery range:", "Electric range:")
        )

    @staticmethod
    def parse_locked(text: str) -> bool | None:
        lowered = text.casefold()
        if "entriegelt" in lowered or "unlocked" in lowered or "unlocking" in lowered:
            return False
        if "verriegelt" in lowered or "locked" in lowered or "locking" in lowered:
            return True
        return None

    @staticmethod
    def parse_sync_age(text: str) -> int | None:
        match = re.search(
            r"(?:Synchronisiert vor|Synced|Synchronised|Updated)\s*"
            r"(?:(\d+)\s*(?:Stunden?|hours?)\s*)?"
            r"(?:(\d+)\s*(?:Minuten?|minutes?))?(?:\s*ago)?",
            text,
            re.IGNORECASE,
        )
        if match and any(match.groups()):
            return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
        if re.search(
            r"Gerade (?:eben )?synchronisiert|Just synced|Synced just now|"
            r"Synchronised just now|Just updated",
            text,
            re.IGNORECASE,
        ):
            return 0
        return None

    @staticmethod
    def parse_climater(text: str) -> bool | None:
        if not re.search(
            r"Vorklimatisierung|Air conditioning|Climate control",
            text,
            re.IGNORECASE,
        ):
            return None
        return not bool(
            re.search(
                r"(?:Vorklimatisierung|Air conditioning|Climate control)"
                r"[.\s:]*(?:Aus|Off)\b",
                text,
                re.IGNORECASE,
            )
        )

    @staticmethod
    def parse_soc(text: str) -> int | None:
        match = re.search(
            r"(?:Batterie(?:ladung)?|Battery(?: charge level| charge| level)?|"
            r"State of charge):?\s*(\d+)\s*(?:%|Prozent|per cent|percent)",
            text,
            re.IGNORECASE,
        )
        return int(match.group(1)) if match else None

    @staticmethod
    def parse_target_temperature(root: ET.Element) -> float:
        candidates: list[tuple[int, float]] = []
        for node in root.iter():
            value = node.attrib.get("text", "").strip()
            if not re.fullmatch(r"\d{2}(?:[.,]\d)?", value):
                continue
            bounds = re.fullmatch(
                r"\[(\d+),(\d+)]\[(\d+),(\d+)]", node.attrib.get("bounds", "")
            )
            width = int(bounds.group(3)) - int(bounds.group(1)) if bounds else 0
            candidates.append((width, float(value.replace(",", "."))))
        if not candidates:
            raise RuntimeError("Volkswagen target temperature not found")
        return max(candidates)[1]

    @staticmethod
    def parse_charging_details(text: str, result: VehicleData) -> None:
        details_match = re.search(
            r"(\d+)\s*(?:Stunden?|hours?)\s+(?:und\.?|and\.?)\s*"
            r"(\d+)\s*(?:Minuten?|minutes?)"
            r".*?(?:Ladegeschwindigkeit|Charging speed):\s*(\d+)\s*"
            r"(?:Kilometer pro Stunde|kilometres? per hour|km/h)"
            r".*?(?:Ladeleistung|Charging power|Charging capacity):\s*(\d+)\s*"
            r"(?:Kilowatt|kW)"
            r".*?(?:Zielladestand|Target charge level|Target charge):\s*(\d+)\s*"
            r"(?:Prozent|per cent|percent|%)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if details_match:
            hours, minutes, rate, power, target = map(int, details_match.groups())
            result.remainingChargeMinutes = hours * 60 + minutes
            result.chargeRateKmH = rate
            result.chargePowerKw = power
            result.targetSoc = target

        target_match = re.search(
            r"(?:Zielladestand|Target charge level|Target charge):?\s*(\d+)\s*"
            r"(?:Prozent|per cent|percent|%)",
            text,
            re.IGNORECASE,
        )
        if target_match:
            result.targetSoc = int(target_match.group(1))

        mode_match = re.search(
            r"(?:Ladeverfahren|Charging mode|Charging type|Charging method)\.\s*"
            r"([^.]+)\.\s*"
            r"(?:Ladeverfahren ändern|Change charging mode|Change charging type|"
            r"Change charging method)",
            text,
            re.IGNORECASE,
        )
        if mode_match:
            result.chargingMode = mode_match.group(1).strip()

    @staticmethod
    def parse_navigation_coordinates(text: str) -> tuple[float, float]:
        match = re.search(
            r"google\.navigation:q=(-?\d+(?:\.\d+)?)(?:%2C|,)"
            r"(-?\d+(?:\.\d+)?)",
            text,
            re.IGNORECASE,
        )
        if not match:
            raise RuntimeError("Volkswagen navigation coordinates not found")
        return (float(match.group(1)), float(match.group(2)))

    def launch(self) -> None:
        self.shell("am", "force-stop", self.package)
        self.shell(
            "monkey",
            "-p",
            self.package,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
            timeout=15,
        )
        time.sleep(self.start_wait)
        if (
            getattr(self.context, "background", False)
            and self.action_pending.is_set()
        ):
            raise ActionPriority("Background refresh preempted by action")

    def wake_screen(self) -> None:
        self.shell("input", "keyevent", "KEYCODE_WAKEUP")
        time.sleep(0.5)
        self.shell("wm", "dismiss-keyguard")
        self.shell("input", "keyevent", "82")
        time.sleep(1)

    def sleep_screen(self) -> None:
        if self.sleep_after_operation:
            self.shell("input", "keyevent", "KEYCODE_SLEEP")

    @contextmanager
    def screen_session(self):
        with self.operation_lock:
            self.wake_screen()
            try:
                yield
            finally:
                self.sleep_screen()

    def read(self) -> VehicleData:
        with self.screen_session():
            return self.with_retries(self._read, "CHARGE")

    def with_retries(self, operation: Callable[[], T], category: str) -> T:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return operation()
            except Exception as exc:
                if isinstance(exc, ActionPriority):
                    raise
                last_error = exc
                self.save_diagnostics(category, exc)
                LOG.warning("%s attempt %d failed: %s", category, attempt + 1, exc)
                if attempt == 0:
                    try:
                        self.launch()
                    except Exception:
                        LOG.exception("Recovery launch failed")
        assert last_error is not None
        raise last_error

    @staticmethod
    def error_category(exc: Exception) -> str:
        if isinstance(exc, UsageLimit):
            return "RATE_LIMIT"
        message = str(exc).casefold()
        if "too many requests" in message or "zu viele anfragen" in message:
            return "RATE_LIMIT"
        if "adb failed" in message:
            return "ADB"
        if "not found" in message or "parse" in message:
            return "UI_PARSE"
        if "timed out" in message or isinstance(exc, TimeoutError):
            return "TIMEOUT"
        return "APP"

    def phone_health(self) -> HealthData:
        health = HealthData(
            adbMode=self.adb_mode,
            adbTransport=self.adb_transport,
            adbWifiConfigured=bool(self.wifi_address),
        )
        try:
            health.adbState = self.adb("get-state", timeout=5).strip()
            health.adbTransport = self.adb_transport
            health.adbLastConnectError = self.adb_last_connect_error
            battery = self.shell("dumpsys", "battery", timeout=8)
            values: dict[str, str] = {}
            for line in battery.splitlines():
                if ":" not in line:
                    continue
                key, value = line.strip().split(":", 1)
                values[key.strip()] = value.strip()
            health.phoneBatteryLevel = int(values["level"])
            health.phoneBatteryTemperatureC = int(values["temperature"]) / 10
            health.phoneUsbPowered = values.get("USB powered", "false") == "true"
            power_sources = [
                name
                for name, key in (
                    ("AC", "AC powered"),
                    ("USB", "USB powered"),
                    ("wireless", "Wireless powered"),
                )
                if values.get(key, "false") == "true"
            ]
            health.phonePowered = bool(power_sources)
            health.phonePowerSource = ", ".join(power_sources)
            health.phoneBatteryStatus = {
                "2": "charging",
                "3": "discharging",
                "4": "not_charging",
                "5": "full",
            }.get(values.get("status", ""), values.get("status", "unknown"))
            package = self.shell("dumpsys", "package", self.package, timeout=8)
            match = re.search(r"versionName=([^\s]+)", package)
            health.appVersion = match.group(1) if match else ""
        except Exception as exc:
            health.status = "error"
            health.adbState = str(exc)
            health.adbTransport = self.adb_transport
            health.adbLastConnectError = self.adb_last_connect_error
        return health

    def _read(self) -> VehicleData:
        self.launch()
        overview = self.open_overview()
        overview_text = "\n".join(self.strings(overview))
        result = VehicleData(
            observedAt=datetime.now().astimezone().isoformat(timespec="seconds")
        )

        range_match = re.search(
            r"(?:Batteriereichweite|Battery range|Electric range):\s*(\d+)\s*"
            r"(?:Kilometer|kilometres?|km)",
            overview_text,
            re.IGNORECASE,
        )
        if range_match:
            result.range = int(range_match.group(1))

        result.syncAgeMinutes = self.parse_sync_age(overview_text)
        result.climater = self.parse_climater(overview_text)
        result.locked = self.parse_locked(overview_text)

        x, y = self.range_tile_center(overview)
        self.shell("input", "tap", str(x), str(y))
        time.sleep(self.detail_wait)

        detail_text = "\n".join(self.strings(self.dump_ui("vw-detail.xml")))
        result.soc = self.parse_soc(detail_text)
        if result.soc is None:
            raise RuntimeError("Volkswagen state of charge not found")
        self.parse_charging_details(detail_text, result)

        lowered = detail_text.casefold()
        if any(
            value in lowered
            for value in (
                "laden stoppen",
                "wird geladen",
                "lädt",
                "stop charging",
                "is charging",
                "charging in progress",
            )
        ):
            result.status = "C"
        elif any(
            value in lowered
            for value in (
                "laden starten",
                "ladestation zeigt den aktuellen status",
                "ladekabel verbunden",
                "start charging",
                "charging station shows the current status",
                "charging cable connected",
            )
        ):
            result.status = "B"

        return result

    def set_locked(self, desired: bool) -> VehicleData:
        with self.screen_session():
            if not self.spin:
                raise RuntimeError("VW_SPIN is not configured")
            self.launch()
            overview = self.open_overview()
            current_text = "\n".join(self.strings(overview))
            current = self.parse_locked(current_text)
            if current is desired:
                return self.with_retries(self._read, "ACTION_VERIFY")

            x, y = self.described_node_center_any(
                overview, ("Fahrzeug.", "Vehicle.")
            )
            self.shell("input", "tap", str(x), str(y))
            time.sleep(self.detail_wait)
            if desired:
                self.shell("input", "swipe", "540", "2050", "540", "1530", "900")
            else:
                self.shell("input", "swipe", "540", "1530", "540", "2050", "900")
            time.sleep(2)

            pin_text = "\n".join(self.strings(self.dump_ui("vw-pin.xml")))
            if not re.search(r"S-?PIN", pin_text, re.IGNORECASE):
                raise RuntimeError("Volkswagen S-PIN dialog not found")
            self.shell("input", "tap", "260", "1020")
            self.shell("input", "text", self.spin)
            time.sleep(8)
            return self.with_retries(self._read, "ACTION_VERIFY")

    def read_location(self) -> LocationData:
        with self.screen_session():
            self.launch()
            root = self.dump_ui("vw-location-start.xml")
            x, y = self.described_node_center(root, "Navigation Tab")
            self.shell("input", "tap", str(x), str(y))
            time.sleep(self.detail_wait)

            map_root = self.dump_ui("vw-location-map.xml")
            x, y = self.described_node_center(map_root, "Car Locate Button")
            self.shell("input", "tap", str(x), str(y))
            time.sleep(self.detail_wait)

            # Car Locate centers the vehicle badge at the stable map viewport center.
            self.shell("input", "tap", "540", "786")
            time.sleep(self.detail_wait)
            details = self.dump_ui("vw-location-details.xml")
            result = LocationData(
                observedAt=datetime.now().astimezone().isoformat(timespec="seconds")
            )
            for value in self.strings(details):
                if not re.search(
                    r"\n(?:Geparkt seit|Parked since|Parked for)\s+",
                    value,
                    re.IGNORECASE,
                ):
                    continue
                result.address, result.parkedDuration = value.split("\n", 1)
                break
            if not result.address:
                raise RuntimeError("Volkswagen vehicle address not found")

            x, y = self.described_node_center(details, "Route")
            self.shell("input", "tap", str(x), str(y))
            time.sleep(self.detail_wait)
            activity = self.shell("dumpsys", "activity", "activities", timeout=20)
            result.latitude, result.longitude = self.parse_navigation_coordinates(
                activity
            )
            for _ in range(3):
                focus = self.shell("dumpsys", "window", "windows", timeout=20)
                focus_match = re.search(r"mCurrentFocus=([^\n]+)", focus)
                if focus_match and self.package in focus_match.group(1):
                    break
                self.shell("input", "keyevent", "KEYCODE_BACK")
                time.sleep(1)
            return result

    def set_charging(self, desired: bool) -> VehicleData:
        with self.screen_session():
            self.launch()
            overview = self.open_overview()
            x, y = self.range_tile_center(overview)
            self.shell("input", "tap", str(x), str(y))
            time.sleep(self.detail_wait)
            detail = self.dump_ui("vw-charge-action.xml")
            text = "\n".join(self.strings(detail))
            current = bool(
                re.search(
                    r"Laden stoppen|Wird geladen|Stop charging|Is charging",
                    text,
                    re.IGNORECASE,
                )
            )
            if current != desired:
                labels = (
                    ("Laden starten", "Start charging")
                    if desired
                    else ("Laden stoppen", "Stop charging")
                )
                x, y = self.described_node_center_any(detail, labels)
                self.shell("input", "tap", str(x), str(y))
                time.sleep(8)
            return self.with_retries(self._read, "ACTION_VERIFY")

    def set_climater(self, desired: bool) -> VehicleData:
        with self.screen_session():
            self.launch()
            overview = self.open_overview()
            overview_text = "\n".join(self.strings(overview))
            current = self.parse_climater(overview_text)
            if current != desired:
                x, y = self.described_node_center_any(
                    overview,
                    ("Vorklimatisierung.", "Air conditioning.", "Climate control."),
                )
                self.shell("input", "tap", str(x), str(y))
                time.sleep(self.detail_wait)
                detail = self.dump_ui("vw-climate-action.xml")
                labels = ("Starten", "Start") if desired else ("Stoppen", "Stop")
                x, y = self.described_node_center_any(detail, labels)
                self.shell("input", "tap", str(x), str(y))
                time.sleep(8)
            return self.with_retries(self._read, "ACTION_VERIFY")

    @staticmethod
    def checked_nodes(root: ET.Element) -> list[ET.Element]:
        nodes = [
            node
            for node in root.iter()
            if node.attrib.get("checkable") == "true"
            and node.attrib.get("clickable") == "true"
        ]
        unique: dict[str, ET.Element] = {}
        for node in nodes:
            unique[node.attrib.get("bounds", "")] = node
        return sorted(
            unique.values(),
            key=lambda node: VolkswagenReader.node_center(node) or (0, 0),
        )

    def open_climate(self) -> ET.Element:
        overview = self.open_overview()
        x, y = self.described_node_center_any(
            overview,
            ("Vorklimatisierung.", "Air conditioning.", "Climate control."),
        )
        self.shell("input", "tap", str(x), str(y))
        time.sleep(self.detail_wait)
        return self.dump_ui("vw-climate.xml")

    def _read_details(self) -> DetailData:
        result = DetailData(
            departureTimes=[],
            observedAt=datetime.now().astimezone().isoformat(timespec="seconds"),
        )

        self.launch()
        climate = self.open_climate()
        result.targetTemperatureC = self.parse_target_temperature(climate)
        x, y = self.described_node_center_any(climate, ("Einstellungen", "Settings"))
        self.shell("input", "tap", str(x), str(y))
        time.sleep(self.detail_wait)
        settings = self.dump_ui("vw-climate-settings.xml")
        switches = self.checked_nodes(settings)
        if len(switches) >= 2:
            result.automaticWindowHeating = switches[1].attrib.get("checked") == "true"
        x, y = self.described_node_center_any(settings, ("Zonen", "Zones"))
        self.shell("input", "tap", str(x), str(y))
        time.sleep(self.detail_wait)
        zones = self.checked_nodes(self.dump_ui("vw-climate-zones.xml"))
        if len(zones) >= 2:
            result.climateZoneFrontLeft = zones[0].attrib.get("checked") == "true"
            result.climateZoneFrontRight = zones[1].attrib.get("checked") == "true"

        self.launch()
        overview = self.open_overview()
        x, y = self.described_node_center_any(
            overview,
            (
                "Fahrzeugzustandsbericht.",
                "Vehicle health report.",
                "Vehicle status report.",
            ),
        )
        self.shell("input", "tap", str(x), str(y))
        time.sleep(self.detail_wait)
        report_text = "\n".join(self.strings(self.dump_ui("vw-report.xml")))
        odometer = re.search(
            r"(?:Gesamtstrecke|Total distance|Odometer)\s*([\d.,]+)\s*km",
            report_text,
            re.IGNORECASE,
        )
        service = re.search(
            r"(?:Nächster Service|Next service)\s*(?:in\s*)?(\d+)\s*"
            r"(?:Tage|days)",
            report_text,
            re.IGNORECASE,
        )
        report_sync = re.search(
            r"(?:Synchronisiert|Synced):\s*([^\n]+)",
            report_text,
            re.IGNORECASE,
        )
        result.odometerKm = (
            int(re.sub(r"[.,]", "", odometer.group(1))) if odometer else None
        )
        result.serviceDays = int(service.group(1)) if service else None
        result.warningStatus = (
            "Keine Meldungen"
            if re.search(r"Keine Meldungen|No messages|No warnings", report_text, re.IGNORECASE)
            else "Meldungen vorhanden"
        )
        result.reportSyncAge = report_sync.group(1).strip() if report_sync else ""

        self.launch()
        overview = self.open_overview()
        x, y = self.described_node_center_any(
            overview, ("Abfahrtszeiten.", "Departure times.")
        )
        self.shell("input", "tap", str(x), str(y))
        time.sleep(self.detail_wait)
        departure = self.dump_ui("vw-departures.xml")
        departure_values = self.strings(departure)
        for index, value in enumerate(departure_values):
            if not re.fullmatch(r"\d{2}:\d{2}", value):
                continue
            day = departure_values[index + 1] if index + 1 < len(departure_values) else ""
            result.departureTimes.append({"time": value, "day": day})
        return result

    def read_details(self) -> DetailData:
        with self.screen_session():
            return self.with_retries(self._read_details, "DETAILS")

    def set_target_temperature(self, desired: float) -> float:
        desired = round(desired * 2) / 2
        if desired < 16 or desired > 30:
            raise ValueError("Target temperature must be between 16 and 30 °C")
        with self.screen_session():
            self.launch()
            climate = self.open_climate()
            current = self.parse_target_temperature(climate)
            x = 984 if desired > current else 70
            for _ in range(int(round(abs(desired - current) * 2))):
                self.shell("input", "tap", str(x), "1100")
                time.sleep(0.4)
            time.sleep(4)
            verify = self.parse_target_temperature(
                self.dump_ui("vw-climate-verify.xml")
            )
            if verify != desired:
                raise RuntimeError("Volkswagen target temperature verification failed")
            return desired

    def set_climate_option(self, option: str, desired: bool) -> bool:
        option_index = {
            "automatic-window-heating": ("settings", 1),
            "zone-front-left": ("zones", 0),
            "zone-front-right": ("zones", 1),
        }
        if option not in option_index:
            raise KeyError(option)
        page, index = option_index[option]
        with self.screen_session():
            self.launch()
            climate = self.open_climate()
            x, y = self.described_node_center_any(
                climate, ("Einstellungen", "Settings")
            )
            self.shell("input", "tap", str(x), str(y))
            time.sleep(self.detail_wait)
            root = self.dump_ui("vw-option-settings.xml")
            if page == "zones":
                x, y = self.described_node_center_any(root, ("Zonen", "Zones"))
                self.shell("input", "tap", str(x), str(y))
                time.sleep(self.detail_wait)
                root = self.dump_ui("vw-option-zones.xml")
            switches = self.checked_nodes(root)
            if index >= len(switches):
                raise RuntimeError(f"Volkswagen climate option not found: {option}")
            current = switches[index].attrib.get("checked") == "true"
            if current != desired:
                center = self.node_center(switches[index])
                assert center is not None
                self.shell("input", "tap", str(center[0]), str(center[1]))
                time.sleep(5)
                verify = self.checked_nodes(self.dump_ui("vw-option-verify.xml"))
                if (verify[index].attrib.get("checked") == "true") != desired:
                    raise RuntimeError(f"Volkswagen climate option verification failed: {option}")
            return desired


class BackgroundCache(Generic[T]):
    def __init__(
        self,
        name: str,
        loader: Callable[[], T],
        interval: Callable[[T | None], float],
        empty_factory: Callable[[], T],
        initial_delay: float = 0,
    ) -> None:
        self.name = name
        self.loader = loader
        self.interval = interval
        self.empty_factory = empty_factory
        self.lock = threading.Lock()
        self.value: T | None = None
        self.last_success_monotonic = 0.0
        self.last_success_at = ""
        self.last_error = ""
        self.last_error_category = ""
        self.refreshing = False
        self.wakeup = threading.Event()
        self.initial_delay = initial_delay
        threading.Thread(target=self._worker, name=f"{name}-refresh", daemon=True).start()

    def _worker(self) -> None:
        if self.initial_delay:
            time.sleep(self.initial_delay)
            self.wakeup.clear()
        while True:
            if self.value is None or self.age() >= self.interval(self.value):
                self.refresh()
            delay = max(5.0, self.interval(self.value) - self.age())
            self.wakeup.wait(delay)
            self.wakeup.clear()

    def age(self) -> float:
        if not self.last_success_monotonic:
            return float("inf")
        return time.monotonic() - self.last_success_monotonic

    def trigger(self) -> None:
        self.wakeup.set()

    def refresh(self) -> T:
        with self.lock:
            if self.refreshing:
                return self.value or self.empty_factory()
            self.refreshing = True
        started = time.monotonic()
        try:
            value = self.loader()
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            setattr(value, "error", "")
            setattr(value, "errorCategory", "")
            setattr(value, "stale", False)
            setattr(value, "lastSuccessfulAt", now)
            setattr(value, "refreshDurationSeconds", round(time.monotonic() - started, 2))
            with self.lock:
                self.value = value
                self.last_success_monotonic = time.monotonic()
                self.last_success_at = now
                self.last_error = ""
                self.last_error_category = ""
                return self.value
        except ActionPriority:
            LOG.info("%s refresh yielded to a pending action", self.name)
            self.trigger()
            with self.lock:
                return self.value or self.empty_factory()
        except Exception as exc:
            category = VolkswagenReader.error_category(exc)
            LOG.exception("%s refresh failed", self.name)
            with self.lock:
                self.last_error = str(exc)
                self.last_error_category = category
                value = self.value or self.empty_factory()
                setattr(value, "error", str(exc))
                setattr(value, "errorCategory", category)
                setattr(value, "stale", self.value is not None)
                setattr(value, "lastSuccessfulAt", self.last_success_at)
                setattr(value, "refreshDurationSeconds", round(time.monotonic() - started, 2))
                self.value = value
                return value
        finally:
            with self.lock:
                self.refreshing = False

    def get(self) -> T:
        with self.lock:
            value = self.value
        if value is None:
            self.trigger()
            value = self.empty_factory()
            setattr(value, "error", "Cache is initializing")
            setattr(value, "errorCategory", "INITIALIZING")
            return value
        if self.age() >= self.interval(value):
            self.trigger()
        return value

    def set_value(self, value: T) -> T:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        setattr(value, "error", "")
        setattr(value, "errorCategory", "")
        setattr(value, "stale", False)
        setattr(value, "lastSuccessfulAt", now)
        with self.lock:
            self.value = value
            self.last_success_monotonic = time.monotonic()
            self.last_success_at = now
            self.last_error = ""
            self.last_error_category = ""
        return value

    def patch_value(self, value: T) -> T:
        with self.lock:
            has_complete_value = bool(self.last_success_monotonic)
        if has_complete_value:
            return self.set_value(value)
        setattr(value, "lastSuccessfulAt", "")
        with self.lock:
            self.value = value
        self.trigger()
        return value


class AppState:
    def __init__(self) -> None:
        self.reader = VolkswagenReader()
        self.usage = UsageLimiter()
        charging_interval = float(os.getenv("CHARGING_INTERVAL_SECONDS", "300"))
        idle_interval = float(os.getenv("IDLE_INTERVAL_SECONDS", "900"))
        detail_interval = float(os.getenv("DETAIL_INTERVAL_SECONDS", "43200"))
        location_interval = float(os.getenv("LOCATION_INTERVAL_SECONDS", "14400"))

        def background(
            loader: Callable[[], T], cost: int
        ) -> Callable[[], T]:
            def run() -> T:
                while self.reader.action_pending.is_set():
                    time.sleep(0.25)
                self.usage.acquire_background(cost)
                self.reader.context.background = True
                try:
                    return loader()
                except UsageLimit as exc:
                    if "reports too many requests" in str(exc):
                        self.usage.record_rate_limit()
                    raise
                finally:
                    self.reader.context.background = False

            return run

        self.charge = BackgroundCache(
            "charge",
            background(self.reader.read, 1),
            lambda value: (
                charging_interval
                if isinstance(value, VehicleData) and value.status == "C"
                else idle_interval
            ),
            VehicleData,
        )
        self.details = BackgroundCache(
            "details",
            background(self.reader.read_details, 3),
            lambda _: detail_interval,
            DetailData,
            initial_delay=600,
        )
        self.location = BackgroundCache(
            "location",
            background(self.reader.read_location, 1),
            lambda _: location_interval,
            LocationData,
            initial_delay=300,
        )

    def action(self, name: str, query: dict[str, list[str]]) -> object:
        self.reader.action_pending.set()
        try:
            self.usage.acquire_action()
            return self._action(name, query)
        except UsageLimit as exc:
            if "reports too many requests" in str(exc):
                self.usage.record_rate_limit()
            raise
        finally:
            self.reader.action_pending.clear()

    def _action(self, name: str, query: dict[str, list[str]]) -> object:
        actions: dict[str, Callable[[], object]] = {
            "lock": lambda: self.reader.set_locked(True),
            "unlock": lambda: self.reader.set_locked(False),
            "charging/start": lambda: self.reader.set_charging(True),
            "charging/stop": lambda: self.reader.set_charging(False),
            "climate/start": lambda: self.reader.set_climater(True),
            "climate/stop": lambda: self.reader.set_climater(False),
        }
        if name in actions:
            value = actions[name]()
            return self.charge.set_value(value)  # type: ignore[arg-type]
        if name == "climate/temperature":
            value = float(query["value"][0])
            desired = self.reader.set_target_temperature(value)
            with self.details.lock:
                current = self.details.value or DetailData()
            details = replace(
                current,
                targetTemperatureC=desired,
                observedAt=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
            return self.details.patch_value(details)
        if name.startswith("climate/option/"):
            desired = query["value"][0].casefold() in ("1", "true", "on")
            option = name.removeprefix("climate/option/")
            verified = self.reader.set_climate_option(option, desired)
            attribute = {
                "automatic-window-heating": "automaticWindowHeating",
                "zone-front-left": "climateZoneFrontLeft",
                "zone-front-right": "climateZoneFrontRight",
            }[option]
            with self.details.lock:
                current = self.details.value or DetailData()
            details = replace(
                current,
                **{
                    attribute: verified,
                    "observedAt": datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                },
            )
            return self.details.patch_value(details)
        if name == "target-soc":
            raise RuntimeError(
                "Target SoC control is unavailable because the idle Volkswagen UI "
                "does not expose a verifiable control"
            )
        raise KeyError(name)

    def health(self) -> HealthData:
        value = self.reader.phone_health()
        value.chargeLastSuccessfulAt = self.charge.last_success_at
        value.chargeAgeSeconds = (
            round(self.charge.age()) if self.charge.last_success_at else None
        )
        value.chargeRefreshing = self.charge.refreshing
        value.detailLastSuccessfulAt = self.details.last_success_at
        value.detailAgeSeconds = (
            round(self.details.age()) if self.details.last_success_at else None
        )
        value.locationLastSuccessfulAt = self.location.last_success_at
        value.locationAgeSeconds = (
            round(self.location.age()) if self.location.last_success_at else None
        )
        usage = self.usage.snapshot()
        value.usageBackgroundUsed = int(usage["backgroundUsed"])
        value.usageBackgroundLimit = int(usage["backgroundLimit"])
        value.usageActionsUsed = int(usage["actionsUsed"])
        value.usageActionsLimit = int(usage["actionsLimit"])
        value.usageCooldownSeconds = int(usage["cooldownSeconds"])
        if self.charge.value is None or value.adbState != "device":
            value.status = "error"
        elif self.charge.last_error or value.usageCooldownSeconds:
            value.status = "degraded"
        return value


class RequestHandler(BaseHTTPRequestHandler):
    state: AppState
    api_key: str

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            value = self.state.health()
            self.send_json(asdict(value), 200 if value.status != "error" else 503)
            return
        caches: dict[str, BackgroundCache[object]] = {
            "/charge": self.state.charge,  # type: ignore[dict-item]
            "/details": self.state.details,  # type: ignore[dict-item]
            "/location": self.state.location,  # type: ignore[dict-item]
        }
        if path == "/refresh":
            values = {
                "charge": asdict(self.state.charge.refresh()),
                "details": asdict(self.state.details.refresh()),
                "location": asdict(self.state.location.refresh()),
            }
            self.send_json(values, 200)
            return
        if path not in caches:
            self.send_error(404)
            return
        value = caches[path].get()
        status = 200 if getattr(value, "lastSuccessfulAt", "") else 503
        self.send_json(asdict(value), status)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        prefix = "/action/"
        if not parsed.path.startswith(prefix):
            self.send_error(404)
            return
        supplied_key = self.headers.get("X-API-Key", "")
        if not self.api_key or not hmac.compare_digest(supplied_key, self.api_key):
            self.send_error(401)
            return
        try:
            action = parsed.path[len(prefix):]
            LOG.info("Action requested: %s", action)
            value = self.state.action(action, parse_qs(parsed.query))
        except KeyError:
            self.send_error(404)
            return
        except UsageLimit as exc:
            self.send_json({"error": str(exc), "errorCategory": "RATE_LIMIT"}, 429)
            return
        except Exception as exc:
            self.send_json({"error": str(exc)}, 503)
            return
        self.send_json(asdict(value), 200)

    def send_json(self, value: object, status: int) -> None:
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, message: str, *args: object) -> None:
        print(f"{self.address_string()} - {message % args}", flush=True)


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    RequestHandler.state = AppState()
    RequestHandler.api_key = os.getenv("API_KEY", "")
    listen = os.getenv("LISTEN_ADDRESS", "127.0.0.1")
    port = int(os.getenv("PORT", "9920"))
    server = ThreadingHTTPServer((listen, port), RequestHandler)
    print(f"Volkswagen app connector listening on {listen}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
