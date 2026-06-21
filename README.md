# Volkswagen App Connector

Based on [janphkre/charge-app-connector](https://github.com/janphkre/charge-app-connector)
for reading vehicle data from the Volkswagen Android app.

The connector uses ADB and Android's accessibility UI hierarchy. It does not
inspect Volkswagen network traffic or store Volkswagen account credentials.
Optional authenticated actions use the same accessibility UI.

## Data

`GET /charge` returns:

```json
{
  "status": "B",
  "soc": 30,
  "range": 126,
  "remainingChargeMinutes": null,
  "chargeRateKmH": null,
  "chargePowerKw": null,
  "targetSoc": null,
  "chargingMode": "",
  "climater": false,
  "locked": false,
  "syncAgeMinutes": 18,
  "observedAt": "2026-06-13T19:20:00+02:00",
  "error": ""
}
```

`GET /location` opens the map, centers the vehicle badge and returns the
displayed address, parked duration, latitude and longitude. Coordinates are
read from the navigation intent opened by the Route button. The endpoint is
intentionally separate because map navigation is slower than the evcc
charge-data poll.

`GET /details` returns the target temperature, automatic window heating,
front climate zones, odometer, service interval, warning status and departure
times. `GET /health` is a cheap endpoint that does not wake the display. It
reports ADB/app status, phone battery telemetry and cache ages.

The connector refreshes data in background. API reads return immediately and
continue serving the last successful value with `stale: true` when a refresh
fails. Failed UI reads are retried once and store a UI dump, screenshot and
error summary in the diagnostics directory.

Usage protection is enforced inside the connector and persisted across service
restarts. Defaults are deliberately conservative: 15 minutes while parked,
5 minutes while charging, details every 12 hours and location every 4 hours.
Background work has a weighted budget of 180 units per local calendar day;
the detail read costs three units because it opens three app views. Actions
have a separate budget of 20 per day and a 60-second minimum interval. If the
app reports too many requests, all app operations pause for 12 hours. Current
usage and cooldown are exposed by `/health`.

Status follows evcc's vehicle convention:

- `A`: disconnected or connection unknown
- `B`: connected, not charging
- `C`: charging

## Requirements

- Android phone with the Volkswagen app already signed in
- Volkswagen app language set to German or English
- regular USB debugging
- Xiaomi devices: `USB debugging (Security settings)` for simulated taps
- `adb` available to the service user
- Python 3.11 or newer
- Python dependencies from `requirements.txt` when MQTT is enabled

The connector supports German and English Volkswagen app localizations. It
matches localized visible labels and accessibility descriptions for vehicle,
charging, climate, location and report views. Both languages are verified
against a live Volkswagen app installation, including read operations and
vehicle actions. Volkswagen may still vary UI wording between app versions and
vehicle capabilities. Other app languages are not supported.

The latest real-device verification used Volkswagen app `3.63.2`
(`versionCode 41262`) on the production Redmi. This is a tested baseline, not
an exact version pin; newer app versions must be reverified because UI labels
and accessibility metadata can change independently of the connector.

## Configuration

Environment variables:

- `ADB_SERIAL`: required ADB serial
- `ADB_MODE`: `usb` (default), `wifi` or `auto`
- `ADB_WIFI_ADDRESS`: optional Android wireless-debugging address as `IP:Port`;
  required for `wifi`, used as fallback by `auto`
- `LISTEN_ADDRESS`: default `127.0.0.1`
- `PORT`: default `9920`
- `CHARGING_INTERVAL_SECONDS`: default `300`
- `IDLE_INTERVAL_SECONDS`: default `900`
- `DETAIL_INTERVAL_SECONDS`: default `43200`
- `LOCATION_INTERVAL_SECONDS`: default `14400`
- `BACKGROUND_MIN_INTERVAL_SECONDS`: default `300`
- `BACKGROUND_ERROR_RETRY_SECONDS`: default `900`; failed cache refreshes wait
  before retrying so persistent UI problems do not consume the daily budget
- `BACKGROUND_DAILY_LIMIT`: default `180`
- `ACTION_MIN_INTERVAL_SECONDS`: default `60`
- `ACTION_DAILY_LIMIT`: default `20`
- `RATE_LIMIT_COOLDOWN_SECONDS`: default `43200`
- `USAGE_STATE_FILE`: default `/var/lib/vw-app-connector/usage.json`
- `CACHE_STATE_DIR`: default `/var/lib/vw-app-connector/cache`; stores the last
  successful endpoint values so restarts can serve data during cache refresh
- `DIAGNOSTICS_DIR`: default `/var/lib/vw-app-connector/diagnostics`
- `APP_PACKAGE`: default `com.volkswagen.weconnect`
- `VERIFIED_APP_VERSION`: default `3.63.2`; write actions are quarantined when
  the installed Volkswagen app version differs
- `APP_START_WAIT_SECONDS`: default `8`
- `DETAIL_WAIT_SECONDS`: default `3`
- `UI_UPDATE_TIMEOUT_SECONDS`: default `8`; maximum wait for an expected UI
  value or semantic element after navigation or a setting change
- `SLEEP_AFTER_OPERATION`: default `true`; wake and unlock before UI automation,
  then switch the display off again
- `API_KEY`: required for `POST /action/*`
- `VW_SPIN`: required for lock and unlock actions
- `MQTT_HOST`: optional broker host; enables read-only MQTT publishing and
  Home Assistant discovery
- `MQTT_PORT`: default `1883`
- `MQTT_USERNAME`, `MQTT_PASSWORD`: optional broker credentials
- `MQTT_TOPIC_PREFIX`: default `vw_app_connector`
- `MQTT_DISCOVERY_PREFIX`: default `homeassistant`
- `MQTT_CLIENT_ID`: default `vw-app-connector`; use a unique value per connector
- `MQTT_TLS`: default `false`; enable TLS using the system CA store

Due detail and location refreshes take priority over routine charge refreshes.
This prevents the five-minute charge polling interval from repeatedly delaying
the less frequent multi-page reads. The background minimum interval and daily
budget still apply unchanged.

Authenticated action endpoints:

- `POST /action/lock`
- `POST /action/unlock`
- `POST /action/charging/start`
- `POST /action/charging/stop`
- `POST /action/charging/target-soc?value=80`
- `POST /action/charging/mode?value=immediate`
- `POST /action/charging/settings`
- `POST /action/charging/option/battery-care?value=true`
- `POST /action/charging/option/reduced-ac?value=true`
- `POST /action/charging-location/direct-soc?name=Home&value=30`
- `POST /action/charging-location/target-soc?name=Home&value=80`
- `POST /action/charging-location/settings?name=Home`
- `POST /action/charging-location/option/reduced-ac?name=Home&value=true`
- `POST /action/charging-location/option/auto-unlock?name=Home&value=true`
- `POST /action/charging-locations`
- `POST /action/climate/start`
- `POST /action/climate/stop`
- `POST /action/climate/temperature?value=20.5`
- `POST /action/climate/option/automatic-window-heating?value=true`
- `POST /action/climate/option/zone-front-left?value=true`
- `POST /action/climate/option/zone-front-right?value=true`

The target state of charge supports 50, 60, 70, 80, 90 and 100 percent. Charging
modes are `immediate`, `preferred-times`, `departure` and `departure-climate`.
Location-specific direct-charge limits support 0 through 50 percent in ten-point
steps; location target limits use the same values as the global target. Slider
positions are derived from the current accessibility layout and every change is
checked against the displayed value before it is saved.

On the live-tested Volkswagen app `3.63.2`, the charging-mode row is readable
but exposes no clickable accessibility element. The connector therefore fails
that action safely instead of using an unverified fixed coordinate. Charging
locations can only be controlled when the app account has a location configured.

### App version quarantine

Read endpoints and MQTT remain available after a Volkswagen app update. If the
installed version differs from `VERIFIED_APP_VERSION`, `/health` stays HTTP 200
with `status: degraded`, `actionAvailable: false` and
`actionBlockedReason: UNVERIFIED_APP_VERSION`. Read-only settings actions remain
available, while write actions return HTTP 409 before consuming action budget.
Set `VERIFIED_APP_VERSION` to an empty value to disable this guard deliberately.

### Optional asynchronous actions

Existing calls remain synchronous and return the verified result with HTTP 200.
Clients can opt into a serialized background job using:

```http
POST /action/charging/target-soc?value=80
Prefer: respond-async
Idempotency-Key: unique-request-id
X-API-Key: replace-with-the-connector-api-key
```

The response is HTTP 202 with a job ID and `Location` header. Read the result
from authenticated `GET /actions/JOB_ID`. States are `queued`, `running`,
`succeeded` and `failed`. Reusing an `Idempotency-Key` with the same request
returns the original job; using it for another request returns HTTP 409. Job
history is memory-only, limited to 100 completed entries and resets when the
service restarts.

Send the API key in the `X-API-Key` header. Keep the environment file readable
only by root because it contains the Volkswagen S-PIN.

Install the files from `deploy/` and adjust `/etc/default/vw-app-connector`.

The connector intentionally contains no shared ADB keys. Authorize the key
generated on the target host using the dialog on the phone.

The Android device must not use a secure display PIN, password or pattern when
`SLEEP_AFTER_OPERATION=true`. ADB wakes the display and dismisses the
non-secure keyguard automatically.

### Xiaomi/MIUI notes

Xiaomi/MIUI pocket mode can block UI automation when the proximity sensor is
covered, especially when the phone is placed display-down. The connector
detects and dismisses the known overlay and retries empty UI hierarchies while
the Volkswagen app is foreground. For reliable unattended operation, keep the
proximity sensor uncovered and place the phone display-up.

The MIUI handling is an additive compatibility fallback; the connector is not
limited to Xiaomi phones. Other manufacturers may expose different wake,
foreground-window, or pocket-mode behavior and should be verified before
unattended use.

### Google Pixel notes

Pixel devices use standard Android USB debugging and do not need Xiaomi's
additional `USB debugging (Security settings)` option. Before unattended use,
verify that the target host sees the phone in `adb devices -l`, that the
Volkswagen app is installed and signed in, and that `/health` reports the
expected ADB transport. If ADB lists no device, check the USB cable/data mode,
developer options, USB debugging, and the authorization dialog on the phone
before changing connector code.

On Android 16, UI dumps may be written under `/storage/emulated/0` even when
`uiautomator` reports a `/sdcard` path. The connector keeps a fallback for that
storage-path difference.

Android 16 also reports focused-window fields in `dumpsys window`; the
connector uses that broader output for foreground detection. If the Pixel keeps
a secure display lock, set `SLEEP_AFTER_OPERATION=false` or disable the secure
lock. Otherwise the connector can wake the phone but cannot dismiss the
keyguard unattended.

## Optional ADB over Wi-Fi

USB remains the default and most reliable transport. To prepare Wi-Fi:

1. Enable Android developer options and wireless debugging.
2. On the host running the connector, run
   `adb pair PHONE_IP:PAIRING_PORT`.
3. Enter the pairing code shown by Android.
4. Note the separate connection address shown by Android and configure it as
   `ADB_WIFI_ADDRESS=PHONE_IP:CONNECTION_PORT`.

Modes:

- `ADB_MODE=usb`: only the configured `ADB_SERIAL` is used.
- `ADB_MODE=wifi`: the connector automatically reconnects to
  `ADB_WIFI_ADDRESS`.
- `ADB_MODE=auto`: USB is preferred; Wi-Fi is used only while USB is
  unavailable.

The Android wireless-debugging connection port may change after a phone
restart or after disabling wireless debugging. In that case update
`ADB_WIFI_ADDRESS`. `/health` exposes `adbMode`, `adbTransport`,
`adbWifiConfigured` and the latest connection error.

## Integrations

### Home Assistant

#### MQTT discovery

MQTT is the simplest Home Assistant setup when HA is already connected to the
same broker. Install the optional dependency on the connector host:

On Debian or Ubuntu, install `python3-paho-mqtt` from the distribution. On
other systems, install the dependencies from `requirements.txt` in the Python
environment used by the service.

Set `MQTT_HOST`, `MQTT_USERNAME` and `MQTT_PASSWORD` in
`/etc/default/vw-app-connector`, then restart the service. Home Assistant
automatically creates a `Volkswagen App Connector` device with charge, range,
charging, climate, lock, vehicle-detail and location entities. No HA YAML is
required. MQTT publishes retained copies of existing cache updates and never
causes an additional Volkswagen app operation. REST remains enabled for evcc
and existing clients.

The integration is intentionally read-only. Vehicle actions continue to use
the authenticated HTTP endpoints. Location includes address and coordinates;
do not enable MQTT location publishing on a broker that is not trusted.

#### REST package

[`examples/vw_app_connector.yaml`](examples/vw_app_connector.yaml)
provides an example Home Assistant package with REST sensors, a vehicle
location tracker and authenticated controls for locking, charging and climate.

Replace `CONNECTOR_HOST` with the connector host name or IP address. Add the
same value configured as `API_KEY` on the connector to Home Assistant's
`secrets.yaml`:

```yaml
vw_app_connector_api_key: replace-with-the-connector-api-key
```

The example assumes that Home Assistant packages are enabled:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

Review the entities and polling intervals before using the example. The
connector performs background refreshes according to its own rate limits;
reading its cached HTTP endpoints from Home Assistant does not trigger one
Volkswagen app operation per request.

### openHAB

openHAB 5 can consume the existing Home Assistant MQTT discovery messages.
Install the MQTT and Home Assistant bindings, connect openHAB to the same
broker and approve the discovered `Volkswagen App Connector` Thing. The Thing
is read-only and does not cause additional Volkswagen app operations.

[`examples/openhab/README.md`](examples/openhab/README.md) documents the setup
and provides optional Rules DSL examples for authenticated charging and climate
actions. Lock and unlock are intentionally omitted because their app gesture
must be reverified before unattended use.

### evcc

[`examples/evcc.yaml`](examples/evcc.yaml) provides a complete custom vehicle
entry for evcc. It exposes state of charge, connection/charging status and
estimated range.

Replace `CONNECTOR_HOST` with the connector host name or IP address and merge
the `vehicles` entry into `evcc.yaml`. If evcc and the connector run on the
same host, use `127.0.0.1`. The vehicle can then be assigned to a loadpoint:

```yaml
loadpoints:
  - title: Garage
    charger: your_charger
    vehicle: volkswagen_app
```

The example only reads cached connector endpoints and does not expose vehicle
write actions to evcc. Connector-side refresh intervals and usage limits
remain authoritative.
