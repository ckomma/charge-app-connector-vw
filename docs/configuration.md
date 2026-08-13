# Configuration and Deployment

Use `deploy/vw-app-connector.default` as the systemd environment template or
the equivalent options in Docker Compose and the Home Assistant App. Keep
secrets in the runtime environment, never in Git.

## Core Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `ADB_SERIAL` | required | Device serial or `auto` for the only authorized USB device |
| `ADB_MODE` | `usb` | `usb`, `wifi` or USB-preferred `auto` |
| `ADB_WIFI_ADDRESS` | empty | Wireless-debugging connection address `IP:Port` |
| `LISTEN_ADDRESS` | `127.0.0.1` | REST bind address |
| `PORT` | `9920` | REST port |
| `APP_PACKAGE` | `com.volkswagen.weconnect` | Volkswagen Android package |
| `MAPS_PACKAGE` | `com.google.android.apps.maps` | Maps package used for coordinates |
| `VERIFIED_APP_VERSION` | `4.3.2` | Real-device-tested write-action baseline |
| `SLEEP_AFTER_OPERATION` | `true` | Wake before and sleep after UI work |
| `API_KEY` | required for writes | Protects `/action/*` and `/admin/*` |
| `VW_SPIN` | empty | Required only for lock/unlock |

Set `VERIFIED_APP_VERSION` to an empty value only when deliberately disabling
the write-action quarantine.

## Refresh and Usage Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `CHARGING_INTERVAL_SECONDS` | `300` | Charge refresh while charging |
| `IDLE_INTERVAL_SECONDS` | `900` | Charge refresh while parked |
| `DETAIL_INTERVAL_SECONDS` | `43200` | Vehicle-detail refresh |
| `LOCATION_INTERVAL_SECONDS` | `14400` | Location refresh and location-only retry |
| `BACKGROUND_MIN_INTERVAL_SECONDS` | `300` | Minimum interval between background work |
| `BACKGROUND_ERROR_RETRY_SECONDS` | `900` | Initial failure retry/backoff interval |
| `BACKGROUND_TRANSIENT_BACKOFF_MAX_SECONDS` | `7200` | Maximum shared exponential pause |
| `SOURCE_STALE_AFTER_MINUTES` | `60` | Volkswagen source-stale threshold |
| `BACKGROUND_DAILY_LIMIT` | `180` | Weighted daily background budget |
| `ACTION_MIN_INTERVAL_SECONDS` | `60` | Minimum interval between actions |
| `ACTION_DAILY_LIMIT` | `20` | Daily action budget |
| `RATE_LIMIT_COOLDOWN_SECONDS` | `43200` | Explicit Volkswagen rate-limit cooldown |
| `COOLDOWN_PROBE_MIN_INTERVAL_SECONDS` | `900` | Minimum interval between admin probes |

State paths:

- `USAGE_STATE_FILE=/var/lib/vw-app-connector/usage.json`
- `CACHE_STATE_DIR=/var/lib/vw-app-connector/cache`
- `DIAGNOSTICS_DIR=/var/lib/vw-app-connector/diagnostics`

UI timing defaults are `APP_START_WAIT_SECONDS=8`, `DETAIL_WAIT_SECONDS=3` and
`UI_UPDATE_TIMEOUT_SECONDS=8`.

## MQTT Settings

`MQTT_HOST` enables read-only MQTT publishing and Home Assistant discovery.
Additional settings are `MQTT_PORT` (`1883`), `MQTT_USERNAME`,
`MQTT_PASSWORD`, `MQTT_TOPIC_PREFIX` (`vw_app_connector`),
`MQTT_DISCOVERY_PREFIX` (`homeassistant`), `MQTT_CLIENT_ID`
(`vw-app-connector`) and `MQTT_TLS` (`false`). Use a unique client ID per
connector and TLS for remote brokers.

## systemd

Install the files from `deploy/`, configure
`/etc/default/vw-app-connector`, keep that file root-readable and then verify:

```bash
systemctl status vw-app-connector
curl -sS http://127.0.0.1:9920/health
curl -sS http://127.0.0.1:9920/charge
```

## Docker Compose

```bash
cp deploy/docker/env.example deploy/docker/.env
editor deploy/docker/.env
docker compose -f deploy/docker/docker-compose-example.yaml up -d --build
```

The example persists connector state and ADB keys in named volumes and binds
the host port to `127.0.0.1`. USB ADB uses `/dev/bus/usb` with privileged mode;
for wireless ADB configure `ADB_MODE=wifi` or `auto` and
`ADB_WIFI_ADDRESS`.

Smoke test:

```bash
docker compose -f deploy/docker/docker-compose-example.yaml exec vw-app-connector adb devices -l
curl -sS http://127.0.0.1:9920/health
curl -sS http://127.0.0.1:9920/charge
```

## Home Assistant App

The custom app is an optional Supervisor deployment alongside systemd and
Docker Compose. It stores state below `/data`, exposes port `9920`, supports USB
or wireless ADB and defaults to manual boot. See the full
[Home Assistant App guide](../deploy/home-assistant/README.md).

## ADB over Wi-Fi

USB is the most reliable default. To prepare wireless debugging:

1. Enable Android developer options and wireless debugging.
2. Run `adb pair PHONE_IP:PAIRING_PORT` and enter the displayed code.
3. Record the separate connection address.
4. Configure `ADB_WIFI_ADDRESS=PHONE_IP:CONNECTION_PORT`.

The connection port can change after a phone restart or toggling wireless
debugging. `/health` exposes the selected mode, transport, Wi-Fi configuration
and latest connection error.

## Device Notes

Xiaomi/MIUI devices require `USB debugging (Security settings)` for simulated
taps. Keep the proximity sensor uncovered and the phone display-up so pocket
mode does not obscure the app.

Pixel devices use standard USB debugging. A secure display lock is incompatible
with automatic sleep cleanup; use `SLEEP_AFTER_OPERATION=false` or remove the
secure lock for unattended operation.

The connector ships no shared ADB keys. Authorize the key generated by each
runtime host on the phone.
