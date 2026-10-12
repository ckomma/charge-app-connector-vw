# Volkswagen App Connector

ADB-based connector for reading and controlling the Volkswagen Android app.
It uses Android's accessibility UI hierarchy and does not inspect Volkswagen
network traffic or store Volkswagen account credentials.

Based on [janphkre/charge-app-connector](https://github.com/janphkre/charge-app-connector).

## Features

- Cached charge, structured vehicle-detail, vehicle-software and location data through a REST API.
- Optional battery health and calculation date from the vehicle health report.
- Authenticated charging, climate, lock and departure-time editor actions.
- Vehicle-observed capabilities with explicit unavailable and unknown states.
- German and English Volkswagen app UI support.
- USB ADB with optional Wi-Fi fallback.
- Persisted usage budgets, retries, cooldowns and stale-data handling.
- MQTT discovery for Home Assistant plus examples for REST, openHAB and evcc.
- systemd, Docker Compose and Home Assistant App deployment options.

## Compatibility

The verified write-action baseline is Volkswagen app `4.6.4` (`406040`).
German and English real-phone tests cover reads, climate start/stop, charging
stop/start and remote lock/unlock. Lock actions now wait for completed vehicle
feedback and verify the result; pending labels remain unknown. The corrected
unlock was physically confirmed by the user in both languages. Existing
explicit version settings remain unchanged. See the
[compatibility findings](docs/compatibility-4.6.4.md).

Version `4.6.4` supports the new data-saving recovery wording and German parking
label. Optional battery-health and vehicle-software fields are available when
the app exposes them; this test account exposed neither optional feature.

Requirements:

- Android phone with the Volkswagen app signed in.
- Volkswagen app language set to German or English.
- Authorized USB or wireless ADB access.
- Python 3.11 or newer.
- `requirements.txt` dependencies when MQTT is enabled.

## Quick Start

1. Choose a deployment:

   - systemd templates in [`deploy/`](deploy/)
   - [Docker Compose example](deploy/docker/docker-compose-example.yaml)
   - [Home Assistant App](deploy/home-assistant/README.md)

2. Configure at least the ADB transport and, for actions, `API_KEY` and
   `VERIFIED_APP_VERSION`. Lock/unlock additionally requires `VW_SPIN`.

3. Start the connector and verify it without waking the phone unnecessarily:

   ```bash
   adb devices -l
   curl -sS http://127.0.0.1:9920/health
   curl -sS http://127.0.0.1:9920/charge
   ```

The default listener is `127.0.0.1:9920`. Do not expose it directly to an
untrusted network.

## API Overview

| Endpoint | Purpose |
| --- | --- |
| `GET /charge` | Cached charge state, state of charge, range and freshness |
| `GET /details` | Climate, odometer, service, battery health, warnings and departures |
| `GET /software` | Cached vehicle software version and update information |
| `GET /location` | Cached address, parking duration and coordinates |
| `GET /health` | ADB, app-version, cache, usage and cooldown health |
| `GET /capabilities` | Supported endpoints, actions and transports |
| `GET /metrics` | Prometheus metrics |
| `GET /diagnostics` | Safe diagnostic-file metadata |

Authenticated `POST /action/*` endpoints control charging, climate and locks.
Administrative endpoints provide a coalesced charge refresh and a guarded
cooldown probe. Reads remain cache-only and do not perform one Volkswagen app
operation per HTTP request.

See [API reference](docs/api.md) for payload behavior, actions, asynchronous
jobs and administrative endpoints.

`POST /action/software/refresh` requires `X-API-Key` and performs a read-only
vehicle software query within the existing action budget. Software has no
background polling; `GET /software` never wakes the phone. Availability depends
on the vehicle/account and app feature rollout. Missing values remain unknown.

## Configuration and Operation

Defaults are deliberately conservative: charge refreshes every 5 minutes while
charging and 15 minutes while parked, details every 12 hours and location every
4 hours. Background and action budgets are persisted across restarts.

- [Configuration and deployment](docs/configuration.md)
- [Operations, health and diagnostics](docs/operations.md)
- [Home Assistant, MQTT, openHAB and evcc](docs/integrations.md)
- [Security policy](SECURITY.md)
- [Home Assistant App changelog](addons/vw-app-connector/CHANGELOG.md)

## Home Assistant App

Add this repository URL as a custom repository under:

```text
Settings -> Add-ons -> Add-on Store -> Repositories
```

The app uses the same connector code, stores persistent state below
Supervisor-managed `/data`, defaults to manual boot and currently reports
version `0.1.28`. See the [installation guide](deploy/home-assistant/README.md).

## Integrations

- Home Assistant through read-only MQTT discovery or the
  [`examples/vw_app_connector.yaml`](examples/vw_app_connector.yaml) REST package.
- openHAB through Home Assistant MQTT discovery; see
  [`examples/openhab/README.md`](examples/openhab/README.md).
- evcc through the cache-only [`examples/evcc.yaml`](examples/evcc.yaml) vehicle.

## Development

Keep the main runtime and Home Assistant copy synchronized. For Python changes:

```bash
python -m unittest discover -s tests -v
python -m py_compile vw_app_connector.py
git diff --check
```

Package the Home Assistant App with:

```powershell
powershell -ExecutionPolicy Bypass -File tools\package_ha_app.ps1 -Clean
```

Real-phone selector or action changes must be verified in German and English
with usage limits and all vehicle states restored afterward.
