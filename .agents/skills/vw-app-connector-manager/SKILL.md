---
name: vw-app-connector-manager
description: Develop, deploy, test, and diagnose the Volkswagen Android App Connector, including ADB USB/Wi-Fi transport, German/English UI parsing, usage limits, systemd operation, evcc, and Home Assistant integration.
---

# Volkswagen App Connector Manager

## Scope

Use this skill for work in the `charge-app-connector-vw-codex` repository:

- Python connector implementation and tests
- Volkswagen app UI parsing and selectors
- German and English localization
- ADB over USB and optional Wi-Fi fallback
- background/action usage budgets and cooldown handling
- systemd deployment and logs
- evcc and Home Assistant example integrations
- live phone verification

Read the repository `AGENTS.md`, `README.md`, `SECURITY.md`, and `MEMORY.md`
before making changes.

## Diagnose First

Prefer read-only checks:

```bash
git status --short
python -m unittest discover -s tests -v
python -m py_compile vw_app_connector.py
```

On a configured runtime host, inspect:

```bash
systemctl status vw-app-connector
curl -sS http://127.0.0.1:9920/health
curl -sS http://127.0.0.1:9920/charge
journalctl -u vw-app-connector --since "30 minutes ago" --no-pager
adb devices -l
```

Runtime access is environment-specific. Discover the SSH target, remote path,
service name, and container/VM boundary from local configuration or current
project context. Do not add personal infrastructure values to the repository.

## Implementation Rules

- Reuse `VolkswagenReader` helpers and `BackgroundCache` patterns.
- Keep selectors semantic and localized through alias tuples.
- Add sanitized parser tests for every new UI wording variant.
- Keep endpoint schemas backward compatible.
- Maintain USB preference in `ADB_MODE=auto`.
- Preserve action priority over background work.
- Treat one multi-page detail read as its configured budget cost; do not remove
  daily limits, minimum intervals, or rate-limit cooldowns.
- Store diagnostics outside the repository.

## Xiaomi And MIUI

- Treat pocket-mode overlays and empty accessibility hierarchies as recoverable
  phone UI states when the Volkswagen app is foreground.
- Preserve the existing one-shot volume-up recovery and retry behavior.
- Foreground detection must consider MIUI's `mObscuringWindow` in addition to
  the standard focused-window fields.
- Preserve `KEYCODE_POWER` as a wake fallback and keep the screen awake during
  multi-step reads.
- Recommend display-up placement with the proximity sensor uncovered.
- Do not generalize Xiaomi-specific behavior further without testing another
  manufacturer; manufacturer-gate the fallback if false positives are found.

## Live Verification

For every explicitly requested live phone or runtime test, daily budgets must
not constrain execution. Save `ACTION_DAILY_LIMIT` and
`BACKGROUND_DAILY_LIMIT` root-only, raise both temporarily to a practically
unlimited test value, and restart only the connector service. Never reset the
persisted usage counters. Keep minimum intervals, rate-limit cooldowns,
API-key authentication and app-version quarantine active. After the test and
vehicle-state restoration, restore both exact production limits, restart the
connector, verify `/health`, counters and cooldown, and remove the temporary
restore file.

For read changes:

1. Deploy the exact locally tested file.
2. Validate service startup and `/health`.
3. Wait for or trigger the narrowest relevant refresh.
4. Check the affected endpoint and logs.
5. Confirm usage counters remain below their limits.

For action changes:

1. Record current lock, climate, charging, and evcc states.
2. Confirm the temporary test-budget override is active and no cooldown exists.
3. Exercise the requested state transition.
4. Verify the state through a fresh UI read.
5. Restore the original state.
6. Test German and English when selectors changed.

Do not commit or push selector/action changes until the requested live-phone
verification is complete.

## Secret Hygiene

Never print or commit:

- API keys or environment-file contents
- Volkswagen account credentials or S-PINs
- ADB private keys or real device serials
- VINs, addresses, coordinates, screenshots, or raw UI dumps

When reporting location tests, report only whether address and coordinates were
present.
