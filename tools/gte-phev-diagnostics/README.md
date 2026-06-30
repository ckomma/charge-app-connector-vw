# GTE/PHEV diagnostics

This directory contains a read-only helper for collecting Volkswagen app UI
information needed to add GTE/PHEV support.

The script does not tap, swipe, sync, save or change vehicle settings. The
tester manually navigates to each relevant Volkswagen app screen and presses
Enter. The script then reads the currently visible Android accessibility tree
through ADB, writes sanitized XML dumps and creates a compact summary.

## Requirements

- Android phone with the Volkswagen app already logged in
- ADB installed on the host running the script
- Authorized USB debugging for the phone
- Python 3.11 or newer

Check the phone first:

```bash
adb devices -l
```

## Usage

From the repository root:

```bash
python tools/gte-phev-diagnostics/gte_phev_diagnostics.py --serial ADB_SERIAL
```

If only one ADB device is connected, `--serial` can be omitted:

```bash
python tools/gte-phev-diagnostics/gte_phev_diagnostics.py
```

The default output directory is `gte-phev-diagnostics-output/`. Share only
these files after manual review:

- `summary.json`
- `README.md`
- `*.sanitized.xml`

Do not share screenshots, raw UI dumps, VINs, addresses, coordinates, account
data, device serials or Volkswagen credentials publicly.

## Optional screenshots

Screenshots can help the tester review whether the captured screen was correct.
They are for local review only and must be redacted manually before sharing.

```bash
python tools/gte-phev-diagnostics/gte_phev_diagnostics.py --screenshots
```

Screenshot files are named `*.local-review-only.png` and ignored by Git.

## Captured screens

By default the script asks for:

- `overview`
- `charging`
- `climate`
- `departure-times`
- `vehicle-details`
- `location-map`

The list can be overridden:

```bash
python tools/gte-phev-diagnostics/gte_phev_diagnostics.py --screens overview climate charging
```

## Targeted anchors

The summary highlights known Volkswagen app anchors that are relevant for GTE
and PHEV work, including:

- `hybridAuxTemperatureSlider`
- `hybridAuxStart`
- `hybridAuxStop`
- `clima_compose_view`
- `cta_start`
- `cta_stop`
- `rangeTile`
- `rangeArcBatterySoc`
- `vehicleCarPlus`
- `climateTile`
- `toggle`
- `value`
- `vwd_save_button`
- `LO` / `HI` temperature labels
- departure-time labels in English and German

These anchors are used only for reporting. The script does not execute any
action based on them.
