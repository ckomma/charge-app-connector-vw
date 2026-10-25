# Changelog

## 0.1.32

- Rename the Home Assistant source-data warning to `Vehicle data outdated`
  and classify it as a diagnostic entity with a regular on/off state instead
  of the `problem` device class.
- Preserve existing entity identities, API fields, source-age thresholds and
  background retry behavior. Explain the distinction between older vehicle
  data and connector health in the integration and operations guides.

## 0.1.31

- Add an optional manual rest mode, off by default, with authenticated pause
  and resume endpoints and Home Assistant example controls.
- Persist explicit pauses across service restarts; keep REST and MQTT online,
  mark paused vehicle data stale and block app actions until explicit resume.
- Resume with a protected charge refresh; retain normal detail/location
  schedules, usage counters, minimum intervals, cooldowns and version policy.
- Expose rest-mode status through health and MQTT discovery.
- Verify real-phone pause/resume, restart persistence and evcc charging
  start/stop during pause. Restore the original target and operating settings.

## 0.1.30

- Report connected-idle (`B`) when charge details explicitly say charging has
  stopped or the charge target is reached, even if the stop control remains
  visible. Prevent `Lädt gerade nicht` from being classified as active charging.
- Add German and English regression coverage and verify the reached-target
  state on a real phone with Volkswagen app 4.8.1 in German.
- Verify evcc charging start and stop with the connector service paused;
  restore the original charging target and all test settings afterward.
  Preserve existing budgets, cooldowns, authentication and app-version policy.

## 0.1.29

- Support Volkswagen 4.8.1 localized vehicle-location map controls in German
  and English, preserving previous app labels and bounded navigation retries.
- Exclude vehicle-location control labels from address parsing.
- Keep the packaged shell launcher Linux-compatible when staging on Windows.
- Verify the Volkswagen 4.8.1 write-action baseline on a real phone in German
  and English, including climate, charging and physically confirmed unlocking.
  Preserve explicit version settings, usage budgets, cooldowns and API
  authentication.

## 0.1.28

- Recognize spelled-out singular synchronization ages in German and English,
  including `Synchronisiert vor Eine Minute` and `Synced one minute ago`, so
  fresh vehicle data can clear the existing stale-source background pause.
- Verify active charging through the production connector and Home Assistant
  after a fresh charge read. Preserve usage budgets, cooldowns and vehicle state.

## 0.1.27

- Accept the new German `Parkt seit` location label for vehicle-marker
  selection and address parsing, preserving existing German/English labels.

- Keep the climate action page open for bounded command confirmation and
  verify the requested state before reporting success. Unconfirmed commands
  return HTTP 503 with `CLIMATE_ACTION_UNCONFIRMED`, without repeating the tap.
- Expose optional battery state of health and its localized calculation date
  through details and Home Assistant MQTT discovery, separate from charge SoC.
- Add cache-only `/software` and authenticated read-only
  `/action/software/refresh` for vehicle version, update status and release-note
  availability, without additional background polling or vehicle writes.
- Recognize the Volkswagen 4.6.4 data-saving recovery screen in German and
  English as `APP_DATA_UPDATING`, preserving caches and existing backoff without
  changing preferences or treating it as a request-limit cooldown.
- Prepare the 4.6.4 write baseline after German/English real-phone verification.
  Treat pending locking/unlocking labels as unknown, keep the app alive for
  completion feedback and reject unconfirmed lock results.

## 0.1.26

- Report `APP_INTERACTION_REQUIRED` when overview navigation fails with a
  recognized data-consent choice visible. Preserve cached values and normal
  retry limits without choosing a preference or immediately relaunching the app.
- Keep accessible vehicle tiles usable alongside recommendation cards and
  expose the interaction reason in health for charge, details and location.
- Verify normal read navigation in German and English on Redmi and Pixel 10,
  including successful background charge reads on both devices. The specific
  consent prompt remains covered by synthetic tests, not a live reproduction.

## 0.1.25

- Add observed, vehicle-specific data and action availability to the additive
  capabilities v2 response while preserving the existing capability lists.
- Expose structured service, oil-service, warning, report-item and departure-time
  details without adding history, schedules or automation state.
- Add authenticated primitive actions for departure-time activation, time,
  weekdays and repetition with semantic UI and persistence verification.
- Select the localized editor action `Speichern` / `Save`; Back and
  `Abbrechen` / `Cancel` intentionally discard editor changes in Volkswagen
  app 4.3.2.
- Treat already-matching editor values as successful no-ops because the app
  only exposes `Speichern` / `Save` after an actual change.
- Expose a read-only departure-editor settings action, control its charging
  and climate switches, and report preferred-time state without attempting the
  infotainment-only edit.
- Recognize an explicit departure-time save failure without blind retry.
- Add vehicle-specific maximum-current and solar, bidirectional and discharge
  charging-mode primitives using Volkswagen app 4.3.2 semantic labels.
- Open the map route directly with a guarded vehicle-overview fallback and
  distinguish map/GPS errors from global connector failures.
- Distinguish request-limit, offline, active-user and optimized-battery-use
  states; never activate optimized battery use automatically.
- Queue one usage-protected charge refresh on connector startup and expose
  health status reasons and data warnings through MQTT discovery.

