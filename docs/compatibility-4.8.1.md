# Volkswagen app 4.8.1 compatibility

Verification date: 2026-10-17. Package `com.volkswagen.weconnect`,
version name/code `4.8.1` / `408010`.

## Changes

- Vehicle-map controls can now use localized `Fahrzeugstandort` or
  `Vehicle location` accessibility descriptions. Map readiness, centering and
  re-centering accept these alongside `Car Locate Button` and `Find vehicle`.
- These control labels are excluded from the location-address fallback, so
  an absent address remains unknown rather than becoming a button label.
- Existing German/English parking labels, pending-action handling, bounded
  confirmation, API-key authentication, minimum intervals, budgets and
  cooldown behavior are preserved.
- The installed production-phone build supports Android API 29 and later.
  Other packages with the same version name/code can require API 32. Check
  the particular package's Android requirement rather than inferring it from
  the version number alone.

## Real-phone verification

Device: Redmi Note 9S, Android 12 / API 31. Languages: German `de-DE` and
English `en-AU`. The existing automatic ADB Wi-Fi fallback was used while
USB was unavailable.

| Check | German | English |
| --- | --- | --- |
| Charge / SoC / synchronization age | Passed | Passed |
| Details, climate temperature and two departure rows | Passed | Passed |
| Location address and navigation coordinates with patched controls | Passed | Passed |
| Authenticated charging-settings read | Passed | Passed |
| Software refresh / absent-menu handling | Passed; not exposed | Passed; not exposed |
| Climate start / stop and fresh readback | Passed | Passed |
| Charging stop / start and fresh readback | Passed | Passed |
| Lock / unlock and physical door confirmation | Passed | Passed |
| Exact language, configuration and vehicle-state restoration | Passed | Passed |

The complete 248-test suite, Python compilation, diff checks, Home Assistant
YAML metadata and the staged launcher's Linux shell syntax passed. Source,
Home Assistant mirror and packaged Python runtime have identical SHA-256.

Volkswagen 4.8.1 is the verified default write-action baseline in Home Assistant
App 0.1.29. Existing explicit version settings are preserved on upgrade. API-key
rejection was also verified in both languages. Tests restored active charging,
climate off, the original unlocked state and German. Production usage limits
were restored without resetting persisted counters; test usage can exceed the
normal daily action limit until the next local-day rollover.

Positive battery-health and vehicle-software values were not available on the
test account. Departure-editor, charge-target, charging-mode and other optional
setting writes were not repeated. Their existing availability checks,
confirmation and unknown-value handling remain unchanged.
