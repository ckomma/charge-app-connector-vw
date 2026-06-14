# Volkswagen App Connector

Fork of [janphkre/charge-app-connector](https://github.com/janphkre/charge-app-connector)
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
- regular USB debugging
- Xiaomi devices: `USB debugging (Security settings)` for simulated taps
- `adb` available to the service user
- Python 3.11 or newer

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
- `BACKGROUND_DAILY_LIMIT`: default `180`
- `ACTION_MIN_INTERVAL_SECONDS`: default `60`
- `ACTION_DAILY_LIMIT`: default `20`
- `RATE_LIMIT_COOLDOWN_SECONDS`: default `43200`
- `USAGE_STATE_FILE`: default `/var/lib/vw-app-connector/usage.json`
- `DIAGNOSTICS_DIR`: default `/var/lib/vw-app-connector/diagnostics`
- `APP_PACKAGE`: default `com.volkswagen.weconnect`
- `APP_START_WAIT_SECONDS`: default `8`
- `DETAIL_WAIT_SECONDS`: default `3`
- `SLEEP_AFTER_OPERATION`: default `true`; wake and unlock before UI automation,
  then switch the display off again
- `API_KEY`: required for `POST /action/*`
- `VW_SPIN`: required for lock and unlock actions

Authenticated action endpoints:

- `POST /action/lock`
- `POST /action/unlock`
- `POST /action/charging/start`
- `POST /action/charging/stop`
- `POST /action/climate/start`
- `POST /action/climate/stop`
- `POST /action/climate/temperature?value=20.5`
- `POST /action/climate/option/automatic-window-heating?value=true`
- `POST /action/climate/option/zone-front-left?value=true`
- `POST /action/climate/option/zone-front-right?value=true`

The target state of charge is read when Volkswagen exposes it in the charging
detail view. The currently observed idle view has no verifiable target-SoC
control, so the connector deliberately does not offer a blind write action.

Send the API key in the `X-API-Key` header. Keep the environment file readable
only by root because it contains the Volkswagen S-PIN.

Install the files from `deploy/` and adjust `/etc/default/vw-app-connector`.

The connector intentionally contains no shared ADB keys. Authorize the key
generated on the target host using the dialog on the phone.

The Android device must not use a secure display PIN, password or pattern when
`SLEEP_AFTER_OPERATION=true`. ADB wakes the display and dismisses the
non-secure keyguard automatically.

## Optional ADB over Wi-Fi

USB remains the default and most reliable transport. To prepare Wi-Fi:

1. Enable Android developer options and wireless debugging.
2. Pair once from the evcc LXC with `adb pair PHONE_IP:PAIRING_PORT`.
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