## 0.1.24

- Treat ADB transport-probe timeouts as transient transport unavailability so
  background refreshes use bounded recovery and the shared `ADB_UNAVAILABLE`
  backoff instead of emitting an uncaught traceback.
- Keep API-visible ADB timeout details free of device identifiers.

## 0.1.23

- Restructure the project documentation into a concise entry-point README and
  focused API, configuration, operations and integration guides.

## 0.1.22

- Add Volkswagen app `4.3.2` support and make it the guarded write-action
  baseline after German and English real-device verification on Redmi and
  Pixel phones.
- Retry the semantic battery-range tile tap once when the charge overview is
  still visible after the detail-navigation timeout.

## 0.1.21

- Treat Volkswagen `Limited Services` and not-logged-into-vehicle location
  states as endpoint-local availability conditions instead of a global app
  outage.
- Retry those expected states at the normal location interval without delaying
  charge or detail refreshes, and expose precise degraded-health reasons.
- Reduce repeated warning noise while preserving the last successful location
  and usage accounting.

## 0.1.20

- Fix the Home Assistant MQTT vehicle-lock entity so a connector value of
  `locked: true` is displayed as locked instead of unlocked.
- Preserve the existing MQTT discovery unique ID so current Home Assistant
  entity IDs, automations and history continue to work.

## 0.1.19

- Add Volkswagen app `4.2.1` support and make it the guarded write-action
  baseline after German and English real-device verification on Redmi and
  Pixel phones.
- Support the Volkswagen `4.2.1` location card and centered vehicle-marker
  layout.
- Scroll charging switches that are semantically exposed but still clipped
  behind the persistent bottom navigation before tapping them.

## 0.1.18

- Keep successfully read but old Volkswagen source data as a separate data
  warning instead of degrading an otherwise healthy connector.
- Expose machine-readable health reasons and dedicated MQTT entities for
  vehicle source age, stale source data and background backoff.
- Add an authenticated, event-driven charge refresh for wallbox, evcc and Home
  Assistant connection events while preserving usage limits, cooldowns,
  transient backoff and action priority.

## 0.1.17

- Prevent unavailable ADB transports from consuming background usage before
  the Volkswagen app can be reached.
- Coordinate charge, details and location through one persisted exponential
  ADB backoff so parallel retry loops cannot inflate the daily usage counter.
- Treat the Volkswagen `Limited Services` location state as a shared transient
  app outage and clear an ADB-triggered pause after a successful cache refresh.

## 0.1.16

- Recheck the live charging state immediately before start/stop taps to narrow
  races with manual Volkswagen app use or another automation.
- Add a Home Assistant `Actions Ready` guard for failed, stale, source-stale or
  old charge data, app-version quarantine and Volkswagen cooldowns; document a
  single-writer/manual-override policy for charging automations.
- Recover missing, offline and unreachable ADB transports with one bounded
  retry and follow a `C` to `B` transition once at the charging interval.
- Reject stale connector data in the evcc example and preserve a numeric zero
  state of charge for empty PHEV batteries when the detail view shows `--`.

## 0.1.15

- Verify Volkswagen app `4.1.1` in German and English on Redmi and Pixel
  phones and make it the guarded write-action baseline.
- Make concurrent action idempotency atomic and keep background work paused
  until every overlapping action has completed.
- Preserve real cache-success timestamps and shared transient backoff across
  action-result patches; publish cache errors and current backoff through MQTT.
- Redact device identifiers from API-visible ADB errors, cap jittered backoff
  at its configured maximum and bound priority yielding so charge telemetry
  cannot starve indefinitely.
- Avoid immediate retries for semantic Volkswagen stale, unavailable and rate
  limit states; coordinate transient failures through a persisted adaptive
  background backoff.
- Expose vehicle source freshness and intelligent power-saving notice telemetry
  through cached charge data, health and Prometheus metrics.

## 0.1.14

- Perform one bounded five-minute charge follow-up after a newly connected
  vehicle or the first connected read without a target state of charge, then
  return to the idle interval to preserve Volkswagen app usage safeguards.

## 0.1.13

- Distinguish transient stale app data from explicit Volkswagen rate limits,
  expose cooldown reason and expiry, and add an authenticated one-shot recovery
  probe that preserves usage safeguards.

## 0.1.12

- Dismiss the localized intelligent power-saving notice before navigating the
  Volkswagen overview.
- Report an explicitly disconnected charging cable as vehicle status `A`.
- Improve vehicle-marker selection on Volkswagen location maps and accept
  odometer values containing localized spacing separators.
- Let Home Assistant derive tracker state from GPS zones and rename the MQTT
  connector status entity to connector health.

## 0.1.11

- Verify Volkswagen app `4.0.3` in German and English on the production Redmi.
- Make overview, climate and charging selectors tolerant of app `4.0.3`
  punctuation and wording changes.
- Avoid treating `Charging station shows current status` as active charging.
- Improve recovery when Android/MIUI system overlays take focus during UI
  navigation.

## 0.1.10

- Reduce expected Volkswagen cooldown/stale-state log noise by logging
  `UsageLimit` refresh failures as concise warnings instead of stack traces.
- Build the add-on image from the packaged add-on files instead of downloading
  connector sources from a fixed GitHub raw URL.
