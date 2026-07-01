# Volkswagen App Connector Memory

Durable implementation and operations notes for future Codex sessions. Keep
this file free of credentials, device identifiers, VINs, addresses,
coordinates, screenshots, raw UI dumps, and private network details.

## Xiaomi And MIUI

- Xiaomi/MIUI pocket mode can cover the Volkswagen app with the message
  `Den Kopfhörerbereich nicht abdecken` when the proximity sensor is covered,
  for example when the phone is placed display-down.
- While this overlay is active, Android can return both an empty accessibility
  hierarchy and an empty screenshot. This is a device-state issue, not a
  Volkswagen API or parsing failure.
- The connector dismisses a recognized localized overlay, or an empty
  hierarchy while the Volkswagen app is foreground, with one volume-up key
  event before retrying.
- On the verified Redmi/MIUI device, `KEYCODE_WAKEUP` may be ignored.
  `KEYCODE_POWER` is the required wake fallback.
- MIUI may expose the foreground package through `mObscuringWindow` instead of
  `mCurrentFocus`; foreground detection must support both.
- Keep the proximity sensor uncovered and place the phone display-up for the
  most reliable unattended operation.
- These workarounds are additive fallbacks and do not make the connector
  Xiaomi-only. If false positives appear on another manufacturer, gate the
  volume-key recovery by Android manufacturer or system properties.

## Screen And Refresh Behavior

- Keep the display awake during a multi-step UI read with
  `svc power stayon true`; the connector's existing sleep cleanup turns the
  display off after the operation.
- Location reads use the same UI retry and overlay-recovery path as charge and
  detail reads.
- Since 2026-07-01, app startup first uses the direct Volkswagen
  `SingleActivity` component and falls back to the launcher intent only if the
  app is not foreground. Foreground detection must prefer `mCurrentFocus` and
  treat an active non-Volkswagen focus such as launcher or notification shade
  as not foreground; `topResumedActivity`, `mResumedActivity`,
  `mObscuringWindow` and `mFocusedApp` are compatibility fallbacks.
- If the Volkswagen app is foreground but the accessibility dump still contains
  only launcher/system resource nodes, retry once with `uiautomator dump
  --compressed`. This handles Android/Compose dump variants without accepting
  a launcher tree as Volkswagen UI.
- A failed background refresh waits
  `BACKGROUND_ERROR_RETRY_SECONDS`, default 900 seconds, before another
  attempt. This prevents a persistent UI obstruction from consuming the daily
  background budget every five minutes.
- USB remains the preferred transport in `ADB_MODE=auto`; configured ADB Wi-Fi
  is only the fallback while USB is unavailable.

## Live Test Budget Policy

- For every explicitly requested live phone or runtime test, daily budgets must
  not constrain test execution. Before testing, save the exact configured
  `ACTION_DAILY_LIMIT` and `BACKGROUND_DAILY_LIMIT` values root-only and raise
  both limits temporarily to a practically unlimited test value.
- Do not reset, reduce or replace persisted usage counters. Test operations
  remain visible in usage telemetry even while the temporary limits are high.
- Do not disable action/background minimum intervals, Volkswagen rate-limit
  cooldowns, API-key authentication or app-version quarantine.
- After vehicle state restoration and test completion, restore both exact
  production limits, restart only `vw-app-connector`, and verify `/health`, the
  usage counters and the absence of a cooldown. Runtime restoration takes
  priority over further testing if a test wrapper or command fails.

## MQTT And Home Assistant

- MQTT is an optional, read-only output enabled by `MQTT_HOST`. REST remains
  available and authoritative for vehicle actions and evcc.
- Home Assistant App/Add-on packaging lives under `deploy/home-assistant/` as
  an optional deployment method alongside systemd and Docker Compose. The
  packaging script stages the current connector sources into `build/` and the
  app defaults to manual boot so an unconfigured HA OS instance does not start
  consuming app budget or producing ADB errors.
- The HA app uses Supervisor-managed `/data` for usage state, caches,
  diagnostics and generated ADB keys. The app healthcheck targets
  `/capabilities`, not `/health`, so Supervisor can distinguish a running
  connector process from an unavailable phone or unconfigured ADB transport.
