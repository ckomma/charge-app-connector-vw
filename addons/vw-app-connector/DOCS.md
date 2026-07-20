# Configuration

Required options:

- `adb_serial`: Android ADB serial. Set to `auto` to select the only authorized
  USB ADB device. Keep an explicit serial when multiple Android devices can be
  visible. Required even when `adb_mode` is `wifi` or `auto`.
- `adb_mode`: `usb`, `wifi`, or `auto`.
- `api_key`: random secret required for authenticated write-action and
  administrative cooldown-probe endpoints.

Optional but common:

- `adb_wifi_address`: Android wireless-debugging connection address as
  `IP:Port` when using Wi-Fi ADB.
- `.android/adbkey` below this add-on's config folder can be used to reuse an
  already authorized ADB key. Otherwise Android must approve the generated
  key on first USB or Wi-Fi use.
- `vw_spin`: Volkswagen S-PIN, required only for lock and unlock actions.
- `mqtt_host`, `mqtt_username`, `mqtt_password`: enable MQTT state publishing
  and Home Assistant discovery.
- `background_transient_backoff_max_seconds`: caps the shared exponential pause
  after repeated stale/unavailable Volkswagen app states.
- `source_stale_after_minutes`: controls when the app-reported vehicle-data age
  is exposed as stale and no longer clears that pause.

## Smoke Test

After starting the add-on:

```bash
curl -sS http://127.0.0.1:9920/health
curl -sS http://127.0.0.1:9920/charge
```

A healthy read-only smoke test has `adbState: device`, the expected
`adbTransport`, a verified app version, no cooldown, and a fresh `/charge`
response.

`/health` separates the Volkswagen rate-limit cooldown from the transient
background backoff and exposes vehicle source age, consecutive stale reads and
the last observed intelligent power-saving notice. Cached API responses remain
available during either pause.

`/details` and `/location` perform slower multi-page reads and spend background
budget. Use them only when you intentionally want to refresh those caches.

An explicit Volkswagen rate limit is preserved across restarts. After fixing a
local phone or ADB problem, use the authenticated `POST /admin/cooldown/probe`
endpoint for one budgeted read. It clears the cooldown only on success and does
not reset usage counters or minimum intervals. Repeated probes are limited by
`cooldown_probe_min_interval_seconds`.

## MQTT

Set `mqtt_host` to your broker hostname or IP. The add-on publishes retained
state from existing connector cache updates and Home Assistant discovery
payloads. MQTT does not trigger additional Volkswagen app refreshes and does
not accept vehicle write commands. Write actions remain REST-only through the
authenticated `/action/*` endpoints.

## USB ADB

USB ADB requires Home Assistant OS to expose the Android device to the add-on.
Authorize the generated ADB key on the phone. If the phone is not visible in
ADB, use ADB over Wi-Fi while keeping the phone powered by USB.
