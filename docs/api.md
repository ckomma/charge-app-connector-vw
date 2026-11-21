# API Reference

The connector serves cached reads on port `9920` by default. Reading a `GET`
endpoint does not directly open the Volkswagen app. Background workers refresh
the caches according to the configured intervals and budgets.

Cached charge, details, location, software and latest-trip payloads also report
live refresh activity:

- `refreshing`: a refresh is running, including protected waits for app access
  or the minimum background interval.
- `refreshRequested`: an early refresh is queued or still being processed.
- `refreshStartedAt`: start time of the running refresh, otherwise empty.
- `retryInSeconds`: remaining local/shared retry pause, otherwise zero. This
  is not the countdown to the next normal periodic read.

For example, `/trips/latest` can show `refreshing: true` while still returning
the previous paired trip and odometer. `observedAt` and `lastSuccessfulAt`
continue to describe that previous measurement until the new read succeeds.
Progress never marks the vehicle data fresh or changes its timestamps.
An initial read still returns HTTP 503 until the first successful reading.

`/health.refreshActivity` groups the live state for `charge`, `details`,
`location`, `trips` and `software`. Each entry contains the fields above and a
`state` of `running`, `waiting`, `queued`, `idle` or `disabled`.
`/capabilities.caches` exposes the same activity metadata for enabled caches.
Activity is process-local and is not restored as a running job after restart.

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

`batteryHealthPercent` is the explicitly labelled battery state of health
(0–100%, optional), distinct from charge SoC. `batteryHealthCalculatedOn` retains
the app's localized calculation date as a string; it is not an ISO timestamp.
Missing cards return `null` / an empty date and capability `NOT_OBSERVED`.
An exposed card without a valid percentage records an unavailable capability.
The existing detail read includes bounded scrolling within the health report;
its weighted budget cost remains three.

### `GET /trips/latest`

Returns the last single trip observed in the Volkswagen app when
`TRIP_POLL_ENABLED=true`. The vehicle and account must expose **Driving data**
and a readable **Last single trip** card. GET reads the persisted cache only.
It returns HTTP 503 before the first successful observation or when polling is
disabled, and HTTP 200 with the last good result if a later read fails.

