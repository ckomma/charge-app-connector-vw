# Volkswagen App Connector

This repository contains an ADB-based connector for reading and controlling the
Volkswagen Android app. Treat the Git repository as the portable source of
truth across Windows, Linux, Termux/mobile, and Codex Cloud.

## Working Rules

- Read `README.md` and `SECURITY.md` before changing runtime behavior.
- Use the repo-scoped `$vw-app-connector-manager` skill for implementation,
  deployment, diagnostics, and release work.
- Keep German and English Volkswagen app localizations supported.
- Preserve existing API response fields and evcc/Home Assistant compatibility
  unless the requested change explicitly requires a breaking change.
- Keep UI selectors semantic and case-insensitive. Prefer localized
  accessibility descriptions over fixed coordinates.
- Use fixed coordinates only where the app exposes no stable semantic element,
  and document the reason in code.
- Preserve background/action budgets, cooldowns, and action priority unless a
  change is explicitly intended to alter App usage.

## Verification

For Python changes, run:

```bash
python -m unittest discover -s tests -v
python -m py_compile vw_app_connector.py
git diff --check
```

For UI parser or selector changes:

1. Add or update tests using sanitized UI strings.
2. Test both German and English when localization behavior is affected.
3. Test on a real phone before committing when vehicle actions or selectors
   change.
4. Restore the vehicle and charging setup to their previous state after tests.
5. Check `/health` and the affected endpoint after deployment.

## Runtime Safety

- Start with read-only checks: service state, `/health`, cached endpoints, logs,
  ADB device state, and saved diagnostics.
- Do not exceed configured background or action limits.
- Do not bypass the configured API key for action endpoints.
- Do not run lock, climate, or charging actions unless the user requested the
  behavior or approved live action testing.
- Prefer USB in `ADB_MODE=auto`; Wi-Fi is the fallback.
- Do not weaken secret-file permissions or expose the connector directly to an
  untrusted network.

## Portability And Secrets

- Locate this repository by folder name under the current device's Codex
  projects root. Do not assume a specific Windows user folder.
- Do not commit `.env` files, `/etc/default` contents, API keys, Volkswagen
  credentials, S-PINs, ADB private keys, device serials, VINs, addresses,
  coordinates, UI dumps, screenshots, or usage-state files.
- Use placeholders in examples and environment-variable names in
  documentation.
- Live deployment hosts, SSH aliases, container identifiers, and local network
  addresses are environment-specific. Discover them from the current device or
  ask when they cannot be determined safely.
- Do not copy Claude/Codex transcripts, permission logs, or credential bundles
  into this repository.

