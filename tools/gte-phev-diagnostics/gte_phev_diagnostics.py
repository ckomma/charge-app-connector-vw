#!/usr/bin/env python3
"""Collect read-only Volkswagen app UI diagnostics for GTE/PHEV support.

The script does not tap, swipe, sync, save or change vehicle settings. It asks
the tester to manually navigate to relevant Volkswagen app screens, then stores
sanitized UI hierarchy dumps and a compact summary that can be shared for
parser development.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


DEFAULT_PACKAGE = "com.volkswagen.weconnect"
DEFAULT_SCREENS = (
    "overview",
    "charging-overview",
    "charging-settings",
    "climate",
    "departure-times",
    "vehicle-details",
    "location-map",
)

SENSITIVE_RESOURCE_PATTERNS = (
    "address",
    "email",
    "fin",
    "location",
    "map",
    "phone",
    "pin",
    "route",
    "vin",
)

PHEV_KEYWORDS = (
    "battery",
    "batterie",
    "charge",
    "charging",
    "e-mode",
    "electric",
    "elektrisch",
    "fuel",
    "hybrid",
    "kraftstoff",
    "laden",
    "reichweite",
    "range",
    "tank",
    "vorklimatisierung",
    "departure",
    "abfahr",
)

TARGET_ANCHORS = (
    {
        "id": "rangeTile",
        "kind": "resource_suffix",
        "value": "rangeTile",
        "purpose": "Overview range tile; usually opens charge/range details.",
    },
    {
        "id": "rangeArcBatterySoc",
        "kind": "resource_suffix",
        "value": "rangeArcBatterySoc",
        "purpose": "Charge/range detail SoC node.",
    },
    {
        "id": "rangeArcRangeAndUnit",
        "kind": "resource_suffix",
        "value": "rangeArcRangeAndUnit",
        "purpose": "Charge/range detail electric range node.",
    },
    {
        "id": "chargingStatsTargetSoc",
        "kind": "resource_suffix",
        "value": "chargingStatsTargetSoc",
        "purpose": "Charge/range detail target SoC node.",
    },
    {
        "id": "subtitle",
        "kind": "resource_suffix",
        "value": "subtitle",
        "purpose": "Settings subtitle node, often adjacent to sliders.",
    },
    {
        "id": "vehicleCarPlus",
        "kind": "resource_suffix",
        "value": "vehicleCarPlus",
        "purpose": "Overview vehicle anchor used near lock/sync information.",
    },
    {
        "id": "climateTile",
        "kind": "resource_suffix",
        "value": "climateTile",
        "purpose": "Overview climate tile.",
    },
    {
        "id": "clima_compose_view",
        "kind": "resource_suffix",
        "value": "clima_compose_view",
        "purpose": "BEV-style climate temperature picker container.",
    },
    {
        "id": "hybridAuxTemperatureSlider",
        "kind": "resource_suffix",
        "value": "hybridAuxTemperatureSlider",
        "purpose": "Hybrid/PHEV climate temperature picker container.",
    },
    {
        "id": "cta_start",
        "kind": "resource_suffix",
        "value": "cta_start",
        "purpose": "Standard climate start button.",
    },
    {
        "id": "cta_stop",
        "kind": "resource_suffix",
        "value": "cta_stop",
        "purpose": "Standard climate stop button.",
    },
    {
        "id": "hybridAuxStart",
        "kind": "resource_suffix",
        "value": "hybridAuxStart",
        "purpose": "Hybrid/PHEV climate start button.",
    },
    {
        "id": "hybridAuxStop",
        "kind": "resource_suffix",
        "value": "hybridAuxStop",
        "purpose": "Hybrid/PHEV climate stop button.",
    },
    {
        "id": "settingsValue",
        "kind": "resource_suffix",
        "value": "value",
        "purpose": "Generic settings value node, often used for target SoC.",
    },
    {
        "id": "toggle",
        "kind": "resource_suffix",
        "value": "toggle",
        "purpose": "Generic settings toggle, e.g. Battery Care or reduced AC.",
    },
    {
        "id": "vwd_save_button",
        "kind": "resource_suffix",
        "value": "vwd_save_button",
        "purpose": "Settings save button.",
    },
    {
        "id": "batteryCare",
        "kind": "content_desc_contains",
        "value": "Battery Care",
        "purpose": "English Battery Care charging setting.",
    },
    {
        "id": "batteryCareGerman",
        "kind": "content_desc_contains",
        "value": "Batterieschon",
        "purpose": "German Battery Care charging setting.",
    },
    {
        "id": "reducedAc",
        "kind": "content_desc_contains",
        "value": "Reduced AC",
        "purpose": "English reduced AC charging setting.",
    },
    {
        "id": "reducedAcGerman",
        "kind": "content_desc_contains",
        "value": "reduzier",
        "purpose": "German reduced AC charging setting.",
    },
    {
        "id": "chargingMethod",
        "kind": "content_desc_contains",
        "value": "Charging method",
        "purpose": "English charging-method row.",
    },
    {
        "id": "chargingMethodGerman",
        "kind": "content_desc_contains",
        "value": "Ladeverfahren",
        "purpose": "German charging-method row.",
    },
    {
        "id": "departureTimes",
        "kind": "content_desc_contains",
        "value": "Departure",
        "purpose": "English departure-times navigation or screen text.",
    },
    {
        "id": "departureTimesGerman",
        "kind": "content_desc_contains",
        "value": "Abfahr",
        "purpose": "German departure-times navigation or screen text.",
    },
    {
        "id": "temperatureLo",
        "kind": "text_exact",
        "value": "LO",
        "purpose": "Climate picker lower boundary label.",
    },
    {
        "id": "temperatureHi",
        "kind": "text_exact",
        "value": "HI",
        "purpose": "Climate picker upper boundary label.",
    },
)


@dataclass(frozen=True)
class ScreenCapture:
    name: str
    raw_node_count: int
    sanitized_node_count: int
    strings: list[str]
    keyword_strings: list[str]
    targeted_anchors: dict[str, list[dict[str, object]]]
    class_counts: dict[str, int]
    resource_suffixes: list[str]
    xml_file: str


def run(command: list[str], timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def adb_prefix(serial: str | None) -> list[str]:
    prefix = ["adb"]
    if serial:
        prefix.extend(["-s", serial])
    return prefix


def adb_text(serial: str | None, *args: str, timeout: float = 30) -> str:
    result = run([*adb_prefix(serial), *args], timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"adb {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}"
        )
    return result.stdout


def adb_bytes(serial: str | None, *args: str, timeout: float = 30) -> bytes:
    result = subprocess.run(
        [*adb_prefix(serial), *args],
        check=False,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).decode("utf-8", errors="replace")
        raise RuntimeError(f"adb {' '.join(args)} failed: {message.strip()}")
    return result.stdout


def dump_ui(serial: str | None, remote_name: str) -> ET.Element:
    remote_path = f"/sdcard/{remote_name}"
    fallback_path = f"/storage/emulated/0/{remote_name}"
    adb_text(serial, "shell", "uiautomator", "dump", remote_path, timeout=30)
    xml = adb_text(serial, "shell", "cat", remote_path, timeout=10)
    if not xml.strip():
        xml = adb_text(serial, "shell", "cat", fallback_path, timeout=10)
    return ET.fromstring(xml)


def node_bounds(node: ET.Element) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)]\[(\d+),(\d+)]", node.attrib.get("bounds", ""))
    if not match:
        return None
    return tuple(map(int, match.groups()))


def is_sensitive_resource(value: str) -> bool:
    lowered = value.casefold()
    return any(pattern in lowered for pattern in SENSITIVE_RESOURCE_PATTERNS)


def looks_like_address(value: str) -> bool:
    if re.search(r"\b\d{4,6}\s+[A-ZÄÖÜ][\wÄÖÜäöüß.-]+", value):
        return True
    return bool(
        re.search(
            r"\b(?:street|strasse|straße|str\.|road|weg|allee|platz|gasse)\b",
            value,
            re.IGNORECASE,
        )
    )


def redact_text(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if "@" in text:
        return "[REDACTED_EMAIL]"
    if looks_like_address(text):
        return "[REDACTED_ADDRESS]"
    text = re.sub(r"\b[A-HJ-NPR-Z0-9]{17}\b", "[REDACTED_VIN]", text)
    text = re.sub(r"\b\d{1,3}(?:\.\d{3}){1,2}\b", "[REDACTED_NUMBER]", text)
    text = re.sub(r"\b\d{5,}\b", "[REDACTED_NUMBER]", text)
    text = re.sub(
        r"\b\d{1,3}(?:[.,]\d+)?\s*[°]\s*[NS]\b", "[REDACTED_COORDINATE]", text
    )
    text = re.sub(
        r"\b\d{1,3}(?:[.,]\d+)?\s*[°]\s*[EW]\b", "[REDACTED_COORDINATE]", text
    )
    return text


def sanitize_tree(root: ET.Element) -> ET.Element:
    sanitized = ET.Element(root.tag, dict(root.attrib))
    for child in root:
        sanitized.append(sanitize_node(child))
    return sanitized


def sanitize_node(node: ET.Element) -> ET.Element:
    keep: dict[str, str] = {}
    resource_id = node.attrib.get("resource-id", "")
    sensitive_resource = is_sensitive_resource(resource_id)

    for key in (
        "index",
        "class",
        "package",
        "resource-id",
        "checkable",
        "checked",
        "clickable",
        "enabled",
        "focusable",
        "focused",
        "scrollable",
        "selected",
        "bounds",
    ):
        if key in node.attrib:
            keep[key] = node.attrib[key]

    if sensitive_resource and resource_id:
        keep["resource-id"] = "[REDACTED_RESOURCE_ID]"

    for key in ("text", "content-desc"):
        value = redact_text(node.attrib.get(key, ""))
        if value and not sensitive_resource:
            keep[key] = value
        elif value:
            keep[key] = "[REDACTED]"

    sanitized = ET.Element(node.tag, keep)
    for child in node:
        sanitized.append(sanitize_node(child))
    return sanitized


def strings(root: ET.Element) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for node in root.iter():
        for key in ("text", "content-desc"):
            value = node.attrib.get(key, "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            values.append(value)
    return values


def keyword_strings(values: list[str]) -> list[str]:
    result = []
    for value in values:
        lowered = value.casefold()
        if any(keyword in lowered for keyword in PHEV_KEYWORDS):
            result.append(value)
    return result


def resource_suffix(value: str) -> str:
    if not value:
        return ""
    return value.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def node_excerpt(node: ET.Element) -> dict[str, object]:
    text = redact_text(node.attrib.get("text", ""))
    description = redact_text(node.attrib.get("content-desc", ""))
    excerpt: dict[str, object] = {
        "class": node.attrib.get("class", ""),
        "resourceSuffix": resource_suffix(node.attrib.get("resource-id", "")),
        "bounds": node.attrib.get("bounds", ""),
        "clickable": node.attrib.get("clickable", ""),
        "checkable": node.attrib.get("checkable", ""),
        "checked": node.attrib.get("checked", ""),
    }
    if text:
        excerpt["text"] = text
    if description:
        excerpt["contentDesc"] = description
    return excerpt


def anchor_matches(root: ET.Element) -> dict[str, list[dict[str, object]]]:
    matches: dict[str, list[dict[str, object]]] = {
        anchor["id"]: [] for anchor in TARGET_ANCHORS
    }
    for node in root.iter():
        suffix = resource_suffix(node.attrib.get("resource-id", ""))
        text = node.attrib.get("text", "")
        description = node.attrib.get("content-desc", "")
        for anchor in TARGET_ANCHORS:
            kind = anchor["kind"]
            value = anchor["value"]
            matched = False
            if kind == "resource_suffix":
                matched = suffix == value
            elif kind == "text_exact":
                matched = text == value
            elif kind == "content_desc_contains":
                matched = value.casefold() in description.casefold()
            if matched:
                matches[anchor["id"]].append(node_excerpt(node))
    return {key: value for key, value in matches.items() if value}


def summarize_screen(name: str, root: ET.Element, output_dir: Path) -> ScreenCapture:
    sanitized = sanitize_tree(root)
    xml_file = output_dir / f"{name}.sanitized.xml"
    ET.indent(sanitized)
    ET.ElementTree(sanitized).write(xml_file, encoding="utf-8", xml_declaration=True)

    safe_strings = strings(sanitized)
    resources = sorted(
        {
            resource_suffix(node.attrib.get("resource-id", ""))
            for node in sanitized.iter()
            if node.attrib.get("resource-id", "")
            and node.attrib.get("resource-id", "") != "[REDACTED_RESOURCE_ID]"
        }
    )
    classes = Counter(
        node.attrib.get("class", "") for node in sanitized.iter() if node.attrib.get("class", "")
    )

    return ScreenCapture(
        name=name,
        raw_node_count=sum(1 for _ in root.iter()),
        sanitized_node_count=sum(1 for _ in sanitized.iter()),
        strings=safe_strings,
        keyword_strings=keyword_strings(safe_strings),
        targeted_anchors=anchor_matches(root),
        class_counts=dict(classes.most_common()),
        resource_suffixes=resources,
        xml_file=xml_file.name,
    )


def write_report(
    output_dir: Path,
    captures: list[ScreenCapture],
    metadata: dict[str, object],
) -> None:
    data = {
        "version": 1,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "purpose": "Volkswagen GTE/PHEV read-only UI diagnostics",
        "privacyNote": (
            "Review all files before sharing. The script redacts common sensitive "
            "patterns, but app UI wording can vary."
        ),
        "metadata": metadata,
        "targetAnchorDefinitions": TARGET_ANCHORS,
        "screens": [capture.__dict__ for capture in captures],
    }
    (output_dir / "summary.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# Volkswagen GTE/PHEV Diagnostics",
        "",
        "Review all files before sharing. Do not share VINs, addresses, coordinates,",
        "account data, device serials, screenshots or raw UI dumps publicly.",
        "",
        "## Metadata",
        "",
    ]
    for key, value in metadata.items():
        lines.append(f"- {key}: {value}")
    for capture in captures:
        lines.extend(
            [
                "",
                f"## {capture.name}",
                "",
                f"- sanitized XML: `{capture.xml_file}`",
                f"- nodes: {capture.sanitized_node_count}",
                "- targeted anchors:",
            ]
        )
        if capture.targeted_anchors:
            for anchor, matches in capture.targeted_anchors.items():
                lines.append(f"  - {anchor}: {len(matches)}")
        else:
            lines.append("  - none detected")
        lines.extend(
            [
                "- PHEV-relevant strings:",
            ]
        )
        if capture.keyword_strings:
            lines.extend(f"  - {value}" for value in capture.keyword_strings)
        else:
            lines.append("  - none detected")
    (output_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def collect_metadata(serial: str | None, package: str) -> dict[str, object]:
    metadata: dict[str, object] = {
        "adbState": adb_text(serial, "get-state", timeout=5).strip(),
        "package": package,
    }
    for key, prop in (
        ("androidManufacturer", "ro.product.manufacturer"),
        ("androidModel", "ro.product.model"),
        ("androidRelease", "ro.build.version.release"),
        ("androidSdk", "ro.build.version.sdk"),
    ):
        try:
            metadata[key] = adb_text(serial, "shell", "getprop", prop, timeout=5).strip()
        except RuntimeError as exc:
            metadata[key] = f"unavailable: {exc}"
    try:
        package_info = adb_text(serial, "shell", "dumpsys", "package", package, timeout=8)
        version = re.search(r"\bversionName=([^\s]+)", package_info)
        version_code = re.search(r"\bversionCode=(\d+)", package_info)
        metadata["appVersion"] = version.group(1) if version else ""
        metadata["appVersionCode"] = version_code.group(1) if version_code else ""
    except RuntimeError as exc:
        metadata["appVersion"] = f"unavailable: {exc}"
        metadata["appVersionCode"] = ""
    return metadata


def maybe_save_screenshot(serial: str | None, output_dir: Path, name: str) -> None:
    png = adb_bytes(serial, "exec-out", "screencap", "-p", timeout=20)
    (output_dir / f"{name}.local-review-only.png").write_bytes(png)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect read-only VW app UI diagnostics for GTE/PHEV support."
    )
    parser.add_argument("--serial", help="ADB serial. Defaults to adb's selected device.")
    parser.add_argument(
        "--package",
        default=DEFAULT_PACKAGE,
        help=f"Volkswagen app package. Default: {DEFAULT_PACKAGE}",
    )
    parser.add_argument(
        "--output",
        default="gte-phev-diagnostics-output",
        help="Output directory. Default: gte-phev-diagnostics-output",
    )
    parser.add_argument(
        "--screens",
        nargs="+",
        default=list(DEFAULT_SCREENS),
        help="Screen names to capture. Tester navigates manually before each dump.",
    )
    parser.add_argument(
        "--screenshots",
        action="store_true",
        help="Also save screenshots for local review only. Do not share without redaction.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Capture screens immediately without pressing Enter between screens.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not shutil.which("adb"):
        print("adb was not found in PATH.", file=sys.stderr)
        return 2

    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Volkswagen GTE/PHEV read-only diagnostics")
    print("This script only reads the current screen UI hierarchy.")
    print("Manually open the Volkswagen app and navigate when prompted.")
    print()

    try:
        metadata = collect_metadata(args.serial, args.package)
    except Exception as exc:
        print(f"Failed to collect ADB metadata: {exc}", file=sys.stderr)
        return 1

    captures: list[ScreenCapture] = []
    for screen in args.screens:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", screen.strip()).strip("-")
        if not safe_name:
            continue
        if not args.non_interactive:
            input(
                f"Navigate to the VW app '{screen}' screen, wait until it is stable, "
                "then press Enter..."
            )
        try:
            root = dump_ui(args.serial, f"vw-{safe_name}.xml")
            captures.append(summarize_screen(safe_name, root, output_dir))
            if args.screenshots:
                maybe_save_screenshot(args.serial, output_dir, safe_name)
            print(f"Captured {safe_name}")
        except Exception as exc:
            print(f"Failed to capture {safe_name}: {exc}", file=sys.stderr)

    write_report(output_dir, captures, metadata)
    print()
    print(f"Wrote diagnostics to: {output_dir}")
    print("Share summary.json, README.md and *.sanitized.xml only after review.")
    print("Do not share *.local-review-only.png publicly.")
    return 0 if captures else 1


if __name__ == "__main__":
    raise SystemExit(main())