- HA OS read-only E2E verification on 2026-06-30 used temporary Wi-Fi ADB and
  raised HA app daily budgets only for the test. `/health`, `/charge` and
  `/location` worked through the HA app with Volkswagen app `3.63.2`; location
  returned address and coordinates but those values were not retained. Details
  remained initializing during the limited test window. No vehicle write action
  was executed because the vehicle was not connected to a wallbox and was being
  used with another account. The temporary ADB key and HA app options were
  removed/restored afterward, and the production connector remained healthy
  with normal limits.
- Publish retained `charge`, `details`, `location`, `health` and `availability`
  topics only from existing cache updates or connection startup. MQTT must not
  trigger a Volkswagen app refresh or consume a usage-budget unit.
- Home Assistant discovery creates one connector device with state, phone,
  usage and GPS entities. Location topics contain sensitive address and
  coordinate data and require a trusted broker.
- Broker or health-publication failures must be logged without changing the
  result of a successful cache refresh.
- Location marker details can expose the address as a separate TextView instead
  of a single combined string containing `Geparkt seit` / `Parked since`.
  Verified on 2026-06-17 with the production Redmi connected to the evcc LXC:
  a direct live location read returned both an address and navigation
  coordinates after using the visible map viewport for the marker tap and a
  position-based address fallback. The current view did not expose a parked
  duration label, so `parkedDuration` can remain empty even when address and
  coordinates are functional.
- Verified again on 2026-06-19 with Volkswagen app `3.63.2`: nearby charging
  POIs can overlap the map center after Car Locate. The vehicle marker label is
  rendered inside the Google Maps canvas and has no accessibility bounds. Tap
  the label position proportionally above the refreshed map center, then verify
  that the selected detail card contains the vehicle name parsed from the
  German or English overview before accepting its address or route intent.
  The complete location flow was live-verified on the production Redmi in both
  German and English; address, parked duration and navigation coordinates were
  present in both localizations.
- On 2026-06-29, Home Assistant history showed changing location addresses with
  an unchanged coordinate set. Root cause: Android `dumpsys activity activities`
  can retain older Google Maps navigation intents, and the connector used the
  first `google.navigation:q=` match. The parser now uses the latest match from
  the activity dump. A production Redmi refresh after deployment returned a new
  coordinate set, no location error, no cooldown, and restored normal production
  usage limits afterward.
- Follow-up on 2026-06-30 showed the new coordinate set was still not the
  current address. External geocoding was used only for diagnostics and showed
  the connector coordinates were far from the displayed address. Stopping
  Google Maps before tapping the Volkswagen Route button caused the subsequent
  Google Maps navigation intent to match the geocoded address within roughly
  ten metres. The connector now stops `MAPS_PACKAGE` before opening Route,
  still deriving coordinates through Google Maps rather than an external
  geocoding fallback. The fix was deployed to the production Redmi runtime,
  live-verified with a fresh location refresh, and production limits were
  restored.

## Pixel 10 / Android 16

- Verified on 2026-06-16 in the evcc LXC on the Proxmox host: the Pixel 10 is
  visible through ADB over USB, the Volkswagen app is installed and signed in,
  battery telemetry is readable, and the Volkswagen app can be foreground.
- Pixel devices use standard Android USB debugging; Xiaomi's extra
  `USB debugging (Security settings)` option is not required.
- Android 16 can report a successful `uiautomator dump /sdcard/name.xml` while
  `cat /sdcard/name.xml` fails. The same dump is readable through
  `/storage/emulated/0/name.xml`, so connector UI-dump reads must keep that
  fallback.
- Android 16 exposes `mCurrentFocus`, `mFocusedApp`, and `mObscuringWindow`
  reliably in `dumpsys window`; `dumpsys window windows` may omit those summary
  fields. Foreground detection must use the broader `dumpsys window` output.
- With a secure Pixel keyguard, unattended runs need `SLEEP_AFTER_OPERATION=false`
  or the secure lock disabled. If the connector puts the screen to sleep, the
  next wake cannot dismiss the secure keyguard and app operations fail before
  parsing.
- For every Pixel test, keep screen-off cleanup disabled for the entire time
  the Pixel is selected: set `SLEEP_AFTER_OPERATION=false` and keep Android's
  stay-awake mode active. Do not send sleep or power-key cleanup events during
  the Pixel test. The temporary stay-awake setting may be cleared only after
  testing is complete and the runtime has been restored to the Redmi.
