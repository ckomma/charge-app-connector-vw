# GTE/PHEV diagnostics

This directory contains a helper for collecting Volkswagen app UI information
needed to add GTE/PHEV support.

By default the script does not tap, swipe, sync, save or change vehicle
settings. The tester manually navigates to each relevant Volkswagen app screen
and presses Enter. The script then reads the currently visible Android
accessibility tree through ADB, writes sanitized XML dumps and creates a compact
summary.

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
- `charging-overview`
- `charging-settings`
- `climate`
- `departure-times`
- `vehicle-details`
- `location-map`

For `charging-overview`, open the small range/charge tile that shows state of
charge and current charging status.

For `charging-settings`, navigate to the charging settings page that shows
settings such as target SoC, Battery Care and reduced AC current. Do not change
or save any setting; only wait until the screen is stable and press Enter.

For `climate`, navigate to the climate screen that shows the target temperature
picker. The summary reports all detected temperature labels, including
localized decimal values and the `LO` / `HI` boundary labels when the app shows
them.

## Optional location marker details

The normal `location-map` capture only verifies the visible map canvas and map
controls. It cannot prove that the connector can select the vehicle marker and
read the address detail sheet, because the vehicle marker is rendered inside
the Google Maps canvas.

To collect that additional state, open the Volkswagen app navigation/map tab
and run:

```bash
python tools/gte-phev-diagnostics/gte_phev_diagnostics.py --screens location-map --location-marker-details
```

With this option the script taps `Car Locate Button`, waits, taps the estimated
vehicle marker label above the centered map pin, then stores additional
sanitized dumps:

- `location-centered-map.sanitized.xml`
- `location-details.sanitized.xml`

This mode is still read-oriented and does not save settings, start charging or
open vehicle actions. It is not tap-free. Review the sanitized files before
sharing because the location detail sheet can contain address or parking
duration text.

The list can be overridden:

```bash
python tools/gte-phev-diagnostics/gte_phev_diagnostics.py --screens overview climate charging-settings
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
- `rangeArcRangeAndUnit`
- `chargingStatsTargetSoc`
- `vehicleCarPlus`
- `climateTile`
- `toggle`
- `value`
- `subtitle`
- `vwd_save_button`
- Battery Care labels
- reduced AC labels
- charging-method labels
- automatic AC connector release labels
- `LO` / `HI` temperature labels
- departure-time labels in English and German
- Google Map / Car Locate / Route anchors
- parked-duration labels in English and German

These anchors are used only for reporting. The script does not execute any
action based on them, except for the explicitly enabled
`--location-marker-details` diagnostic flow described above.
