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
- A failed background refresh waits
  `BACKGROUND_ERROR_RETRY_SECONDS`, default 900 seconds, before another
  attempt. This prevents a persistent UI obstruction from consuming the daily
  background budget every five minutes.
- USB remains the preferred transport in `ADB_MODE=auto`; configured ADB Wi-Fi
  is only the fallback while USB is unavailable.
- Location marker details can expose the address as a separate TextView instead
  of a single combined string containing `Geparkt seit` / `Parked since`.
  Verified on 2026-06-17 with the production Redmi connected to the evcc LXC:
  a direct live location read returned both an address and navigation
  coordinates after using the visible map viewport for the marker tap and a
  position-based address fallback. The current view did not expose a parked
  duration label, so `parkedDuration` can remain empty even when address and
  coordinates are functional.

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
- During the verification, the running connector still reported `ADB_MODE=auto`
  with Wi-Fi transport because the old Redmi Wi-Fi device was still available.
  To make the Pixel the production device, update the runtime `ADB_SERIAL` to
  the Pixel USB serial on the evcc LXC and reload/restart the connector, then
  verify `/health` reports USB transport.
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

## Verification

- After wake, overlay, selector, or localization changes, test on a real phone
  before committing.
- Verify `/health`, the affected cached endpoint, service logs, screen-off
  cleanup, and usage counters.
- Test German and English when localized UI matching is affected.
