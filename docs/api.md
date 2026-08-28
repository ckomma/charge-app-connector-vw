# API Reference

The connector serves cached reads on port `9920` by default. Reading a `GET`
endpoint does not directly open the Volkswagen app. Background workers refresh
the caches according to the configured intervals and budgets.

## Read Endpoints

### `GET /charge`

Returns connection and charging state, state of charge, electric and optional
fuel range, charge telemetry, target state of charge, climate and lock state,
plus cache and Volkswagen-source freshness.

```json
{
  "status": "B",
  "soc": 30,
  "range": 126,
  "fuelRange": null,
  "remainingChargeMinutes": null,
  "chargeRateKmH": null,
  "chargePowerKw": null,
  "targetSoc": null,
  "chargingMode": "",
  "climater": false,
  "locked": false,
  "syncAgeMinutes": 18,
  "sourceAgeMinutes": 18,
  "sourceFreshnessKnown": true,
  "sourceStale": false,
  "observedAt": "2026-06-13T19:20:00+02:00",
  "error": ""
}
```

Status follows the evcc convention:

- `A`: disconnected or connection unknown
- `B`: connected, not charging
- `C`: charging

For PHEVs, `range` remains the electric range and `fuelRange` exposes the fuel
range when available. If the overview shows numeric `0%` while charge details
show `--`, the overview value is used; numeric detail values remain
authoritative.

### `GET /details`

Returns target temperature, automatic window heating, front climate zones,
odometer, service and oil-service intervals, warning status, structured vehicle
health-report items and departure times. Service intervals expose both days and
distance when the app provides them. Departure entries keep the existing
`time` and `day` fields and add a one-based `index` plus `enabled` when the
switch state is exposed.

### `GET /location`

Returns the last successful address, parked duration and navigation
coordinates. Location is separate because map navigation is slower than the
charge read. Treat the response as sensitive data.

### `GET /health`

Does not wake the display. It reports connector status, machine-readable
`statusReasons`, separate `dataWarnings`, ADB transport, app-version policy,
phone power telemetry, cache ages, usage counters, cooldown and shared backoff.
Old but successfully read Volkswagen data is reported as `SOURCE_DATA_STALE`
without automatically turning a healthy connector into a software failure.

### Other reads

- `GET /capabilities`: supported endpoints, actions, ADB transport, MQTT and
  app-version verification. Capability schema version 2 preserves the existing
  lists and adds `actions.byName` plus `vehicle.data` and `vehicle.features`.
  Each dynamic entry distinguishes implemented, observed and currently
  available functionality. `NOT_OBSERVED` is not the same as unsupported;
  `NOT_EXPOSED_BY_VEHICLE` records an explicit negative observation.
- `GET /metrics`: Prometheus text-format health, usage, cache, ADB and version
  gauges.
- `GET /diagnostics`: safe metadata for diagnostic files, never raw UI dumps,
  screenshots, addresses, coordinates or device identifiers.

## Authenticated Actions

Send `X-API-Key` with every action request.

### Vehicle and charging

- `POST /action/lock`
- `POST /action/unlock`
- `POST /action/charging/start`
- `POST /action/charging/stop`
- `POST /action/charging/target-soc?value=80`
- `POST /action/charging/mode?value=immediate`
- `POST /action/charging/settings`
- `POST /action/charging/option/battery-care?value=true`
- `POST /action/charging/option/reduced-ac?value=true`
- `POST /action/charging/option/auto-release-ac?value=true`

Target state of charge supports 50, 60, 70, 80, 90 and 100 percent. Charging
modes are `immediate`, `preferred-times`, `departure` and
`departure-climate`.

### Charging locations

- `POST /action/charging-locations`
- `POST /action/charging-location/settings?name=Home`
- `POST /action/charging-location/direct-soc?name=Home&value=30`
- `POST /action/charging-location/target-soc?name=Home&value=80`
- `POST /action/charging-location/option/reduced-ac?name=Home&value=true`
- `POST /action/charging-location/option/auto-unlock?name=Home&value=true`

Direct-charge limits support 0 through 50 percent in ten-point steps.

### Climate

- `POST /action/climate/start`
- `POST /action/climate/stop`
- `POST /action/climate/temperature?value=20.5`
- `POST /action/climate/option/automatic-window-heating?value=true`
- `POST /action/climate/option/zone-front-left?value=true`
- `POST /action/climate/option/zone-front-right?value=true`

Temperatures support 15.5 through 30.0 degrees Celsius in half-degree steps;
Volkswagen `LO` and `HI` variants map to the lower and upper boundaries.

### Departure times

- `POST /action/departure-time/enabled?index=1&value=true`
- `POST /action/departure-time/time?index=1&value=07:30`
- `POST /action/departure-time/weekdays?index=1&value=monday,wednesday`
- `POST /action/departure-time/repeat?index=1&value=true`

Indices come from cached `GET /details` departure entries and are one-based.
Time uses 24-hour `HH:MM` values in five-minute increments. Weekdays use one or
more comma-separated English weekday identifiers. The connector explicitly
selects the localized editor action `Speichern` / `Save`; Back and
`Abbrechen` / `Cancel` discard editor changes. Scheduling policy, PV decisions
and profiles remain the responsibility of evcc or Home Assistant.
Volkswagen app 4.3.2 automatically enables repetition when an additional
weekday is selected; the action response reports that resulting app state.

The connector verifies displayed values before saving, waits for the save
operation to finish and then reopens the editor to verify persistence. It fails
safely when the app exposes no stable control or save action.

## Asynchronous Actions

Actions are synchronous by default. Add `Prefer: respond-async` and an optional
`Idempotency-Key` to create a serialized background job:

```http
POST /action/charging/target-soc?value=80
Prefer: respond-async
Idempotency-Key: unique-request-id
X-API-Key: replace-with-the-connector-api-key
```

The server returns HTTP 202 with a job ID and `Location` header. Read the result
from authenticated `GET /actions/JOB_ID`.

## Administrative Endpoints

### `POST /admin/refresh/charge`

Queues one early charge-cache refresh, intended for a disconnected-to-connected
event from evcc, a wallbox or Home Assistant. Concurrent requests are
coalesced. The operation consumes normal background budget and preserves the
minimum interval, cooldown, shared backoff and action priority.

### `POST /admin/cooldown/probe`

After correcting a local ADB or phone problem, this endpoint performs one
budgeted charge probe during an active Volkswagen rate-limit cooldown. It
requires authorized ADB and the verified app version. The unchanged original
cooldown is cleared only after success; failures preserve it. Probe attempts
have their own minimum interval and never reset usage counters.