The response includes `tripEndedAtLocal` (the displayed trip end, without a
timezone), `distanceKm`, `averageSpeedKmh`, `drivingMinutes`, optional
`electricConsumption` and `fuelConsumption` objects with a `value` and the
displayed `unit`, and `observedAt` (the connector's read time). The
`odometerKmAtObservation` is the mileage that prompted the read.

The connector checks the odometer at `TRIP_INTERVAL_SECONDS` (default one
hour), independently of `DETAIL_INTERVAL_SECONDS`. Each check reserves two
background budget units for the vehicle health report and the potential
driving data read. The connector opens the trip card on the first usable
mileage and after an increase. It reads both pages in one phone session
and publishes the mileage and trip fields together in this endpoint.
If mileage increases and the app still shows the previous trip, the previous
pair remains available with `stale: true` and `TRIP_NOT_UPDATED`. The next
attempt waits at least the configured trip interval (one hour by default) and
the background error retry interval. Missing trips and unavailable or older
mileage use the same bounded retry policy. Existing cooldown,
backoff and rest-mode rules also apply.

If an existing charge refresh can see the Driving data summary in the vehicle
overview, a change in its displayed distance or consumption queues an early
trip check. The summary is only a trigger. No extra scroll or UI dump is
needed to look for it. The configured trip interval still applies when the
summary is absent or unchanged. Early checks use the same budget and cooldown
rules as scheduled checks.

Trip, detail and location reads preserve a short buffer of at most two charge
budget units. If remaining budget cannot cover the configured schedules until
local midnight, the connector spaces all four background read types further
apart, weighted by their configured intervals and costs. It reports
`BACKGROUND_BUDGET_PACED` while waiting and preserves admission timestamps across
restarts. SoC polling may consequently run less often than the configured
interval. An early trip request takes priority over routine detail/location
work when eligible; it cannot bypass pacing or a pending retry.

An optional read that would spend the short charge buffer reports
`BACKGROUND_BUDGET_RESERVED`. Both budget categories are exposed in
`/health.dataWarnings`. No daily limit or cooldown is bypassed, counters are not
reset, and extremely small or already exhausted budgets cannot guarantee fresh
data for every endpoint.

An opened vehicle health report with no contents is checked for a bounded
period, then reported as `VEHICLE_REPORT_NOT_READY` without immediately reopening
the app. Details retry after at least one hour; trip retries use at least the
trip interval. This state degrades connector health. Optional trip, detail and
location caches also degrade health after twice their normal interval without a
successful read (at least one hour), using `TRIP_DATA_STALE`,
`DETAIL_DATA_STALE` or `LOCATION_DATA_STALE`. Existing HA problem sensors that
check connector health therefore show these prolonged failures.

Use `odometerKmAtObservation` from this response as the automation trigger
and consume the trip fields from the same response. `/details` continues
to report its own mileage observation with its existing schedule. The trip
record contains no trip start odometer. The app can group drives resumed
within two hours into one single trip. If multiple trips appear between
checks, this endpoint captures the latest visible one. Treat trip timestamps
and mileage as sensitive data.

### `GET /software`

Cache-only vehicle software information, separate from the Android app version:

```json
{
  "available": true,
  "installedVersion": "5.4",
  "updateAvailable": false,
  "updateStatus": "up-to-date",
  "releaseNotesAvailable": true
}
```

The payload also carries `observedAt`, `lastSuccessfulAt`, `stale`, `error`,
`errorCategory` and `refreshDurationSeconds`. Update status is `available`,
`up-to-date`, `unknown` or `not-exposed`. A missing software menu yields
`available: false` and null version/update fields. Unknown or contradictory
status is never interpreted as “up-to-date”. `releaseNotesAvailable` reports
the visible link, without opening release notes or installing an update.

Software is **not polled automatically**. Before its first explicit refresh,
GET returns HTTP 503 with `NOT_OBSERVED`; a persisted successful observation
returns HTTP 200. Observations older than `DETAIL_INTERVAL_SECONDS` are marked
stale without opening the app. Home Assistant MQTT discovery includes battery
health/date, vehicle software, update status, update availability and release
notes availability. Missing or stale optional readings become unavailable.

Refresh with authenticated `POST /action/software/refresh` (or the existing
asynchronous action mechanism). This opens vehicle settings and the localized
software page, consumes one action-budget unit and respects action priority,
minimum intervals and cooldowns. It is read-only and therefore permitted while
vehicle writes are version-quarantined. HTTP errors preserve the last good
software observation as stale. `SOFTWARE_SERVICES_UNAVAILABLE` denotes the app's
update-service error and does not pause other endpoints. No update-installation
action is provided; the app directs installation to vehicle infotainment.

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
Its shared retry pause does not grow exponentially: it is capped at
`BACKGROUND_ERROR_RETRY_SECONDS` (default 15 minutes), including old persisted
pauses after restart. Normal refresh intervals, background budgets, minimum
intervals and cooldowns still apply, so this is not a guaranteed update deadline.

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

Climate start/stop waits for the action control to change and then checks the
observed vehicle state. A pending or unconfirmed result returns HTTP 503 with
`CLIMATE_ACTION_UNCONFIRMED`, rather than HTTP 200 with the opposite state. The
wait is bounded, does not repeat the command and preserves normal action usage,
cooldowns and app-version quarantine.

### Vehicle and charging

- `POST /action/lock`
- `POST /action/unlock`
- `POST /action/charging/start`
- `POST /action/charging/stop`
- `POST /action/charging/target-soc?value=80`
- `POST /action/charging/max-current?value=13`
- `POST /action/charging/mode?value=immediate`
- `POST /action/charging/settings`
- `POST /action/charging/option/battery-care?value=true`
- `POST /action/charging/option/reduced-ac?value=true`
- `POST /action/charging/option/auto-release-ac?value=true`

Target state of charge supports 50, 60, 70, 80, 90 and 100 percent. Maximum
charging current is accepted only when the vehicle exposes that setting.
Charging modes are `immediate`, `preferred-times`, `departure`,
`departure-climate`, `solar`, `bidirectional` and `discharge`; availability is
vehicle-specific and reported by `GET /capabilities`.

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

- `POST /action/departure-time/settings?index=1`
- `POST /action/departure-time/enabled?index=1&value=true`
- `POST /action/departure-time/time?index=1&value=07:30`
- `POST /action/departure-time/weekdays?index=1&value=monday,wednesday`
- `POST /action/departure-time/repeat?index=1&value=true`
- `POST /action/departure-time/charging?index=1&value=true`
- `POST /action/departure-time/climate?index=1&value=true`

Indices come from cached `GET /details` departure entries and are one-based.
The read-only `settings` action reports the editor's `charging`, `climate`,
`preferredTimesAvailable` and `preferredTimesActive` state. Preferred times
remain read-only because Volkswagen app 4.3.2 directs their configuration to
the vehicle infotainment system. Time uses 24-hour `HH:MM` values in
five-minute increments. Weekdays use one or
more comma-separated English weekday identifiers. The connector explicitly
selects the localized editor action `Speichern` / `Save`; Back and
`Abbrechen` / `Cancel` discard editor changes. Scheduling policy, PV decisions
and profiles remain the responsibility of evcc or Home Assistant.
Volkswagen app 4.3.2 automatically enables repetition when an additional
weekday is selected; the action response reports that resulting app state.

The connector verifies displayed values before saving, waits for the save
operation to finish and then reopens the editor to verify persistence. It fails
safely when the app exposes no stable control or save action. Volkswagen's
explicit `Abfahrtszeit speichern fehlgeschlagen` / `Error saving departure
time` result is returned immediately and is not blindly retried.

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

### `POST /admin/rest/pause` and `POST /admin/rest/resume`

Require `X-API-Key`. Pause stops automatic app polling after running work
finishes; resume queues a protected fresh charge read. Responses include `paused`,
`since`, `reason` and `changed`. Both operations are idempotent and preserve
usage counters. `since` is the most recent pause timestamp, also after resume.
The default is unpaused; an explicitly requested pause survives restart.

Cache GETs remain available during pause but report `stale: true` and
`errorCategory: REST_MODE_PAUSED`. After resume, old values report
`REST_MODE_RESUMING` until replaced by fresh measurements. Explicit app actions
are rejected during pause with `REST_MODE_PAUSED`; resume first.

These endpoints never start or stop vehicle charging. See the manual rest-mode
section in [operations](operations.md) for Home Assistant and evcc behavior.

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

Lock/unlock completion is checked before success is returned. Pending labels
such as `Wird entriegelt` / `Is being unlocked` produce an unknown `locked`
value rather than a completed state. The app remains open while the vehicle
command finishes. Missing completion or a mismatching final read returns
HTTP 503 / `LOCK_ACTION_UNCONFIRMED`; Volkswagen's unstable-connection failure
returns `LOCK_ACTION_FAILED`. Commands are not blindly retapped.
