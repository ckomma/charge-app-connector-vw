# Operations, Health and Diagnostics

## Cache Model

The connector refreshes charge, details and location in background. HTTP reads
return immediately from cache and preserve the last successful value with
`stale: true` after refresh failures.

Default cadence:

- charge: 5 minutes while charging, 15 minutes while parked
- details: 12 hours
- location: 4 hours

A newly connected state, a charging-to-idle transition or the first connected
read without a target state of charge schedules one bounded follow-up at the
charging interval. Continued connected-idle state then returns to the parked
interval.

Due details and location work have priority over routine charge refreshes.
Routine charge work yields for at most one background minimum interval so it
cannot be starved indefinitely.

## Usage Protection

Background work has a persisted weighted daily budget. Actions have a separate
persisted daily budget and minimum interval. Service restarts do not reset
counters.

An explicit Volkswagen `too many requests` response starts the global
rate-limit cooldown. Volkswagen stale/unavailable states use a shared persisted
exponential background pause instead. Successfully read source data above the
stale threshold also advances that pause until a fresh charge read clears it.

Location-only `Limited Services` and not-logged-into-vehicle states remain
endpoint-local. They preserve the last successful location and retry at the
location interval without delaying charge or detail refreshes.

## App-Version Quarantine

When the installed app differs from `VERIFIED_APP_VERSION`, reads and MQTT stay
available, while `/health` reports degraded status and
`UNVERIFIED_APP_VERSION`. Write actions return HTTP 409 before action budget is
consumed. Read-only settings actions remain available.

Test every public Volkswagen app update on a real phone in German and English
before changing the verified baseline.

## ADB Recovery

Missing, offline or unreachable ADB transports receive one bounded recovery:
reconnect offline devices, restart the local ADB server only when necessary,
reselect the configured transport and retry once.

Background workers check ADB before acquiring usage budget. An unavailable
transport starts one shared `ADB_UNAVAILABLE` pause so parallel endpoint loops
cannot inflate usage while the phone is unreachable. A successful cache refresh
clears that transport pause.

## Health Checks

Start diagnosis with read-only checks:

```bash
systemctl show vw-app-connector \
  -p ActiveState -p SubState -p Result -p NRestarts -p ExecMainStartTimestamp
curl -sS http://127.0.0.1:9920/health
curl -sS http://127.0.0.1:9920/charge
journalctl -u vw-app-connector --since "30 minutes ago" --no-pager
adb devices -l
```

Check these `/health` areas separately:

- connector `status`, `statusReasons` and ADB transport
- installed versus verified app version and `actionAvailable`
- charge, detail and location cache ages/errors
- `dataWarnings` and Volkswagen source freshness
- background usage, action usage, cooldown and shared backoff

An endpoint-local location state is not evidence that charge or details are
broken. Likewise, old Volkswagen source data is a data-quality warning rather
than automatically a connector software error.

The connector distinguishes Volkswagen's general request limit from the
vehicle-specific too-many-requests response. It also reports vehicle offline,
another active vehicle user, map unavailability and GPS unavailability as
machine-readable reasons. An optimized-battery-use prompt is never activated
automatically; the connector selects only `Nicht jetzt` / `Not now` and reports
`VEHICLE_ENERGY_UNAVAILABLE`.

On service startup the charge worker queues one protected early refresh so a
phone connection event missed during downtime does not wait for the parked
interval. Usage limits, minimum intervals, cooldown and shared backoff remain
authoritative and may still suppress that refresh.

## Diagnostics

Failed UI reads retry once and store an error summary, UI dump and screenshot
below `DIAGNOSTICS_DIR`. These artifacts can contain sensitive operational
context and must not be committed or exposed publicly.

`GET /diagnostics` returns metadata only. `GET /metrics` exposes Prometheus
gauges without triggering a Volkswagen app refresh.

## Operational Safety

- Keep `/etc/default/vw-app-connector` root-readable.
- Protect `/action/*` and `/admin/*` with a non-empty API key.
- Never reset usage counters to work around a limit.
- Do not weaken minimum intervals, cooldowns or version quarantine during
  normal operation.
- Use one authoritative writer for charging decisions.
- Treat location, lock state, MQTT payloads, logs and diagnostics as sensitive.
- Keep the connector on localhost or behind a trusted authenticated network
  boundary.
