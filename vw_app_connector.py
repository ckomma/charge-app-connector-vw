#!/usr/bin/env python3
"""Read Volkswagen app vehicle data through ADB UI automation.

Modified from janphkre/charge-app-connector for the Volkswagen Android app.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Generic, TypeVar
from urllib.parse import parse_qs, urlparse

from mqtt_publisher import MqttPublisher


LOG = logging.getLogger("vw-app-connector")
T = TypeVar("T")


class ActionPriority(RuntimeError):
    pass


class UsageLimit(RuntimeError):
    pass


class ActionQuarantined(RuntimeError):
    def __init__(self, reason: str, app_version: str, verified_version: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.app_version = app_version
        self.verified_version = verified_version


class IdempotencyConflict(RuntimeError):
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

    def acquire_background(
        self,
        cost: int,
        yield_to: Callable[[], bool] | None = None,
    ) -> None:
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
                wait = (
                    0.25
                    if yield_to is not None and yield_to()
                    else self.background_min_interval
                    - (now - float(self.state["lastBackgroundAt"]))
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
    fuelRange: int | None = None
    remainingChargeMinutes: int | None = None
    chargeRateKmH: int | None = None
    chargePowerKw: float | None = None
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
class ChargingSettingsData:
    targetSoc: int | None = None
    batteryCare: bool | None = None
    reducedAc: bool | None = None
    autoReleaseAcConnector: bool | None = None


@dataclass
class ChargingLocationSettingsData:
    name: str = ""
    directSoc: int | None = None
    targetSoc: int | None = None
    reducedAc: bool | None = None
    autoUnlock: bool | None = None
    previousDirectSoc: int | None = None
    previousTargetSoc: int | None = None


@dataclass
class ChargingLocationsData:
    locations: list[str] | None = None


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
    verifiedAppVersion: str = ""
    appVersionVerified: bool = True
    actionAvailable: bool = True
    actionBlockedReason: str = ""
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
    BATTERY_CARE_LABELS = ("Batterieschutz", "Battery Care", "Battery care")
    REDUCED_AC_LABELS = (
        "Reduzierter AC-Ladestrom",
        "Reduced AC current",
        "Reduced AC charging current",
    )
    AUTO_RELEASE_AC_LABELS = (
        "Automatisch entriegeln",
        "Automatic unlock",
        "Auto unlock",
        "Automatically release AC connector",
    )
    VEHICLE_REPORT_LABELS = (
        "Fahrzeugzustandsbericht.",
        "Fahrzeugzustandsbericht",
        "Fahrzeugzustand.",
        "Fahrzeugzustand",
        "Vehicle health report.",
        "Vehicle health report",
        "Vehicle Health Report",
        "Vehicle health.",
        "Vehicle health",
        "Vehicle Health",
        "Vehicle status report.",
        "Vehicle status report",
    )
    VEHICLE_REPORT_CONTENT_LABELS = (
        "Gesamtstrecke",
        "Total distance",
        "Odometer",
        "Kilometerstand",
        "Nächster Service",
        "NÃ¤chster Service",
        "NÃƒÂ¤chster Service",
        "Next service",
        "Keine Meldungen",
        "No issues found",
        "Synchronisiert:",
        "Synchronised:",
        "Synced:",
    )

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
        self.maps_package = os.getenv("MAPS_PACKAGE", "com.google.android.apps.maps")
        self.start_wait = float(os.getenv("APP_START_WAIT_SECONDS", "8"))
        self.detail_wait = float(os.getenv("DETAIL_WAIT_SECONDS", "3"))
        self.ui_update_timeout = float(
            os.getenv("UI_UPDATE_TIMEOUT_SECONDS", "8")
        )
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

    @staticmethod
    def parse_adb_devices(output: str) -> list[str]:
        serials: list[str] = []
        for line in output.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices attached"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serials.append(parts[0])
        return serials

    def resolve_usb_serial(self) -> str:
        if self.usb_serial.casefold() != "auto":
            return self.usb_serial

        result = self.run_adb("devices", "-l", timeout=5)
        message = (result.stdout or result.stderr).strip()
        if result.returncode:
            self.adb_last_connect_error = message or "ADB device discovery failed"
            raise RuntimeError(self.adb_last_connect_error)

        serials = [
            serial
            for serial in self.parse_adb_devices(result.stdout)
            if ":" not in serial
        ]
        if len(serials) == 1:
            self.adb_last_connect_error = ""
            return serials[0]
        if not serials:
            self.adb_last_connect_error = "No authorized USB ADB device found"
        else:
            self.adb_last_connect_error = "Multiple USB ADB devices found"
        raise RuntimeError(self.adb_last_connect_error)

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
                try:
                    usb_serial = self.resolve_usb_serial()
                except RuntimeError:
                    if self.adb_mode == "usb":
                        self.serial = self.usb_serial
                        self.adb_transport = "usb"
                        raise
                else:
                    if self.adb_state(usb_serial) == "device":
                        self.serial = usb_serial
                        self.adb_transport = "usb"
                        self.adb_last_connect_error = ""
                        return self.serial
                    if self.adb_mode == "usb":
                        self.serial = usb_serial
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

    @staticmethod
    def ui_dump_paths(remote_name: str) -> tuple[str, str]:
        return f"/sdcard/{remote_name}", f"/storage/emulated/0/{remote_name}"

    def read_ui_dump(self, remote_name: str, timeout: float = 10) -> str:
        primary_path, fallback_path = self.ui_dump_paths(remote_name)
        try:
            return self.shell("cat", primary_path, timeout=timeout)
        except RuntimeError:
            return self.shell("cat", fallback_path, timeout=timeout)

    def dump_ui(self, remote_name: str, compressed: bool = False) -> ET.Element:
        remote_path, _ = self.ui_dump_paths(remote_name)
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                args = ["uiautomator", "dump"]
                if compressed:
                    args.append("--compressed")
                args.append(remote_path)
                self.shell(*args, timeout=30)
                return ET.fromstring(self.read_ui_dump(remote_name, timeout=10))
            except (RuntimeError, ET.ParseError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1)
        assert last_error is not None
        raise last_error

    @classmethod
    def is_proximity_overlay(cls, root: ET.Element) -> bool:
        text = "\n".join(cls.strings(root)).casefold()
        return any(
            phrase in text
            for phrase in (
                "den kopfhörerbereich nicht abdecken",
                "don't cover the earphone area",
                "do not cover the earphone area",
                "don't cover the top of the screen",
            )
        )

    def dump_ui_with_overlay_recovery(self, remote_name: str) -> ET.Element:
        root = self.dump_ui(remote_name)
        if self.is_proximity_overlay(root) or not self.strings(root):
            self.shell("input", "keyevent", "KEYCODE_VOLUME_UP")
            time.sleep(2)
            root = self.dump_ui(remote_name)
        return root

    def dump_ui_with_compose_fallback(self, remote_name: str) -> ET.Element:
        root = self.dump_ui_with_overlay_recovery(remote_name)
        if self.has_app_resource_nodes(root) or not self.app_in_foreground():
            return root
        compressed = self.dump_ui(remote_name, compressed=True)
        return compressed if self.has_app_resource_nodes(compressed) else root

    def has_app_resource_nodes(self, root: ET.Element) -> bool:
        return any(
            self.package in node.attrib.get("resource-id", "")
            for node in root.iter()
        )

    def save_diagnostics(self, category: str, error: Exception) -> None:
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        stem = self.diagnostics_dir / f"{stamp}-{category.casefold()}"
        try:
            self.shell("uiautomator", "dump", "/sdcard/vw-error.xml", timeout=30)
            xml = self.read_ui_dump("vw-error.xml", timeout=10)
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

    def open_overview(
        self, required_prefixes: tuple[str, ...] = ()
    ) -> ET.Element:
        for navigation_attempt in range(2):
            deadline = time.monotonic() + self.ui_update_timeout
            saw_overview = False
            nudged_overview = False
            while True:
                overview = self.dump_ui_with_compose_fallback("vw-overview.xml")
                if not self.app_in_foreground():
                    if time.monotonic() >= deadline:
                        break
                    self.close_system_overlays()
                    if not self.app_in_foreground():
                        self.launch()
                    time.sleep(1)
                    continue
                overview_text = "\n".join(self.strings(overview)).casefold()
                if (
                    "too many requests" in overview_text
                    or "zu viele anfragen" in overview_text
                ):
                    raise UsageLimit("Volkswagen app reports too many requests")
                self.raise_for_lockout_state(overview)
                try:
                    self.range_tile_center(overview)
                    saw_overview = True
                    if not required_prefixes:
                        return overview
                except RuntimeError:
                    pass
                if required_prefixes:
                    try:
                        self.described_node_center_any(overview, required_prefixes)
                        return overview
                    except RuntimeError:
                        pass
                if time.monotonic() >= deadline:
                    break
                if saw_overview and required_prefixes and not nudged_overview:
                    width, height = self.viewport_size(overview)
                    # Volkswagen occasionally shows a non-semantic promotional
                    # banner over the overview menu. A single bounds-derived
                    # content nudge gives that transient layer time to clear
                    # without depending on a fixed close-button coordinate.
                    self.shell(
                        "input",
                        "swipe",
                        str(width // 2),
                        str(round(height * 0.78)),
                        str(width // 2),
                        str(round(height * 0.48)),
                        "300",
                    )
                    nudged_overview = True
                    time.sleep(1)
                else:
                    time.sleep(0.5)
            if navigation_attempt == 0:
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

    @classmethod
    def raise_for_lockout_state(cls, root: ET.Element) -> None:
        text = "\n".join(cls.strings(root)).casefold()
        if "data no longer up-to-date" in text:
            raise UsageLimit(
                "Volkswagen app reports data no longer up-to-date"
            )
        if "currently unavailable. please try again later." in text:
            raise UsageLimit(
                "Volkswagen app reports data currently unavailable"
            )

    @staticmethod
    def node_bounds(node: ET.Element) -> tuple[int, int, int, int] | None:
        match = re.fullmatch(
            r"\[(\d+),(\d+)]\[(\d+),(\d+)]", node.attrib.get("bounds", "")
        )
        if not match:
            return None
        return tuple(map(int, match.groups()))

    @classmethod
    def node_center(cls, node: ET.Element) -> tuple[int, int] | None:
        bounds = cls.node_bounds(node)
        if not bounds:
            return None
        left, top, right, bottom = bounds
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
                cls.text_matches_label(description, prefix)
                or cls.text_matches_label(text, prefix)
                for prefix in prefixes
            ):
                continue
            center = cls.node_center(node)
            if center:
                return center
        raise RuntimeError(
            f"Volkswagen UI element not found: {' / '.join(prefixes)}"
        )

    @staticmethod
    def text_matches_label(value: str, label: str) -> bool:
        lowered = value.casefold()
        wanted = label.casefold()
        if wanted in lowered:
            return True
        return wanted.rstrip(".:") in lowered

    @classmethod
    def resource_nodes(cls, root: ET.Element, suffix: str) -> list[ET.Element]:
        return [
            node
            for node in root.iter()
            if node.attrib.get("resource-id", "").endswith(suffix)
            and cls.node_center(node) is not None
        ]

    @classmethod
    def resource_node_center(cls, root: ET.Element, suffix: str) -> tuple[int, int]:
        nodes = cls.resource_nodes(root, suffix)
        if not nodes:
            raise RuntimeError(f"Volkswagen UI resource not found: {suffix}")
        center = cls.node_center(nodes[0])
        assert center is not None
        return center

    @classmethod
    def range_tile_center(cls, root: ET.Element) -> tuple[int, int]:
        candidates: list[tuple[int, int, int]] = []
        for node in root.iter():
            text = " ".join(
                value
                for key in ("text", "content-desc")
                if (value := node.attrib.get(key, "").strip())
            )
            if not re.search(
                r"Batteriereichweite|Battery range|Electric range",
                text,
                re.IGNORECASE,
            ):
                continue
            if not re.search(
                r"\b\d+\s*(?:Kilometer|kilometres?|km)\b",
                text,
                re.IGNORECASE,
            ):
                continue
            center = cls.node_center(node)
            bounds = cls.node_bounds(node)
            if center and bounds:
                left, top, right, bottom = bounds
                candidates.append(((right - left) * (bottom - top), *center))
        if candidates:
            _area, x, y = max(candidates)
            return (x, y)
        raise RuntimeError(
            "Volkswagen UI element not found: "
            "Batteriereichweite / Battery range / Electric range"
        )

    @classmethod
    def map_view_center(cls, root: ET.Element) -> tuple[int, int]:
        for node in root.iter():
            if node.attrib.get("class") == "android.view.TextureView":
                center = cls.node_center(node)
                if center:
                    return center
        for node in root.iter():
            if node.attrib.get("resource-id", "").endswith("catNavMapFragment"):
                center = cls.node_center(node)
                if center:
                    return center
        return (540, 786)

    @classmethod
    def vehicle_marker_label_center(cls, root: ET.Element) -> tuple[int, int]:
        x, y = cls.map_view_center(root)
        _width, height = cls.viewport_size(root)
        # Google Maps renders the vehicle label in the map canvas, so Android
        # exposes neither semantic text nor bounds for it. Car Locate centers
        # the marker pin; its tappable label sits just above that pin.
        return (x, y - max(40, round(height * 0.075)))

    @classmethod
    def parse_vehicle_name(cls, root: ET.Element) -> str:
        text = "\n".join(cls.strings(root))
        match = re.search(
            r"(?:Ihr Fahrzeug|Your vehicle):\s*(.+?)\.\s*"
            r"(?:Gerade synchronisiert|Just synchronized|Just synchronised|"
            r"Just synced|Synchronisiert|Synchronised|Synced)\b",
            text,
            re.IGNORECASE,
        )
        return match.group(1).strip() if match else ""

    @classmethod
    def vehicle_report_center(cls, root: ET.Element) -> tuple[int, int]:
        return cls.described_node_center_any(root, cls.VEHICLE_REPORT_LABELS)

    @classmethod
    def is_vehicle_report_page(cls, root: ET.Element) -> bool:
        text = "\n".join(cls.strings(root)).casefold()
        return any(
            label.casefold() in text
            for label in cls.VEHICLE_REPORT_CONTENT_LABELS
        )

    @classmethod
    def viewport_size(cls, root: ET.Element) -> tuple[int, int]:
        bounds = [
            value
            for node in root.iter()
            if (value := cls.node_bounds(node)) is not None
        ]
        if not bounds:
            raise RuntimeError("Volkswagen UI viewport not found")
        return (
            max(value[2] for value in bounds),
            max(value[3] for value in bounds),
        )

    @classmethod
    def editable_node_center(cls, root: ET.Element) -> tuple[int, int]:
        for node in root.iter():
            if node.attrib.get("class") != "android.widget.EditText":
                continue
            center = cls.node_center(node)
            if center:
                return center
        raise RuntimeError("Volkswagen S-PIN input field not found")

    def wait_for_lock_control(self, expected: bool) -> ET.Element:
        deadline = time.monotonic() + self.ui_update_timeout
        while True:
            root = self.dump_ui_with_overlay_recovery("vw-lock-control.xml")
            current = self.parse_locked("\n".join(self.strings(root)))
            if current is expected:
                return root
            if time.monotonic() >= deadline:
                raise RuntimeError("Volkswagen lock control not found")
            time.sleep(0.5)

    def wait_for_pin_dialog(self) -> ET.Element:
        deadline = time.monotonic() + self.ui_update_timeout
        while True:
            root = self.dump_ui("vw-pin.xml")
            text = "\n".join(self.strings(root))
            if re.search(r"S-?PIN", text, re.IGNORECASE):
                try:
                    self.editable_node_center(root)
                except RuntimeError:
                    pass
                else:
                    return root
            if time.monotonic() >= deadline:
                raise RuntimeError("Volkswagen S-PIN dialog not found")
            time.sleep(0.5)

    def dismiss_map_notice(self, root: ET.Element) -> ET.Element:
        text = "\n".join(self.strings(root)).casefold()
        if "google maps" not in text:
            return root
        try:
            x, y = self.described_node_center_any(
                root,
                (
                    "Agree",
                    "I agree",
                    "Accept",
                    "OK",
                    "Zustimmen",
                    "Einverstanden",
                    "Akzeptieren",
                ),
            )
        except RuntimeError:
            return root
        self.shell("input", "tap", str(x), str(y))
        time.sleep(1)
        return self.dump_ui_with_overlay_recovery("vw-location-map-notice-dismissed.xml")

    def dismiss_app_notice(self, root: ET.Element, remote_name: str) -> ET.Element:
        for labels in (
            (
                "Not now",
                "Maybe later",
                "No thanks",
                "Later",
                "Nicht jetzt",
                "Später",
                "Spaeter",
                "Nein danke",
            ),
            ("Close", "Schließen", "Schliessen"),
        ):
            try:
                x, y = self.described_node_center_any(root, labels)
            except RuntimeError:
                continue
            self.shell("input", "tap", str(x), str(y))
            time.sleep(1)
            return self.dump_ui_with_overlay_recovery(remote_name)
        return root

    def dismiss_charge_notice(self, root: ET.Element, remote_name: str) -> ET.Element:
        text = "\n".join(self.strings(root)).casefold()
        english_notice = any(
            phrase in text
            for phrase in (
                "vehicle health",
                "commands may be executed",
                "executed with a delay",
                "may be delayed",
            )
        )
        german_notice = "fahrzeuggesundheit" in text or (
            ("befehl" in text or "kommando" in text) and "verz" in text
        )
        if not english_notice and not german_notice:
            return root
        for labels in (
            ("Verstanden", "Alles klar", "Got it", "OK", "Okay"),
            ("Close", "SchlieÃŸen", "Schliessen"),
        ):
            try:
                x, y = self.described_node_center_any(root, labels)
                break
            except RuntimeError:
                continue
        else:
            try:
                x, y = self.described_node_center_any(
                    root,
                    (
                        "Warning",
                        "Warnung",
                        "vehicle health",
                        "commands may be executed",
                        "Fahrzeuggesundheit",
                    ),
                )
            except RuntimeError:
                return root
        self.shell("input", "tap", str(x), str(y))
        time.sleep(1)
        return self.dump_ui_with_overlay_recovery(remote_name)

    @classmethod
    def is_charge_detail_page(cls, root: ET.Element) -> bool:
        text = "\n".join(cls.strings(root))
        if cls.parse_soc(text) is not None:
            return True
        return bool(
            re.search(
                r"Laden starten|Laden stoppen|Wird geladen|Start charging|"
                r"Stop charging|Is charging|Zielladestand|Target charge",
                text,
                re.IGNORECASE,
            )
        )

    def wait_for_charge_detail(self, remote_name: str) -> ET.Element:
        deadline = time.monotonic() + self.ui_update_timeout
        while True:
            root = self.dump_ui_with_overlay_recovery(remote_name)
            root = self.dismiss_charge_notice(root, remote_name)
            if self.is_charge_detail_page(root):
                return root
            if time.monotonic() >= deadline:
                raise RuntimeError("Volkswagen charge details did not open")
            time.sleep(0.5)

    def wait_for_car_locate_button(self, remote_name: str) -> ET.Element:
        deadline = time.monotonic() + self.ui_update_timeout
        while True:
            root = self.dismiss_map_notice(
                self.dump_ui_with_overlay_recovery(remote_name)
            )
            try:
                self.described_node_center(root, "Car Locate Button")
                return root
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)

    @classmethod
    def parse_location_details(cls, root: ET.Element) -> tuple[str, str]:
        for value in cls.strings(root):
            if not re.search(
                r"\n(?:Geparkt seit|Parked since|Parked for)\s+",
                value,
                re.IGNORECASE,
            ):
                continue
            return tuple(value.split("\n", 1))  # type: ignore[return-value]

        parked_duration = ""
        for value in cls.strings(root):
            if re.match(
                r"(?:Geparkt seit|Parked since|Parked for)\b",
                value,
                re.IGNORECASE,
            ):
                parked_duration = value
                break

        try:
            _route_x, route_y = cls.described_node_center(root, "Route")
        except RuntimeError:
            return "", ""

        candidates: list[tuple[int, str]] = []
        ignored = {
            "Route",
            "Share",
            "Navigation Tab",
            "Google Map",
            "Google Maps",
            "Map Back Button",
            "Map Settings Button",
            "Car Locate Button",
            "Device Location Button",
            "Close details view",
        }
        for node in root.iter():
            if node.attrib.get("class") != "android.widget.TextView":
                continue
            text = node.attrib.get("text", "").strip()
            if not text or text in ignored or text == parked_duration:
                continue
            bounds = cls.node_bounds(node)
            if not bounds:
                continue
            left, top, right, _bottom = bounds
            if top >= route_y or left > 140 or right - left < 360:
                continue
            candidates.append((top, text))

        if candidates:
            candidates.sort()
            return candidates[-1][1], parked_duration
        return "", ""

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
        if match:
            return int(match.group(1))
        compact_match = re.search(
            r"\b(\d+)\s*%\s*[•·]\s*(?:Charging|Wird geladen|Lädt|LÃ¤dt)\b",
            text,
            re.IGNORECASE,
        )
        return int(compact_match.group(1)) if compact_match else None

    @staticmethod
    def text_reports_active_charging(text: str) -> bool:
        return bool(
            re.search(
                r"Laden stoppen|Wird geladen|Lädt|Stop charging|"
                r"Is charging|charging in progress|"
                r"(?:^|\n)\s*(?:Stop|Stopp)\s*(?:\n|$)|"
                r"%\s*[•·]\s*Charging\b(?!\s+station)",
                text,
                re.IGNORECASE,
            )
        )

    @staticmethod
    def parse_range_value(text: str, labels: tuple[str, ...]) -> int | None:
        label_pattern = "|".join(re.escape(label) for label in labels)
        match = re.search(
            rf"(?:{label_pattern}):\s*(\d+)\s*"
            r"(?:Kilometer|kilometres?|km)",
            text,
            re.IGNORECASE,
        )
        return int(match.group(1)) if match else None

    @staticmethod
    def temperature_label_value(label: str) -> float | None:
        value = label.strip().replace(",", ".")
        if value.casefold() == "lo":
            return 15.5
        if value.casefold() == "hi":
            return 30.0
        if re.fullmatch(r"\d{2}(?:\.\d)?", value):
            return float(value)
        return None

    @staticmethod
    def temperature_node_value(node: ET.Element) -> float | None:
        for key in ("text", "content-desc"):
            value = VolkswagenReader.temperature_label_value(
                node.attrib.get(key, "")
            )
            if value is not None:
                return value
        return None

    @staticmethod
    def parse_target_temperature(root: ET.Element) -> float:
        viewport_bounds = [
            bounds
            for node in root.iter()
            if (bounds := VolkswagenReader.node_bounds(node)) is not None
        ]
        if not viewport_bounds:
            raise RuntimeError("Volkswagen target temperature not found")
        viewport_center = (
            min(bounds[0] for bounds in viewport_bounds)
            + max(bounds[2] for bounds in viewport_bounds)
        ) / 2
        candidates: list[tuple[float, float]] = []
        for node in root.iter():
            value = VolkswagenReader.temperature_node_value(node)
            if value is None:
                continue
            center = VolkswagenReader.node_center(node)
            if center:
                candidates.append((abs(center[0] - viewport_center), value))
        if not candidates:
            raise RuntimeError("Volkswagen target temperature not found")
        return min(candidates)[1]

    @classmethod
    def temperature_value_center(
        cls, root: ET.Element, desired: float
    ) -> tuple[int, int]:
        for node in root.iter():
            value = cls.temperature_node_value(node)
            if value != desired:
                continue
            center = cls.node_center(node)
            if center:
                return center
        raise RuntimeError(
            f"Volkswagen target temperature value not found: {desired:g}"
        )

    def wait_for_target_temperature(
        self, desired: float, remote_name: str
    ) -> ET.Element:
        deadline = time.monotonic() + self.ui_update_timeout
        while True:
            root = self.dump_ui_with_overlay_recovery(remote_name)
            try:
                if self.parse_target_temperature(root) == desired:
                    return root
            except RuntimeError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Volkswagen target temperature adjustment failed"
                )
            time.sleep(0.5)

    def wait_for_described_node(
        self, remote_name: str, labels: tuple[str, ...]
    ) -> tuple[ET.Element, tuple[int, int]]:
        deadline = time.monotonic() + self.ui_update_timeout
        while True:
            root = self.dump_ui(remote_name)
            try:
                return root, self.described_node_center_any(root, labels)
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)

    @staticmethod
    def duration_part_minutes(value: str, unit_minutes: int) -> int:
        lowered = value.casefold()
        if lowered in ("null", "zero"):
            return 0
        return int(value) * unit_minutes

    @staticmethod
    def parse_charging_details(text: str, result: VehicleData) -> None:
        details_match = re.search(
            r"(\d+|Null|Zero)\s*(?:Stunden?|hours?)\s+(?:und\.?|and\.?)\s*"
            r"(\d+)\s*(?:Minuten?|minutes?)"
            r".*?(?:Ladegeschwindigkeit|Charging speed):\s*(\d+)\s*"
            r"(?:Kilometer pro Stunde|kilometres? per hour|km/h)"
            r".*?(?:Ladeleistung|Charging power|Charging capacity):\s*"
            r"(\d+(?:[,.]\d+)?)\s*"
            r"(?:Kilowatt|kW)"
            r".*?(?:Zielladestand|Target charge level|Target charge):\s*(\d+)\s*"
            r"(?:Prozent|per cent|percent|%)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if details_match:
            hours_text, minutes_text, rate_text, power_text, target_text = (
                details_match.groups()
            )
            result.remainingChargeMinutes = (
                VolkswagenReader.duration_part_minutes(hours_text, 60)
                + int(minutes_text)
            )
            result.chargeRateKmH = int(rate_text)
            result.chargePowerKw = float(power_text.replace(",", "."))
            result.targetSoc = int(target_text)

        compact_time = re.search(r"\b(\d{1,2}):(\d{2})\s*h\b", text)
        if compact_time:
            hours_text, minutes_text = compact_time.groups()
            result.remainingChargeMinutes = int(hours_text) * 60 + int(minutes_text)

        compact_rate = re.search(
            r"\b(\d+)\s*(?:km\s*per\s*h|km/h|Kilometer pro Stunde)\b",
            text,
            re.IGNORECASE,
        )
        if compact_rate:
            result.chargeRateKmH = int(compact_rate.group(1))

        compact_power = re.search(
            r"\b(\d+(?:[,.]\d+)?)\s*(?:kW|Kilowatt)\b",
            text,
            re.IGNORECASE,
        )
        if compact_power:
            result.chargePowerKw = float(compact_power.group(1).replace(",", "."))

        target_match = re.search(
            r"(?:Zielladestand|Target charge level|Target charge|"
            r"Upper charge limit|Ladeobergrenze|Obere Ladegrenze):?\s*(\d+)\s*"
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
        elif mode_match := re.search(
            r"\b(Sofortladen|Immediate charging)\b",
            text,
            re.IGNORECASE,
        ):
            result.chargingMode = mode_match.group(1).strip()

    @classmethod
    def parse_vehicle_report(cls, root: ET.Element, result: DetailData) -> None:
        values = cls.strings(root)
        report_text = "\n".join(values)

        odometer = re.search(
            r"(?:Gesamtstrecke|Total distance|Odometer)\s*([\d.,]+)\s*km",
            report_text,
            re.IGNORECASE,
        )
        if not odometer:
            for index, value in enumerate(values):
                if not re.fullmatch(
                    r"Gesamtstrecke|Total distance|Odometer",
                    value,
                    re.IGNORECASE,
                ):
                    continue
                for candidate in values[index + 1:index + 4]:
                    odometer = re.search(r"([\d.,]+)\s*km", candidate, re.IGNORECASE)
                    if odometer:
                        break
                if odometer:
                    break

        service = re.search(
            r"(?:Nächster Service|NÃ¤chster Service|Next service)\s*"
            r"(?:in\s*)?(\d+)\s*(?:Tage|days)",
            report_text,
            re.IGNORECASE,
        )
        if not service:
            for index, value in enumerate(values):
                if not re.fullmatch(
                    r"Nächster Service|NÃ¤chster Service|Next service",
                    value,
                    re.IGNORECASE,
                ):
                    continue
                for candidate in values[index + 1:index + 4]:
                    service = re.search(
                        r"(\d+)\s*(?:Tage|days)",
                        candidate,
                        re.IGNORECASE,
                    )
                    if service:
                        break
                if service:
                    break

        report_sync = re.search(
            r"(?:Synchronisiert|Synchronised|Synced):\s*([^\n]+)",
            report_text,
            re.IGNORECASE,
        )

        if not (odometer or service or report_sync or cls.is_vehicle_report_page(root)):
            raise RuntimeError("Volkswagen vehicle health report did not open")

        result.odometerKm = (
            int(re.sub(r"[.,]", "", odometer.group(1))) if odometer else None
        )
        result.serviceDays = int(service.group(1)) if service else None
        result.warningStatus = (
            "Keine Meldungen"
            if re.search(
                r"Keine Meldungen|No messages|No warnings|No issues found",
                report_text,
                re.IGNORECASE,
            )
            else "Meldungen vorhanden"
        )
        result.reportSyncAge = report_sync.group(1).strip() if report_sync else ""

    @staticmethod
    def parse_navigation_coordinates(text: str) -> tuple[float, float]:
        matches = re.findall(
            r"google\.navigation:q=(-?\d+(?:\.\d+)?)(?:%2C|,)"
            r"(-?\d+(?:\.\d+)?)",
            text,
            re.IGNORECASE,
        )
        if not matches:
            raise RuntimeError("Volkswagen navigation coordinates not found")
        latitude, longitude = matches[-1]
        return (float(latitude), float(longitude))

    def launch(self) -> None:
        self.close_system_overlays()
        self.shell("am", "force-stop", self.package)
        self.shell(
            "am",
            "start",
            "-n",
            f"{self.package}/.SingleActivity",
            timeout=15,
        )
        time.sleep(self.start_wait)
        self.close_system_overlays()
        if not self.app_in_foreground():
            self.close_system_overlays()
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
            self.close_system_overlays()
        if not self.app_in_foreground():
            raise RuntimeError("Volkswagen app did not reach the foreground")
        if (
            getattr(self.context, "background", False)
            and self.action_pending.is_set()
        ):
            raise ActionPriority("Background refresh preempted by action")

    def close_system_overlays(self) -> None:
        try:
            self.shell("cmd", "statusbar", "collapse", timeout=5)
        except RuntimeError:
            LOG.debug("Android status bar collapse failed", exc_info=True)

    def app_in_foreground(self) -> bool:
        windows = self.shell("dumpsys", "window", timeout=20)
        current_focus = re.search(r"mCurrentFocus=([^\n]+)", windows)
        if current_focus:
            focused_window = current_focus.group(1)
            if self.package in focused_window:
                return True
            if "null" not in focused_window.casefold():
                return False
        activity = self.shell("dumpsys", "activity", "activities", timeout=20)
        for line in activity.splitlines():
            if (
                "topResumedActivity" in line
                or "mResumedActivity" in line
            ) and self.package in line:
                return True
        return bool(
            re.search(rf"mObscuringWindow=.*{re.escape(self.package)}", windows)
            or re.search(rf"mFocusedApp=.*{re.escape(self.package)}", windows)
        )

    def keyguard_showing(self) -> bool:
        policy = self.shell("dumpsys", "window", "policy", timeout=20)
        return "isKeyguardShowing=true" in policy

    def display_size(self) -> tuple[int, int]:
        output = self.shell("wm", "size", timeout=10)
        match = re.search(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", output)
        if not match:
            raise RuntimeError("Android display size not found")
        return int(match.group(1)), int(match.group(2))

    def wake_screen(self) -> None:
        self.shell("input", "keyevent", "KEYCODE_WAKEUP")
        time.sleep(0.5)
        power = self.shell("dumpsys", "power", timeout=10)
        if "mWakefulness=Awake" not in power:
            self.shell("input", "keyevent", "KEYCODE_POWER")
            time.sleep(1)
        self.shell("svc", "power", "stayon", "true")
        self.shell("wm", "dismiss-keyguard")
        self.shell("input", "keyevent", "82")
        time.sleep(1)
        if self.keyguard_showing():
            width, height = self.display_size()
            self.shell(
                "input",
                "swipe",
                str(width // 2),
                str(round(height * 0.8)),
                str(width // 2),
                str(round(height * 0.2)),
                "700",
            )
            time.sleep(1)
            if self.keyguard_showing():
                raise RuntimeError(
                    "Android keyguard could not be dismissed; disable the secure lock"
                )

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

        result.range = self.parse_range_value(
            overview_text,
            ("Batteriereichweite", "Battery range", "Electric range"),
        )
        result.fuelRange = self.parse_range_value(
            overview_text,
            ("Kraftstoffreichweite", "Fuel range"),
        )

        result.syncAgeMinutes = self.parse_sync_age(overview_text)
        result.climater = self.parse_climater(overview_text)
        result.locked = self.parse_locked(overview_text)

        x, y = self.range_tile_center(overview)
        self.shell("input", "tap", str(x), str(y))
        time.sleep(self.detail_wait)

        detail_text = "\n".join(
            self.strings(self.wait_for_charge_detail("vw-detail.xml"))
        )
        result.soc = self.parse_soc(detail_text)
        if result.soc is None:
            raise RuntimeError("Volkswagen state of charge not found")
        self.parse_charging_details(detail_text, result)

        lowered = detail_text.casefold()
        if self.text_reports_active_charging(detail_text):
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
            try:
                self.wait_for_lock_control(current)
                width, height = self.display_size()
                swipe_x = width // 2
                lower_y = round(height * 0.85)
                upper_y = round(height * 0.63)
                # The Compose lock graphic has no stable accessibility node.
                # Use physical display coordinates because MIUI clips the app's
                # accessibility viewport above the gesture's actual touch area.
                if desired:
                    self.shell(
                        "input", "swipe", str(swipe_x), str(lower_y),
                        str(swipe_x), str(upper_y), "900"
                    )
                else:
                    self.shell(
                        "input", "swipe", str(swipe_x), str(upper_y),
                        str(swipe_x), str(lower_y), "900"
                    )

                pin_root = self.wait_for_pin_dialog()
                x, y = self.editable_node_center(pin_root)
                self.shell("input", "tap", str(x), str(y))
                self.shell("input", "text", self.spin)
            except Exception as exc:
                self.save_diagnostics("LOCK" if desired else "UNLOCK", exc)
                raise
            time.sleep(8)
            return self.with_retries(self._read, "ACTION_VERIFY")

    def _read_location(self) -> LocationData:
        self.launch()
        root = self.dismiss_app_notice(
            self.dump_ui_with_overlay_recovery("vw-location-start.xml"),
            "vw-location-start-notice-dismissed.xml",
        )
        root_text = "\n".join(self.strings(root)).casefold()
        if (
            "limited services" in root_text
            or "not logged into the vehicle" in root_text
            or "eingeschränkte dienste" in root_text
            or "eingeschraenkte dienste" in root_text
            or "nicht im fahrzeug angemeldet" in root_text
        ):
            raise RuntimeError(
                "Volkswagen app reports limited services; not logged into the vehicle"
            )
        vehicle_name = self.parse_vehicle_name(root)
        x, y = self.described_node_center(root, "Navigation Tab")
        self.shell("input", "tap", str(x), str(y))
        time.sleep(self.detail_wait)

        map_root = self.wait_for_car_locate_button("vw-location-map.xml")
        x, y = self.described_node_center(map_root, "Car Locate Button")
        self.shell("input", "tap", str(x), str(y))
        time.sleep(self.detail_wait)

        centered_map = self.dump_ui("vw-location-centered-map.xml")
        x, y = self.vehicle_marker_label_center(centered_map)
        self.shell("input", "tap", str(x), str(y))
        time.sleep(self.detail_wait)
        details = self.dump_ui("vw-location-details.xml")
        if vehicle_name and not any(
            vehicle_name.casefold() in value.casefold()
            for value in self.strings(details)
        ):
            raise RuntimeError("Volkswagen vehicle marker was not selected")
        result = LocationData(
            observedAt=datetime.now().astimezone().isoformat(timespec="seconds")
        )
        result.address, result.parkedDuration = self.parse_location_details(details)
        if not result.address:
            raise RuntimeError("Volkswagen vehicle address not found")

        x, y = self.described_node_center(details, "Route")
        self.shell("am", "force-stop", self.maps_package)
        time.sleep(1)
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

    def read_location(self) -> LocationData:
        with self.screen_session():
            return self.with_retries(
                self._read_location,
                "LOCATION",
            )

    def set_charging(self, desired: bool) -> VehicleData:
        with self.screen_session():
            self.launch()
            overview = self.open_overview()
            x, y = self.range_tile_center(overview)
            self.shell("input", "tap", str(x), str(y))
            time.sleep(self.detail_wait)
            detail = self.wait_for_charge_detail("vw-charge-action.xml")
            text = "\n".join(self.strings(detail))
            current = self.text_reports_active_charging(text)
            if current != desired:
                labels = (
                    ("Laden starten", "Start charging")
                    if desired
                    else ("Laden stoppen", "Stop charging")
                )
                x, y = self.described_node_center_any(detail, labels)
                self.shell("input", "tap", str(x), str(y))
                time.sleep(8)
            result = self.with_retries(self._read, "ACTION_VERIFY")
            if desired and result.status != "C":
                if (
                    result.soc is not None
                    and result.targetSoc is not None
                    and result.soc >= result.targetSoc
                ):
                    raise RuntimeError(
                        "Volkswagen did not start charging; current SoC is at "
                        "or above the target charge level"
                    )
                raise RuntimeError("Volkswagen did not start charging")
            if not desired and result.status == "C":
                raise RuntimeError("Volkswagen did not stop charging")
            return result

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

    @classmethod
    def checked_node_near_labels(
        cls, root: ET.Element, labels: tuple[str, ...]
    ) -> ET.Element:
        label_centers: list[tuple[int, int]] = []
        for node in root.iter():
            text = " ".join(
                (
                    node.attrib.get("text", ""),
                    node.attrib.get("content-desc", ""),
                )
            ).casefold()
            if not any(label.casefold() in text for label in labels):
                continue
            center = cls.node_center(node)
            if center:
                label_centers.append(center)
        switches = cls.checked_nodes(root)
        if not label_centers or not switches:
            raise RuntimeError(
                f"Volkswagen climate option not found: {' / '.join(labels)}"
            )
        return min(
            switches,
            key=lambda node: min(
                abs((cls.node_center(node) or (0, 0))[1] - label[1])
                for label in label_centers
            ),
        )

    def wait_for_checked_option(
        self,
        remote_name: str,
        labels: tuple[str, ...],
        desired: bool | None = None,
    ) -> tuple[ET.Element, ET.Element]:
        deadline = time.monotonic() + self.ui_update_timeout
        while True:
            root = self.dump_ui(remote_name)
            try:
                node = self.checked_node_near_labels(root, labels)
                if desired is None or (
                    node.attrib.get("checked") == "true"
                ) == desired:
                    return root, node
            except RuntimeError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Volkswagen climate option not found or not updated: "
                    f"{' / '.join(labels)}"
                )
            time.sleep(0.5)

    def open_climate(self) -> ET.Element:
        overview = self.open_overview(
            ("Vorklimatisierung.", "Air conditioning.", "Climate control.")
        )
        x, y = self.described_node_center_any(
            overview,
            ("Vorklimatisierung.", "Air conditioning.", "Climate control."),
        )
        self.shell("input", "tap", str(x), str(y))
        deadline = time.monotonic() + self.ui_update_timeout
        while True:
            root = self.dump_ui("vw-climate.xml")
            try:
                self.parse_target_temperature(root)
                return root
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Volkswagen climate page did not finish loading"
                    )
                time.sleep(0.5)

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
        overview = self.open_overview(self.VEHICLE_REPORT_LABELS)
        x, y = self.vehicle_report_center(overview)
        self.shell("input", "tap", str(x), str(y))
        time.sleep(self.detail_wait)
        report = self.dump_ui("vw-report.xml")
        self.raise_for_lockout_state(report)
        if not self.is_vehicle_report_page(report):
            raise RuntimeError("Volkswagen vehicle health report did not open")
        self.parse_vehicle_report(report, result)

        self.launch()
        overview = self.open_overview(("Abfahrtszeiten.", "Departure times."))
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
        if desired < 15.5 or desired > 30:
            raise ValueError("Target temperature must be between 15.5 and 30 °C")
        with self.screen_session():
            self.launch()
            climate = self.open_climate()
            current = self.parse_target_temperature(climate)
            while current != desired:
                next_value = current + (0.5 if desired > current else -0.5)
                x, y = self.temperature_value_center(climate, next_value)
                self.shell("input", "tap", str(x), str(y))
                climate = self.wait_for_target_temperature(
                    next_value, "vw-climate-adjust.xml"
                )
                current = next_value
            verify = self.parse_target_temperature(climate)
            if verify != desired:
                raise RuntimeError("Volkswagen target temperature verification failed")
            return desired

    def set_climate_option(self, option: str, desired: bool) -> bool:
        option_spec = {
            "automatic-window-heating": (
                "settings",
                ("Automatische Scheibenheizung", "Automatic window heating"),
            ),
            "zone-front-left": (
                "zones",
                ("Vorne links", "Front left"),
            ),
            "zone-front-right": (
                "zones",
                ("Vorne rechts", "Front right"),
            ),
        }
        if option not in option_spec:
            raise KeyError(option)
        page, labels = option_spec[option]
        with self.screen_session():
            self.launch()
            climate = self.open_climate()
            x, y = self.described_node_center_any(
                climate, ("Einstellungen", "Settings")
            )
            self.shell("input", "tap", str(x), str(y))
            if page == "zones":
                _root, (x, y) = self.wait_for_described_node(
                    "vw-option-settings.xml", ("Zonen", "Zones")
                )
                self.shell("input", "tap", str(x), str(y))
                _root, switch = self.wait_for_checked_option(
                    "vw-option-zones.xml", labels
                )
            else:
                _root, switch = self.wait_for_checked_option(
                    "vw-option-settings.xml", labels
                )
            current = switch.attrib.get("checked") == "true"
            if current != desired:
                center = self.node_center(switch)
                assert center is not None
                self.shell("input", "tap", str(center[0]), str(center[1]))
                _root, verify = self.wait_for_checked_option(
                    "vw-option-verify.xml", labels, desired
                )
            return desired

    @staticmethod
    def percentage_value(node: ET.Element) -> int | None:
        value = node.attrib.get("text", "").strip()
        match = re.fullmatch(r"(\d{1,3})\s*%", value)
        return int(match.group(1)) if match else None

    @classmethod
    def setting_values(cls, root: ET.Element) -> list[tuple[ET.Element, int]]:
        values: list[tuple[ET.Element, int]] = []
        for node in cls.resource_nodes(root, "/value"):
            value = cls.percentage_value(node)
            if value is not None:
                values.append((node, value))
        return sorted(
            values,
            key=lambda item: (cls.node_center(item[0]) or (0, 0))[1],
        )

    def wait_for_settings_values(
        self, remote_name: str, minimum: int = 1
    ) -> ET.Element:
        deadline = time.monotonic() + self.ui_update_timeout
        while True:
            root = self.dump_ui_with_overlay_recovery(remote_name)
            if len(self.setting_values(root)) >= minimum:
                return root
            if time.monotonic() >= deadline:
                raise RuntimeError("Volkswagen charging settings did not finish loading")
            time.sleep(0.5)

    def find_overview_element(
        self,
        root: ET.Element,
        labels: tuple[str, ...],
        resource_suffix: str = "",
    ) -> tuple[ET.Element, tuple[int, int]]:
        for attempt in range(20):
            try:
                if resource_suffix:
                    return root, self.resource_node_center(root, resource_suffix)
                return root, self.described_node_center_any(root, labels)
            except RuntimeError:
                try:
                    return root, self.described_node_center_any(root, labels)
                except RuntimeError:
                    if attempt == 19:
                        raise
                    width, height = self.viewport_size(root)
                    card_centers = [
                        center
                        for node in root.iter()
                        if (
                            "details öffnen" in node.attrib.get("content-desc", "").casefold()
                            or "open details" in node.attrib.get("content-desc", "").casefold()
                        )
                        and (center := self.node_center(node)) is not None
                    ]
                    swipe_x, swipe_y = (
                        max(card_centers, key=lambda center: center[1])
                        if card_centers
                        else (width // 2, round(height * 0.6))
                    )
                    self.shell(
                        "input", "swipe", str(swipe_x), str(swipe_y),
                        str(swipe_x), str(max(200, swipe_y - round(height * 0.3))),
                        "800",
                    )
                    time.sleep(1.5)
                    root = self.dump_ui_with_overlay_recovery(
                        "vw-overview-scroll.xml"
                    )
        raise RuntimeError("Volkswagen overview element not found")

    def open_charging_settings(self) -> ET.Element:
        overview = self.open_overview()
        _overview, (x, y) = self.find_overview_element(
            overview,
            (
                "Ladeeinstellungen.",
                "Einstellungen.",
                "Charging settings.",
                "Settings. Open details",
            ),
            "settingsTile",
        )
        self.shell("input", "tap", str(x), str(y))
        return self.wait_for_settings_values("vw-charging-settings.xml")

    def save_settings(self, root: ET.Element) -> None:
        for attempt in range(3):
            try:
                x, y = self.resource_node_center(root, "/vwd_save_button")
                break
            except RuntimeError:
                try:
                    x, y = self.described_node_center_any(root, ("Speichern", "Save"))
                    break
                except RuntimeError:
                    if attempt == 2:
                        raise RuntimeError("Volkswagen charging settings save button not found")
                    width, height = self.viewport_size(root)
                    self.shell(
                        "input", "swipe", str(width // 2), str(round(height * 0.8)),
                        str(width // 2), str(round(height * 0.45)), "300",
                    )
                    root = self.dump_ui("vw-settings-save-scroll.xml")
        self.shell("input", "tap", str(x), str(y))
        time.sleep(self.detail_wait)

    def dismiss_setting_notice(self, root: ET.Element) -> ET.Element:
        try:
            x, y = self.described_node_center_any(
                root, ("Verstanden", "Alles klar", "Got it", "OK")
            )
        except RuntimeError:
            return root
        self.shell("input", "tap", str(x), str(y))
        time.sleep(0.5)
        return self.dump_ui("vw-setting-notice-dismissed.xml")

    def set_percentage_slider(
        self,
        root: ET.Element,
        index: int,
        desired: int,
        allowed: tuple[int, ...],
        remote_name: str,
    ) -> ET.Element:
        if desired not in allowed:
            raise ValueError(f"value must be one of {list(allowed)}")
        values = self.setting_values(root)
        if index >= len(values):
            raise RuntimeError("Volkswagen charging percentage control not found")
        value_node, current = values[index]
        if current == desired:
            return root

        subtitles = self.resource_nodes(root, "/subtitle")
        value_bounds = self.node_bounds(value_node)
        if not value_bounds:
            raise RuntimeError("Volkswagen charging slider geometry not found")
        value_center = self.node_center(value_node)
        assert value_center is not None
        nearby = [
            node
            for node in subtitles
            if (self.node_center(node) or (0, -10000))[1] <= value_center[1]
        ]
        anchor = min(
            nearby or subtitles,
            key=lambda node: abs((self.node_center(node) or (0, 0))[1] - value_center[1]),
        ) if (nearby or subtitles) else None
        anchor_bounds = self.node_bounds(anchor) if anchor is not None else None
        if not anchor_bounds:
            raise RuntimeError("Volkswagen charging slider anchor not found")

        low_x = anchor_bounds[0]
        high_x = value_bounds[0] - 5
        # The Compose slider track is rendered slightly below the midpoint
        # between its subtitle and value nodes on the verified VW layout.
        track_y = (anchor_bounds[3] + value_bounds[1]) // 2 + 7
        for _attempt in range(14):
            x = (low_x + high_x) // 2
            self.shell("input", "tap", str(x), str(track_y))
            time.sleep(0.8)
            root = self.dismiss_setting_notice(self.dump_ui(remote_name))
            values = self.setting_values(root)
            if index >= len(values):
                continue
            current = values[index][1]
            if current == desired:
                return root
            if current < desired:
                low_x = x + 1
            else:
                high_x = x - 1
        raise RuntimeError(
            f"Volkswagen charging percentage verification failed: {desired}%"
        )

    @classmethod
    def option_state(cls, root: ET.Element, labels: tuple[str, ...]) -> bool:
        return cls.checked_node_near_labels(root, labels).attrib.get("checked") == "true"

    def checked_option_with_scroll(
        self, root: ET.Element, labels: tuple[str, ...], remote_name: str
    ) -> tuple[ET.Element, ET.Element]:
        for attempt in range(3):
            try:
                return root, self.checked_node_near_labels(root, labels)
            except RuntimeError:
                if attempt == 2:
                    raise
                width, height = self.viewport_size(root)
                self.shell(
                    "input", "swipe", str(width // 2), str(round(height * 0.8)),
                    str(width // 2), str(round(height * 0.45)), "300",
                )
                root = self.dump_ui_with_overlay_recovery(remote_name)
        raise RuntimeError("Volkswagen charging option not found")

    def read_charging_settings(self, root: ET.Element) -> ChargingSettingsData:
        values = self.setting_values(root)
        result = ChargingSettingsData(targetSoc=values[0][1] if values else None)
        for attribute, labels in (
            ("batteryCare", self.BATTERY_CARE_LABELS),
            ("reducedAc", self.REDUCED_AC_LABELS),
            ("autoReleaseAcConnector", self.AUTO_RELEASE_AC_LABELS),
        ):
            try:
                setattr(result, attribute, self.option_state(root, labels))
            except RuntimeError:
                pass
        return result

    def set_target_soc(self, desired: int) -> ChargingSettingsData:
        with self.screen_session():
            self.launch()
            root = self.open_charging_settings()
            current = self.read_charging_settings(root)
            if current.targetSoc == desired:
                return current
            root = self.set_percentage_slider(
                root, 0, desired, (50, 60, 70, 80, 90, 100),
                "vw-target-soc-adjust.xml",
            )
            result = self.read_charging_settings(root)
            for attribute, labels in (
                ("batteryCare", self.BATTERY_CARE_LABELS),
                ("reducedAc", self.REDUCED_AC_LABELS),
                ("autoReleaseAcConnector", self.AUTO_RELEASE_AC_LABELS),
            ):
                if getattr(result, attribute) is not None:
                    continue
                try:
                    root, switch = self.checked_option_with_scroll(
                        root, labels, "vw-target-soc-settings-scroll.xml"
                    )
                except RuntimeError:
                    continue
                else:
                    setattr(result, attribute, switch.attrib.get("checked") == "true")
            self.save_settings(root)
            return result

    def get_charging_settings(self) -> ChargingSettingsData:
        with self.screen_session():
            self.launch()
            root = self.open_charging_settings()
            result = self.read_charging_settings(root)
            for attribute, labels in (
                ("batteryCare", self.BATTERY_CARE_LABELS),
                ("reducedAc", self.REDUCED_AC_LABELS),
                ("autoReleaseAcConnector", self.AUTO_RELEASE_AC_LABELS),
            ):
                if getattr(result, attribute) is not None:
                    continue
                try:
                    root, switch = self.checked_option_with_scroll(
                        root, labels, "vw-charging-settings-read-scroll.xml"
                    )
                except RuntimeError:
                    continue
                else:
                    setattr(result, attribute, switch.attrib.get("checked") == "true")
            return result

    def set_charging_option(self, option: str, desired: bool) -> ChargingSettingsData:
        specs = {
            "battery-care": ("batteryCare", self.BATTERY_CARE_LABELS),
            "reduced-ac": ("reducedAc", self.REDUCED_AC_LABELS),
            "auto-release-ac": (
                "autoReleaseAcConnector",
                self.AUTO_RELEASE_AC_LABELS,
            ),
        }
        if option not in specs:
            raise KeyError(option)
        attribute, labels = specs[option]
        with self.screen_session():
            self.launch()
            root = self.open_charging_settings()
            result = self.read_charging_settings(root)
            root, switch = self.checked_option_with_scroll(
                root, labels, "vw-charging-option-scroll.xml"
            )
            current = switch.attrib.get("checked") == "true"
            if current != desired:
                center = self.node_center(switch)
                assert center is not None
                self.shell("input", "tap", str(center[0]), str(center[1]))
                self.dismiss_setting_notice(
                    self.dump_ui_with_overlay_recovery(
                        "vw-charging-option-notice.xml"
                    )
                )
                root, _switch = self.wait_for_checked_option(
                    "vw-charging-option-verify.xml", labels, desired
                )
                self.save_settings(root)
            setattr(result, attribute, desired)
            return result

    def set_charging_mode(self, mode: str) -> str:
        modes = {
            "immediate": ("Sofortladen", "Immediate charging"),
            "preferred-times": ("Zu bevorzugten Zeiten laden", "Charge at preferred times"),
            "departure": ("Zur Abfahrtszeit laden", "Charge for departure time"),
            "departure-climate": (
                "Zur Abfahrtszeit laden und klimatisieren",
                "Charge/air condition for departure",
            ),
        }
        if mode not in modes:
            raise ValueError(f"mode must be one of {list(modes)}")
        with self.screen_session():
            self.launch()
            overview = self.open_overview()
            x, y = self.range_tile_center(overview)
            self.shell("input", "tap", str(x), str(y))
            time.sleep(self.detail_wait)
            detail = self.wait_for_described_node(
                "vw-charge-mode-current.xml",
                (
                    "Ladeverfahren",
                    "Charging mode",
                    "Charging method",
                ),
            )[0]
            x, y = self.described_node_center_any(
                detail,
                (
                    "Ladeverfahren",
                    "Charging mode",
                    "Charging method",
                ),
            )
            self.shell("input", "tap", str(x), str(y))
            choices, (x, y) = self.wait_for_described_node(
                "vw-charge-mode-choices.xml", modes[mode]
            )
            self.shell("input", "tap", str(x), str(y))
            time.sleep(self.detail_wait)
            verify = self.dump_ui("vw-charge-mode-verify.xml")
            text = "\n".join(self.strings(verify))
            if not any(label.casefold() in text.casefold() for label in modes[mode]):
                raise RuntimeError("Volkswagen charging mode verification failed")
            return mode

    def open_charging_location(self, name: str) -> ET.Element:
        overview = self.open_overview()
        _overview, (x, y) = self.find_overview_element(
            overview,
            ("Abfahrtszeiten.", "Departure times."),
            "departureTimesTile",
        )
        self.shell("input", "tap", str(x), str(y))
        departures = self.dump_ui("vw-location-departures.xml")
        x, y = self.described_node_center_any(departures, (name,))
        self.shell("input", "tap", str(x), str(y))
        location = self.dump_ui("vw-location-selected.xml")
        try:
            x, y = self.resource_node_center(location, "/vwd_setting_button")
        except RuntimeError:
            x, y = self.described_node_center_any(
                location, ("Ladeeinstellungen", "Charging settings", "Settings")
            )
        self.shell("input", "tap", str(x), str(y))
        return self.wait_for_settings_values("vw-location-settings.xml", 2)

    def list_charging_locations(self) -> ChargingLocationsData:
        with self.screen_session():
            self.launch()
            overview = self.open_overview()
            _overview, (x, y) = self.find_overview_element(
                overview,
                ("Abfahrtszeiten.", "Departure times."),
                "departureTimesTile",
            )
            self.shell("input", "tap", str(x), str(y))
            root = self.dump_ui("vw-location-list.xml")
            names = [
                node.attrib.get("text", "").strip()
                for node in self.resource_nodes(root, "/name")
                if node.attrib.get("text", "").strip()
            ]
            return ChargingLocationsData(locations=list(dict.fromkeys(names)))

    def get_charging_location_settings(
        self, name: str
    ) -> ChargingLocationSettingsData:
        with self.screen_session():
            self.launch()
            root = self.open_charging_location(name)
            result = self.read_charging_location_settings(name, root)
            for attribute, labels in (
                ("reducedAc", ("Reduzierter AC-Ladestrom", "Reduced AC current")),
                ("autoUnlock", ("Automatisch entriegeln", "Automatic unlock", "Auto unlock")),
            ):
                if getattr(result, attribute) is not None:
                    continue
                root, switch = self.checked_option_with_scroll(
                    root, labels, "vw-location-settings-read-scroll.xml"
                )
                setattr(result, attribute, switch.attrib.get("checked") == "true")
            return result

    def read_charging_location_settings(
        self, name: str, root: ET.Element
    ) -> ChargingLocationSettingsData:
        values = self.setting_values(root)
        result = ChargingLocationSettingsData(
            name=name,
            directSoc=values[0][1] if len(values) > 0 else None,
            targetSoc=values[1][1] if len(values) > 1 else None,
        )
        for attribute, labels in (
            ("reducedAc", ("Reduzierter AC-Ladestrom", "Reduced AC current")),
            ("autoUnlock", ("Automatisch entriegeln", "Automatic unlock", "Auto unlock")),
        ):
            try:
                setattr(result, attribute, self.option_state(root, labels))
            except RuntimeError:
                pass
        return result

    def set_charging_location_percentage(
        self, name: str, kind: str, desired: int
    ) -> ChargingLocationSettingsData:
        specs = {
            "direct-soc": (0, (0, 10, 20, 30, 40, 50)),
            "target-soc": (1, (50, 60, 70, 80, 90, 100)),
        }
        if kind not in specs:
            raise KeyError(kind)
        index, allowed = specs[kind]
        with self.screen_session():
            self.launch()
            root = self.open_charging_location(name)
            current = self.read_charging_location_settings(name, root)
            if getattr(current, "directSoc" if index == 0 else "targetSoc") == desired:
                return current
            root = self.set_percentage_slider(
                root, index, desired, allowed, "vw-location-soc-adjust.xml"
            )
            self.save_settings(root)
            result = self.read_charging_location_settings(name, root)
            if index == 0:
                result.previousDirectSoc = current.directSoc
            else:
                result.previousTargetSoc = current.targetSoc
            return result

    def set_charging_location_option(
        self, name: str, option: str, desired: bool
    ) -> ChargingLocationSettingsData:
        specs = {
            "reduced-ac": ("Reduzierter AC-Ladestrom", "Reduced AC current"),
            "auto-unlock": ("Automatisch entriegeln", "Automatic unlock", "Auto unlock"),
        }
        if option not in specs:
            raise KeyError(option)
        labels = specs[option]
        with self.screen_session():
            self.launch()
            root = self.open_charging_location(name)
            result = self.read_charging_location_settings(name, root)
            root, switch = self.checked_option_with_scroll(
                root, labels, "vw-location-option-scroll.xml"
            )
            current = switch.attrib.get("checked") == "true"
            if current != desired:
                center = self.node_center(switch)
                assert center is not None
                self.shell("input", "tap", str(center[0]), str(center[1]))
                self.dismiss_setting_notice(
                    self.dump_ui_with_overlay_recovery(
                        "vw-location-option-notice.xml"
                    )
                )
                root, _switch = self.wait_for_checked_option(
                    "vw-location-option-verify.xml", labels, desired
                )
                self.save_settings(root)
            setattr(result, "reducedAc" if option == "reduced-ac" else "autoUnlock", desired)
            return result


class BackgroundCache(Generic[T]):
    def __init__(
        self,
        name: str,
        loader: Callable[[], T],
        interval: Callable[[T | None], float],
        empty_factory: Callable[[], T],
        initial_delay: float = 0,
        error_retry_interval: float = 900,
        state_path: Path | None = None,
        on_update: Callable[[str, T], None] | None = None,
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
        self.next_attempt_monotonic = 0.0
        self.wakeup = threading.Event()
        self.initial_delay = initial_delay
        self.error_retry_interval = error_retry_interval
        self.state_path = state_path
        self.on_update = on_update
        self._load_persisted()
        threading.Thread(target=self._worker, name=f"{name}-refresh", daemon=True).start()

    def _load_persisted(self) -> None:
        if self.state_path is None:
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            raw_value = payload["value"]
            value = self.empty_factory()
            for key in vars(value):
                if key in raw_value:
                    setattr(value, key, raw_value[key])
            last_success_at = str(getattr(value, "lastSuccessfulAt", ""))
            saved_at = datetime.fromisoformat(last_success_at)
            age = max(
                0.0,
                (datetime.now().astimezone() - saved_at).total_seconds(),
            )
            self.value = value
            self.last_success_at = last_success_at
            self.last_success_monotonic = time.monotonic() - age
            setattr(value, "stale", age >= self.interval(value))
            setattr(value, "error", "")
            setattr(value, "errorCategory", "")
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            return

    def _save_persisted(self, value: T) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "value": asdict(value)}),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(self.state_path)

    def _worker(self) -> None:
        if self.initial_delay and self.value is None:
            time.sleep(self.initial_delay)
            self.wakeup.clear()
        while True:
            now = time.monotonic()
            with self.lock:
                retry_wait = max(0.0, self.next_attempt_monotonic - now)
            due = self.value is None or self.age() >= self.interval(self.value)
            if due and retry_wait <= 0:
                self.refresh()
            if retry_wait > 0:
                delay = max(5.0, retry_wait)
            else:
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
                self.next_attempt_monotonic = 0.0
            self._save_persisted(value)
            if self.on_update is not None:
                self.on_update(self.name, value)
            return value
        except ActionPriority:
            LOG.info("%s refresh yielded to a pending action", self.name)
            self.trigger()
            with self.lock:
                return self.value or self.empty_factory()
        except Exception as exc:
            category = VolkswagenReader.error_category(exc)
            if isinstance(exc, UsageLimit):
                LOG.warning("%s refresh skipped: %s", self.name, exc)
            else:
                LOG.exception("%s refresh failed", self.name)
            with self.lock:
                self.last_error = str(exc)
                self.last_error_category = category
                self.next_attempt_monotonic = (
                    time.monotonic() + self.error_retry_interval
                )
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
        self._save_persisted(value)
        if self.on_update is not None:
            self.on_update(self.name, value)
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


class ActionJobManager:
    def __init__(
        self,
        executor: Callable[[str, dict[str, list[str]]], object],
        max_history: int = 100,
    ) -> None:
        self.executor = executor
        self.max_history = max_history
        self.jobs: dict[str, dict[str, object]] = {}
        self.order: list[str] = []
        self.idempotency: dict[str, tuple[str, str]] = {}
        self.lock = threading.Lock()
        self.pending: queue.Queue[tuple[str, str, dict[str, list[str]]]] = queue.Queue()
        threading.Thread(
            target=self._worker,
            name="action-job-worker",
            daemon=True,
        ).start()

    @staticmethod
    def now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def submit(
        self,
        action: str,
        query: dict[str, list[str]],
        idempotency_key: str = "",
    ) -> dict[str, object]:
        signature = json.dumps(
            [action, {key: list(values) for key, values in sorted(query.items())}],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self.lock:
            existing = self.idempotency.get(idempotency_key) if idempotency_key else None
            if existing is not None:
                job_id, existing_signature = existing
                if signature != existing_signature:
                    raise IdempotencyConflict(
                        "Idempotency-Key was already used for another action"
                    )
                job = self.jobs.get(job_id)
                if job is not None:
                    return json.loads(json.dumps(job, ensure_ascii=False))
        job_id = uuid.uuid4().hex
        job: dict[str, object] = {
            "jobId": job_id,
            "action": action,
            "state": "queued",
            "createdAt": self.now(),
            "startedAt": "",
            "completedAt": "",
            "result": None,
            "error": "",
            "errorCategory": "",
        }
        with self.lock:
            self.jobs[job_id] = job
            self.order.append(job_id)
            if idempotency_key:
                self.idempotency[idempotency_key] = (job_id, signature)
            while len(self.order) > self.max_history:
                expired = next(
                    (
                        candidate
                        for candidate in self.order
                        if self.jobs[candidate]["state"] in ("succeeded", "failed")
                    ),
                    None,
                )
                if expired is None:
                    break
                self.order.remove(expired)
                self.jobs.pop(expired, None)
                for key, value in list(self.idempotency.items()):
                    if value[0] == expired:
                        self.idempotency.pop(key, None)
            submitted = json.loads(json.dumps(job, ensure_ascii=False))
        copied_query = {key: list(values) for key, values in query.items()}
        self.pending.put((job_id, action, copied_query))
        return submitted

    def snapshot(self, job_id: str) -> dict[str, object] | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            return json.loads(json.dumps(job, ensure_ascii=False))

    def _update(self, job_id: str, **values: object) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None:
                job.update(values)

    def _worker(self) -> None:
        while True:
            job_id, action, query = self.pending.get()
            self._update(job_id, state="running", startedAt=self.now())
            try:
                value = self.executor(action, query)
                result = asdict(value) if is_dataclass(value) else value
                self._update(
                    job_id,
                    state="succeeded",
                    completedAt=self.now(),
                    result=result,
                )
            except Exception as exc:
                category = (
                    "APP_VERSION"
                    if isinstance(exc, ActionQuarantined)
                    else "RATE_LIMIT"
                    if isinstance(exc, UsageLimit)
                    else VolkswagenReader.error_category(exc)
                )
                self._update(
                    job_id,
                    state="failed",
                    completedAt=self.now(),
                    error=str(exc),
                    errorCategory=category,
                )
            finally:
                self.pending.task_done()


class AppState:
    READ_ONLY_ACTIONS = {
        "charging/settings",
        "charging-location/settings",
        "charging-locations",
    }
    SUPPORTED_ACTIONS = (
        "lock",
        "unlock",
        "charging/start",
        "charging/stop",
        "charging/target-soc",
        "charging/mode",
        "charging/settings",
        "charging/option/battery-care",
        "charging/option/reduced-ac",
        "charging/option/auto-release-ac",
        "charging-location/direct-soc",
        "charging-location/target-soc",
        "charging-location/settings",
        "charging-location/option/reduced-ac",
        "charging-location/option/auto-unlock",
        "charging-locations",
        "climate/start",
        "climate/stop",
        "climate/temperature",
        "climate/option/automatic-window-heating",
        "climate/option/zone-front-left",
        "climate/option/zone-front-right",
    )

    def __init__(self) -> None:
        self.mqtt: MqttPublisher | None = None
        self.reader = VolkswagenReader()
        self.usage = UsageLimiter()
        self.verified_app_version = os.getenv(
            "VERIFIED_APP_VERSION", "4.0.3"
        ).strip()
        self.priority_lock = threading.Lock()
        self.priority_waiters = 0
        charging_interval = float(os.getenv("CHARGING_INTERVAL_SECONDS", "300"))
        idle_interval = float(os.getenv("IDLE_INTERVAL_SECONDS", "900"))
        detail_interval = float(os.getenv("DETAIL_INTERVAL_SECONDS", "43200"))
        location_interval = float(os.getenv("LOCATION_INTERVAL_SECONDS", "14400"))
        error_retry_interval = float(
            os.getenv("BACKGROUND_ERROR_RETRY_SECONDS", "900")
        )
        cache_dir = Path(
            os.getenv("CACHE_STATE_DIR", "/var/lib/vw-app-connector/cache")
        )

        def priority_pending() -> bool:
            with self.priority_lock:
                return self.priority_waiters > 0

        def background(
            loader: Callable[[], T], cost: int, priority: bool = False
        ) -> Callable[[], T]:
            def run() -> T:
                if priority:
                    with self.priority_lock:
                        self.priority_waiters += 1
                try:
                    while self.reader.action_pending.is_set():
                        time.sleep(0.25)
                    self.usage.acquire_background(
                        cost,
                        yield_to=(
                            None
                            if priority
                            else priority_pending
                        ),
                    )
                    self.reader.context.background = True
                    try:
                        return loader()
                    except UsageLimit as exc:
                        if "reports " in str(exc):
                            self.usage.record_rate_limit()
                        raise
                    finally:
                        self.reader.context.background = False
                finally:
                    if priority:
                        with self.priority_lock:
                            self.priority_waiters -= 1

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
            state_path=cache_dir / "charge.json",
            on_update=self._cache_updated,
        )
        self.details = BackgroundCache(
            "details",
            background(self.reader.read_details, 3, priority=True),
            lambda _: detail_interval,
            DetailData,
            initial_delay=600,
            error_retry_interval=error_retry_interval,
            state_path=cache_dir / "details.json",
            on_update=self._cache_updated,
        )
        self.location = BackgroundCache(
            "location",
            background(self.reader.read_location, 1, priority=True),
            lambda _: location_interval,
            LocationData,
            initial_delay=300,
            error_retry_interval=error_retry_interval,
            state_path=cache_dir / "location.json",
            on_update=self._cache_updated,
        )

        self.mqtt = MqttPublisher.from_environment(self.mqtt_state)
        if self.mqtt is not None:
            self.mqtt.start()
        self.action_jobs = ActionJobManager(self.action)

    def _cache_updated(self, name: str, value: object) -> None:
        if self.mqtt is None:
            return
        try:
            self.mqtt.publish_state(name, value)
            if name == "charge":
                self.mqtt.publish_state("health", self.health())
        except Exception:
            LOG.exception("MQTT update failed for %s", name)

    def mqtt_state(self) -> dict[str, object]:
        return {
            "charge": self.charge.value,
            "details": self.details.value,
            "location": self.location.value,
            "health": self.health(),
        }

    @staticmethod
    def _cache_snapshot(cache: BackgroundCache[object]) -> dict[str, object]:
        with cache.lock:
            value = cache.value
            last_success_at = cache.last_success_at
            last_error_category = cache.last_error_category
            refreshing = cache.refreshing
            next_attempt = cache.next_attempt_monotonic
        return {
            "available": value is not None and bool(last_success_at),
            "lastSuccessfulAt": last_success_at,
            "ageSeconds": round(cache.age()) if last_success_at else None,
            "stale": bool(getattr(value, "stale", False)) if value is not None else False,
            "refreshing": refreshing,
            "lastErrorCategory": last_error_category,
            "retryInSeconds": (
                max(0, round(next_attempt - time.monotonic()))
                if next_attempt
                else 0
            ),
        }

    def capabilities(self) -> dict[str, object]:
        health = self.health()
        supported = list(self.SUPPORTED_ACTIONS)
        read_only = [name for name in supported if not self.is_write_action(name)]
        write = [name for name in supported if self.is_write_action(name)]
        return {
            "version": 1,
            "status": health.status,
            "readEndpoints": {
                "charge": True,
                "details": True,
                "location": True,
                "health": True,
                "capabilities": True,
                "metrics": True,
                "diagnostics": True,
            },
            "features": {
                "adbMode": health.adbMode,
                "adbTransport": health.adbTransport,
                "adbWifiFallbackConfigured": health.adbWifiConfigured,
                "appVersion": health.appVersion,
                "verifiedAppVersion": health.verifiedAppVersion,
                "appVersionVerified": health.appVersionVerified,
                "mqtt": self.mqtt is not None,
                "cachePersistence": True,
                "asyncActions": True,
                "diagnosticsIndex": True,
                "germanLocalization": True,
                "englishLocalization": True,
            },
            "actions": {
                "available": health.actionAvailable,
                "blockedReason": health.actionBlockedReason,
                "readOnlyAvailable": True,
                "supported": supported,
                "readOnly": read_only,
                "write": write,
            },
            "caches": {
                "charge": self._cache_snapshot(self.charge),  # type: ignore[arg-type]
                "details": self._cache_snapshot(self.details),  # type: ignore[arg-type]
                "location": self._cache_snapshot(self.location),  # type: ignore[arg-type]
            },
            "usage": self.usage.snapshot(),
        }

    @staticmethod
    def _metric_label(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    def metrics_text(self) -> str:
        health = self.health()
        usage = self.usage.snapshot()
        caches = {
            "charge": self.charge,
            "details": self.details,
            "location": self.location,
        }
        lines = [
            "# HELP vw_app_connector_up Connector health status, 1 when not error.",
            "# TYPE vw_app_connector_up gauge",
            f'vw_app_connector_up{{status="{self._metric_label(health.status)}"}} '
            f"{1 if health.status != 'error' else 0}",
            "# HELP vw_app_connector_action_available Write actions allowed by the app-version guard.",
            "# TYPE vw_app_connector_action_available gauge",
            f"vw_app_connector_action_available {1 if health.actionAvailable else 0}",
            "# HELP vw_app_connector_usage_used Current local-day usage counter.",
            "# TYPE vw_app_connector_usage_used gauge",
            f'vw_app_connector_usage_used{{kind="background"}} {usage["backgroundUsed"]}',
            f'vw_app_connector_usage_used{{kind="actions"}} {usage["actionsUsed"]}',
            "# HELP vw_app_connector_usage_limit Current local-day usage limit.",
            "# TYPE vw_app_connector_usage_limit gauge",
            f'vw_app_connector_usage_limit{{kind="background"}} {usage["backgroundLimit"]}',
            f'vw_app_connector_usage_limit{{kind="actions"}} {usage["actionsLimit"]}',
            "# HELP vw_app_connector_cooldown_seconds Active Volkswagen rate-limit cooldown.",
            "# TYPE vw_app_connector_cooldown_seconds gauge",
            f'vw_app_connector_cooldown_seconds {usage["cooldownSeconds"]}',
            "# HELP vw_app_connector_phone_battery_level_percent Android phone battery level.",
            "# TYPE vw_app_connector_phone_battery_level_percent gauge",
        ]
        if health.phoneBatteryLevel is not None:
            lines.append(
                f"vw_app_connector_phone_battery_level_percent {health.phoneBatteryLevel}"
            )
        lines.extend(
            [
                "# HELP vw_app_connector_cache_age_seconds Seconds since a cache last refreshed successfully.",
                "# TYPE vw_app_connector_cache_age_seconds gauge",
            ]
        )
        for name, cache in caches.items():
            with cache.lock:
                last_success_at = cache.last_success_at
                refreshing = cache.refreshing
                last_error_category = cache.last_error_category
            if last_success_at:
                lines.append(
                    f'vw_app_connector_cache_age_seconds{{cache="{name}"}} '
                    f"{round(cache.age())}"
                )
            lines.append(
                f'vw_app_connector_cache_refreshing{{cache="{name}"}} '
                f"{1 if refreshing else 0}"
            )
            lines.append(
                f'vw_app_connector_cache_error{{cache="{name}",category="'
                f'{self._metric_label(last_error_category)}"}} '
                f"{1 if last_error_category else 0}"
            )
        lines.extend(
            [
                "# HELP vw_app_connector_adb_transport Current ADB transport selected by the connector.",
                "# TYPE vw_app_connector_adb_transport gauge",
                f'vw_app_connector_adb_transport{{transport="{self._metric_label(health.adbTransport)}"}} 1',
                "# HELP vw_app_connector_app_version_info Installed and verified Volkswagen app versions.",
                "# TYPE vw_app_connector_app_version_info gauge",
                "vw_app_connector_app_version_info{"
                f'app_version="{self._metric_label(health.appVersion)}",'
                f'verified_app_version="{self._metric_label(health.verifiedAppVersion)}",'
                f'verified="{str(health.appVersionVerified).lower()}"'
                "} 1",
            ]
        )
        return "\n".join(lines) + "\n"

    def diagnostics_index(self, limit: int = 20) -> dict[str, object]:
        groups: dict[str, dict[str, object]] = {}
        try:
            files = sorted(
                self.reader.diagnostics_dir.glob("*"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            files = []
        for path in files:
            if path.suffix not in (".txt", ".xml", ".png"):
                continue
            stem = path.stem
            if "-" not in stem:
                continue
            _stamp, category = stem.rsplit("-", 1)
            group = groups.setdefault(
                stem,
                {
                    "id": stem,
                    "category": category.upper(),
                    "createdAt": "",
                    "artifacts": {
                        "summary": False,
                        "uiDump": False,
                        "screenshot": False,
                    },
                    "errorType": "",
                    "artifactBytes": 0,
                },
            )
            try:
                modified = datetime.fromtimestamp(
                    path.stat().st_mtime
                ).astimezone().isoformat(timespec="seconds")
                if not group["createdAt"] or modified < group["createdAt"]:
                    group["createdAt"] = modified
                group["artifactBytes"] = int(group["artifactBytes"]) + path.stat().st_size
            except OSError:
                pass
            artifacts = group["artifacts"]
            assert isinstance(artifacts, dict)
            if path.suffix == ".txt":
                artifacts["summary"] = True
                try:
                    first_line = path.read_text(encoding="utf-8").splitlines()[0]
                    group["errorType"] = first_line.split(":", 1)[0][:80]
                except (IndexError, OSError, UnicodeDecodeError):
                    pass
            elif path.suffix == ".xml":
                artifacts["uiDump"] = True
            elif path.suffix == ".png":
                artifacts["screenshot"] = True
        entries = sorted(
            groups.values(),
            key=lambda item: str(item.get("createdAt", "")),
            reverse=True,
        )[:limit]
        return {
            "diagnosticsDirConfigured": True,
            "count": len(entries),
            "entries": entries,
        }

    @staticmethod
    def version_policy(
        app_version: str, verified_app_version: str
    ) -> tuple[bool, str]:
        if not verified_app_version:
            return True, ""
        if not app_version:
            return False, "APP_VERSION_UNKNOWN"
        if app_version != verified_app_version:
            return False, "UNVERIFIED_APP_VERSION"
        return True, ""

    @classmethod
    def is_write_action(cls, name: str) -> bool:
        return name not in cls.READ_ONLY_ACTIONS

    @staticmethod
    def supports_action(name: str) -> bool:
        return (
            name in AppState.SUPPORTED_ACTIONS
            or name.startswith("climate/option/")
            or name.startswith("charging/option/")
            or name.startswith("charging-location/option/")
        )

    def ensure_action_allowed(self, name: str, app_version: str | None = None) -> None:
        if not self.is_write_action(name):
            return
        actual = (
            app_version
            if app_version is not None
            else self.reader.phone_health().appVersion
        )
        available, reason = self.version_policy(
            actual, self.verified_app_version
        )
        if not available:
            raise ActionQuarantined(
                reason,
                actual,
                self.verified_app_version,
            )

    def submit_action(
        self,
        name: str,
        query: dict[str, list[str]],
        idempotency_key: str = "",
    ) -> dict[str, object]:
        if not self.supports_action(name):
            raise KeyError(name)
        self.ensure_action_allowed(name)
        return self.action_jobs.submit(name, query, idempotency_key)

    def action_job(self, job_id: str) -> dict[str, object] | None:
        return self.action_jobs.snapshot(job_id)

    def action(self, name: str, query: dict[str, list[str]]) -> object:
        if not self.supports_action(name):
            raise KeyError(name)
        self.ensure_action_allowed(name)
        self.reader.action_pending.set()
        try:
            self.usage.acquire_action()
            return self._action(name, query)
        except UsageLimit as exc:
            if "reports " in str(exc):
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
        if name == "charging/target-soc":
            settings = self.reader.set_target_soc(int(query["value"][0]))
            with self.charge.lock:
                current = self.charge.value
            if current is not None:
                self.charge.patch_value(
                    replace(current, targetSoc=settings.targetSoc)
                )
            return settings
        if name == "charging/mode":
            self.reader.set_charging_mode(query["value"][0])
            return self.charge.set_value(self.reader.read())
        if name == "charging/settings":
            return self.reader.get_charging_settings()
        if name.startswith("charging/option/"):
            desired = query["value"][0].casefold() in ("1", "true", "on")
            option = name.removeprefix("charging/option/")
            return self.reader.set_charging_option(option, desired)
        if name.startswith("charging-location/option/"):
            desired = query["value"][0].casefold() in ("1", "true", "on")
            option = name.removeprefix("charging-location/option/")
            return self.reader.set_charging_location_option(
                query["name"][0], option, desired
            )
        if name == "charging-location/settings":
            return self.reader.get_charging_location_settings(query["name"][0])
        if name.startswith("charging-location/"):
            kind = name.removeprefix("charging-location/")
            return self.reader.set_charging_location_percentage(
                query["name"][0], kind, int(query["value"][0])
            )
        if name == "charging-locations":
            return self.reader.list_charging_locations()
        raise KeyError(name)

    def health(self) -> HealthData:
        value = self.reader.phone_health()
        value.verifiedAppVersion = self.verified_app_version
        value.appVersionVerified, value.actionBlockedReason = self.version_policy(
            value.appVersion, self.verified_app_version
        )
        value.actionAvailable = value.appVersionVerified
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
        elif (
            self.charge.last_error
            or value.usageCooldownSeconds
            or not value.actionAvailable
        ):
            value.status = "degraded"
        return value


class RequestHandler(BaseHTTPRequestHandler):
    state: AppState
    api_key: str

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        job_prefix = "/actions/"
        if path.startswith(job_prefix):
            supplied_key = self.headers.get("X-API-Key", "")
            if not self.api_key or not hmac.compare_digest(
                supplied_key, self.api_key
            ):
                self.send_error(401)
                return
            job = self.state.action_job(path.removeprefix(job_prefix))
            if job is None:
                self.send_error(404)
                return
            self.send_json(job, 200)
            return
        if path == "/health":
            value = self.state.health()
            self.send_json(asdict(value), 200 if value.status != "error" else 503)
            return
        if path == "/capabilities":
            self.send_json(self.state.capabilities(), 200)
            return
        if path == "/metrics":
            self.send_text(
                self.state.metrics_text(),
                200,
                "text/plain; version=0.0.4; charset=utf-8",
            )
            return
        if path == "/diagnostics":
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = max(1, min(100, int(query.get("limit", ["20"])[0])))
            except ValueError:
                self.send_error(400)
                return
            self.send_json(self.state.diagnostics_index(limit), 200)
            return
        caches: dict[str, BackgroundCache[object]] = {
            "/charge": self.state.charge,  # type: ignore[dict-item]
            "/details": self.state.details,  # type: ignore[dict-item]
            "/location": self.state.location,  # type: ignore[dict-item]
        }
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
            query = parse_qs(parsed.query)
            prefer = self.headers.get("Prefer", "")
            respond_async = any(
                token.strip().casefold() == "respond-async"
                for token in prefer.split(",")
            )
            if respond_async:
                job = self.state.submit_action(
                    action,
                    query,
                    self.headers.get("Idempotency-Key", "").strip(),
                )
                job_id = str(job["jobId"])
                status_url = f"/actions/{job_id}"
                self.send_json(
                    {
                        "jobId": job_id,
                        "state": job["state"],
                        "statusUrl": status_url,
                    },
                    202,
                    {"Location": status_url},
                )
                return
            value = self.state.action(action, query)
        except KeyError:
            self.send_error(404)
            return
        except ActionQuarantined as exc:
            self.send_json(
                {
                    "error": "Vehicle actions are quarantined for this app version",
                    "errorCategory": "APP_VERSION",
                    "reason": exc.reason,
                    "appVersion": exc.app_version,
                    "verifiedAppVersion": exc.verified_version,
                },
                409,
            )
            return
        except IdempotencyConflict as exc:
            self.send_json(
                {
                    "error": str(exc),
                    "errorCategory": "IDEMPOTENCY",
                },
                409,
            )
            return
        except UsageLimit as exc:
            self.send_json({"error": str(exc), "errorCategory": "RATE_LIMIT"}, 429)
            return
        except Exception as exc:
            self.send_json({"error": str(exc)}, 503)
            return
        self.send_json(asdict(value), 200)

    def send_json(
        self,
        value: object,
        status: int,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, header_value in (headers or {}).items():
            self.send_header(name, header_value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def send_text(
        self,
        value: str,
        status: int,
        content_type: str,
    ) -> None:
        body = value.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
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