- Verified on 2026-06-19 with the Pixel 10 temporarily selected for the
  compatibility test: `/health` reported USB transport, USB power and an
  authorized ADB device. The Redmi remains the production phone; do not leave
  the Pixel configured as the runtime `ADB_SERIAL` after Pixel testing.
- After the 2026-06-19 Pixel test, the runtime target was restored to the Redmi
  over USB with `ADB_MODE=auto` and `SLEEP_AFTER_OPERATION=true`. `/health` and
  `/charge` were healthy, the charge cache was fresh and the display-off
  cleanup worked.
- Functional Pixel/USB test on 2026-06-16:
  `/health`, `/charge`, and `/details` worked after adding the UI-dump and
  foreground-detection fallbacks. `/location` reached the Volkswagen app but
  failed at address parsing (`Volkswagen vehicle address not found`).
  Climate start/stop, charging stop/start, automatic window heating, and front
  climate-zone toggles worked and were restored to their original values.
  Unlock initially failed because the S-PIN dialog was not found while
  app-level fingerprint/face unlock was enabled. After disabling fingerprint
  and face unlock for apps, unlock succeeded and lock restore succeeded. Setting
  target temperature to 21.0 failed verification both before and after disabling
  fingerprint/face unlock for apps; restoring 20.5 succeeded.
- Pixel/USB follow-up on 2026-06-19: direct charge, details and location reads
  succeeded. The location result contained an address, parked duration and
  navigation coordinates. The previous address-parsing failure is resolved by
  the current location parser.
- The physical-display lock gesture fix was live-verified on the Pixel 10 on
  2026-06-22 with Volkswagen app `3.63.2` over USB. The runtime temporarily
  used `SLEEP_AFTER_OPERATION=false` and kept the Pixel awake because of its
  secure keyguard.
  A locked -> unlocked -> locked API test returned HTTP 200 for both actions,
  physically changed the vehicle state, and restored the original lock,
  charging and climate states. The runtime was then restored to the production
  Redmi with `ADB_MODE=auto`, `SLEEP_AFTER_OPERATION=true`, healthy Wi-Fi
  fallback, the normal 20-action limit and no cooldown.
- The Pixel temperature selector places its clickable values lower than the old
  fixed tap coordinates. After a temperature change, a side value can also be
  wider than the selected center value. Selecting the visible numeric value by
  its accessibility bounds and parsing the value nearest the horizontal screen
  center fixed both problems. A live 20.5 -> 21.0 -> 20.5 test succeeded and
  restored the original temperature.

## Verification

- openHAB 5.1.4 end-to-end verification on 2026-06-20/21 used the separate
  Home Assistant binding with the existing MQTT broker. The discovered
  `Volkswagen App Connector` Thing was online and read-only. After adding
  discovery entries for automatic window heating and the two front climate
  zones, the live Thing updated automatically from 31 to 34 channels.
- A live target-SoC action verified the cache publication fix: changing
  100 -> 90 -> 100 percent updated the linked openHAB
  `Number:Dimensionless` Item immediately from `1` to `0.9` and back to `1`.
  This factor representation is openHAB's unit conversion for percent values,
  not a connector scaling error.
- The same openHAB test exercised charging stop/start, climate start/stop,
  target temperature, automatic window heating, both front climate zones,
  target SoC, Battery Care and reduced AC current. All successful changes were
  restored to their recorded initial values. Temporary openHAB Items and links
  were removed afterward.
- Lock succeeded during the live test, but the connector's unlock gesture did
  not open the S-PIN dialog on the production Redmi. Manual app unlock then
  auto-relocked when no door was opened; a later physical unlock was observed
  correctly.
- A supervised manual Redmi retest on 2026-06-22 started with the vehicle
  locked. The lock slider appeared, a downward swipe to unlock opened the
  S-PIN dialog, entering the S-PIN closed the dialog without a separate
  confirmation, and the vehicle physically unlocked. This confirms that the
  current app, S-PIN dialog and vehicle-side unlock action work manually. The
  connector failure was therefore isolated to its automated swipe geometry,
  not S-PIN entry or API authentication.
