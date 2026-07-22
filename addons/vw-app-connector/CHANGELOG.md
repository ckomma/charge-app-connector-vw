# Changelog

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
