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

Explicit charge-detail feedback such as `Lädt gerade nicht` / `Not charging`
or a reached charge target takes precedence over a still-visible stop control.
It reports connected-idle (`B`), rather than active charging (`C`). This is
the app-reported state; an independent wallbox meter remains the reference
for actual power flow.

Due details and location work have priority over routine charge refreshes.
Routine charge work yields for at most one background minimum interval so it
cannot be starved indefinitely.

## Manual Rest Mode

Rest mode is off by default and never starts automatically. The optional
Home Assistant package includes a `Volkswagen App Ruhemodus` switch. Turning
it on calls authenticated `POST /admin/rest/pause`; turning it off calls
`POST /admin/rest/resume`.

Pause waits for a running background read or action to finish, then prevents
new automatic app reads. REST, MQTT and phone-health telemetry remain available.
Vehicle caches are returned with `stale: true` and `REST_MODE_PAUSED`, preserving
their original timestamps. Vehicle actions, read-only app actions and cooldown
probes require explicit resume; a charge-refresh request does not end pause.

The pause and its timestamp are persisted in `CACHE_STATE_DIR/rest-mode.json`,
including across service restarts. Repeated pause/resume calls are idempotent.
Resume queues a charge read without bypassing budgets, minimum intervals,
cooldowns or existing backoff. Details and location retain their normal
refresh schedules. Each cache remains stale with
`REST_MODE_RESUMING` until a fresh read completes. Software still requires an
explicit software refresh. Health exposes `restModePaused`, `restModeSince`
and `restModeReason`; MQTT also discovers a read-only rest-mode binary sensor.

This first version has no automatic connection or charging triggers. Resume
manually before vehicle actions or when fresh vehicle data is required. An
evcc configuration rejecting stale data will use its missing-data behavior;
validate charge control and SoC estimation for the installation. Rest mode
does not guarantee vehicle sleep or reduced 12-V consumption.

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

Volkswagen app 4.6.4 can show a data-saving recovery screen while vehicle data
is loading. The localized recovery wording together with `Datensparmodus` /
`data saving mode` is reported as `APP_DATA_UPDATING`. The connector preserves
cached data as stale and uses the existing persisted shared background backoff;
it does not dismiss the screen, change the data-saving preference or immediately
relaunch the app. This state does not start the server request-limit cooldown.
A data-saving settings label alone does not block reads.

On service startup the charge worker queues one protected early refresh so a
phone connection event missed during downtime does not wait for the parked
interval. Usage limits, minimum intervals, cooldown and shared backoff remain
authoritative and may still suppress that refresh.

## Diagnostics

### Recommendation cards and data-consent prompts

Additional Volkswagen recommendation cards are left untouched when the required
vehicle tile is accessible. Overview navigation retains its bounded scroll for
menu entries displaced by cards; it does not select consent or close buttons.
Closing a workshop recommendation can itself open a reminder dialog.

If overview navigation times out while a recognizable data-consent prompt with
both accept and reject labels is visible, the affected cache reports
`APP_INTERACTION_REQUIRED`, also exposed in `/health.statusReasons`. Open the
Volkswagen app on the connector phone and review the choice manually. The
connector preserves previous values as stale, skips the immediate app-relaunch
retry, and retains the normal background error retry interval and usage limits.
Successful subsequent refreshes clear the affected cache's error.

Detection is deliberately conservative: an inline card alone does not prove
that it blocks the UI, and unknown wording still uses the normal UI error path.
German and English label combinations are covered by synthetic tests; the
specific prompts reported in issue #28 still need real-device UI verification.

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


### Secure screen locks during temporary phone tests

On a securely locked phone, ADB cannot replace manual authentication. The Pixel
10 test passed after manual unlocking with `SLEEP_AFTER_OPERATION=false` in the
isolated test environment. With `true`, putting the display to sleep activated
the device lock between reads. Restore the original production setting after
the temporary test; do not treat successful initial reads as proof that a
securely locked phone supports unattended operation.