- The Redmi lock/unlock regression was fixed and live-verified on 2026-06-22.
  The connector had scaled the gesture from the MIUI accessibility viewport
  height of 2168 pixels, placing the unlock start above the Compose slider.
  Scaling from the physical 1080x2400 display restores the verified gesture.
  The connector now also waits for the current lock control and the complete
  S-PIN dialog, and saves diagnostics immediately if either step fails. A live
  locked -> unlocked -> locked API test returned HTTP 200 for both actions and
  restored the original vehicle, charging and climate states. The temporary
  test action limit was restored to 20 without resetting persisted usage.

- App-version quarantine and asynchronous actions were deployed and verified on
  2026-06-20. A temporary version mismatch kept `/health` and `/charge` at HTTP
  200, reported `status: degraded` and `actionAvailable: false`, rejected a
  charging write with HTTP 409, and did not increment the action counter.
- With the verified version restored, two asynchronous read-only settings calls
  using the same `Idempotency-Key` returned the same job. The job completed as
  `succeeded` and consumed one action-budget unit. The legacy synchronous
  settings call still returned HTTP 200 after the configured minimum interval.
- `VERIFIED_APP_VERSION=3.63.2` is active on the verified runtime. Quarantine
  applies only to writes; cached REST reads, MQTT and read-only settings actions
  remain available.

- Live deployment verification on 2026-06-19 with Volkswagen app `3.63.2`:
  global target SoC changed 100 -> 90 -> 100 successfully. Battery Care changed
  true -> false -> true and reduced AC changed false -> true -> false. Enabling
  Battery Care at a 100% target opens a localized confirmation dialog and later
  resets the displayed target to 80%; the test restored the original 100% target.
- The same live test found that the charging-mode row is readable as
  `Ladeverfahren. Sofortladen. Ladeverfahren ändern` but its Compose node is not
  clickable and bounds-relative taps do not open the selector. No blind fixed
  coordinate was added, and the mode remained `Sofortladen`.
- The production account exposed departure times but no configured charging
  location, so the charging-location list correctly returned empty and no
  location-specific write was possible.
- MIUI can ignore several overview swipes and intermittently return an empty UI
  tree. Overview searches for lower tiles need bounds-derived swipes, the existing
  overlay recovery, and enough bounded retry attempts.
- The action limit was temporarily raised for the explicitly requested live test
  and restored afterward. The persisted action count can remain above the normal
  daily limit until the next local-day rollover; do not reset it manually.

- The latest production-Redmi verification on 2026-06-19 used Volkswagen app
  `3.63.2` (`versionCode 41262`). Record app versions as tested baselines, not
  strict compatibility pins.
- On 2026-07-01, the foreground/startup/UI-dump refactoring was deployed for
  live verification before commit. Production Redmi E2E passed `/health`,
  `/charge`, `/details`, `/location`, charging settings, climate start/stop,
  and unlock/lock with vehicle state restored and normal usage limits restored.
  Pixel 10 USB E2E also passed the same read and write-action path while
  temporarily selected with test budgets, then the runtime was restored to the
  production Redmi.
- On 2026-07-01, the overview-menu readiness fix for transient
  `Discover Volkswagen` banners was deployed to the production Redmi runtime
  and live-verified. Direct live reads passed charge, details and location,
  including details navigation through climate settings, vehicle report and
  departure times. API write E2E passed climate start/stop and unlock/lock,
  with climate off and the vehicle locked again afterward. Temporary test
  budgets were restored to the exact production limits and no cooldown was
  active.
- Pixel 10 follow-up on 2026-07-01 used USB with `SLEEP_AFTER_OPERATION=false`
  and temporary test budgets. Charge and details refreshed successfully on the
  Pixel. Location was not fully verifiable because the Volkswagen app showed
  `Limited Services` / not logged into the vehicle and a rating nag screen;
  a Google Maps confirmation dialog was also observed. The connector now
  dismisses the Google Maps consent and common rating-style app notices, and
  reports the limited-services state as an app-state error instead of a generic
  missing navigation element. The runtime was restored to the production Redmi
  configuration with exact production limits, no cooldown, and Pixel stay-awake
  cleared.
- After wake, overlay, selector, or localization changes, test on a real phone
  before committing.
- Verify `/health`, the affected cached endpoint, service logs, screen-off
  cleanup, and usage counters.
- Test German and English when localized UI matching is affected.
