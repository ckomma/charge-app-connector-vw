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

## Verification

- After wake, overlay, selector, or localization changes, test on a real phone
  before committing.
- Verify `/health`, the affected cached endpoint, service logs, screen-off
  cleanup, and usage counters.
- Test German and English when localized UI matching is affected.
