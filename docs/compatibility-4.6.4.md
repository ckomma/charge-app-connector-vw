# Volkswagen app 4.6.4 compatibility

Audit date: 2026-10-10. Package: `com.volkswagen.weconnect`.

## Version evidence and safety

The directly fetched Google Play page identifies version `4.6.4`; its public
listing reports an update on 2026-10-08. The installed package independently
reports version name/code `4.6.4` / `406040`. Search-index and third-party listing dates
can lag or disagree and must not be used alone to decide that no update exists.

The prepared write-action baseline is now `4.6.4`. German/English real-phone
reads, climate, charging and lock/unlock verification passed. Physical feedback
exposed a lock-completion defect during testing: “Wird entriegelt” / “Is being
unlocked” had been interpreted as a completed unlock. The corrected parser
returns unknown for pending states, and actions keep the app alive until bounded
completion feedback and a matching fresh read. The user physically confirmed
corrected unlocks in German and English, opening and closing the door. Existing
explicit version settings are preserved. Production code and configuration
have not been promoted or deployed.

## Verification results

| Check | Result |
| --- | --- |
| Real-phone German charge/SoC read | Passed; initial disconnected `A`, later connected `B` and charging `C` |
| Real-phone German details read | Passed; odometer, service, climate settings and two departure rows |
| Real-phone German location read | Initially service-limited; after profile activation and parking-label fix, address and coordinates present |
| German/English recovery-screen regressions | Passed; the actual 30-day recovery state was not forced live |
| Battery-health / software fields and API | Implemented; German/English parser regressions passed |
| Real-phone optional feature reads | Passed absent-feature paths; no health card or software menu exposed |
| Software refresh authentication | Missing key rejected with HTTP 401; authenticated read/cache returned HTTP 200 |
| Full Python regression suite | 244 tests passed |
| Compilation, diff and local documentation links | Passed |
| Source, Home Assistant mirror and built package | Identical runtime SHA-256 |
| German lock/unlock | Corrected completion verified; user physically confirmed unlocking |
| German climate start/stop | Fresh overview confirmed `climater: true` then `false` |
| Climate confirmation errors | Unconfirmed commands now return HTTP 503 instead of misleading success |
| Location parking label | Added new German `Parkt seit`, preserving existing German/English labels |
| German charging stop/start | Fresh UI confirmed `B` then `C`; wallbox measured about 10.7 kW |
| Real-phone English navigation | Charge, climate, charging, software absent-menu and location passed; corrected lock/unlock passed (`en-AU`); unlock physically confirmed |

## Initial read-only stages

The candidate was tested from an isolated runtime directory. The production
service was restored with its exact original environment and limits of 180
background operations / 20 actions. Background usage increased from 127 to 132
by the expected weighted read cost; action usage stayed at zero. No cooldown or
shared backoff remained. After restoration the service was active/running and
all four cached API endpoints returned HTTP 200. Existing old charge/location
values can still be stale; HTTP 200 does not establish fresh vehicle data.

Production still reports `UNVERIFIED_APP_VERSION` for 4.6.4 as intended. The
implementation and Home Assistant package are prepared locally; this audit does
not constitute a deployed release or a write-action compatibility approval.

The follow-up feature implementation was tested twice through an isolated,
localhost-only API on the real German-language phone. Software refreshes were
authenticated and read-only; both returned the absent-menu observation. Details
continued returning odometer and two departure rows, with unknown battery
health because no card was exposed. Across these feature tests, background
usage increased 135 -> 141 and action-budget usage 0 -> 2; the two action units
were software **reads**, and no vehicle writes occurred. Both production limits
were restored byte-for-byte after each test. The final check confirmed limits
180/20, zero cooldown/shared backoff, active service, root-only environment,
removed restore file and HTTP 200 for health/charge/details/location. The new
features remain prepared locally rather than installed in production.

## Compatibility changes

- The German parking-position card now uses `Parkt seit`. Vehicle-marker
  selection and address/parking-duration parsing accept this wording in merged
  and separate accessibility nodes, preserving English and older German labels.
- Climate actions now wait for command feedback and verify the observed state.
  A missing confirmation returns `CLIMATE_ACTION_UNCONFIRMED` (HTTP 503).
  Neither a successful tap nor a fixed delay is treated as successful climate
  control. Repeated reads are bounded and do not repeat the command.
- The data-saving recovery screen has new localized text for loading vehicle
  data after at least 30 days of app inactivity. The connector now recognizes
  this state as `APP_DATA_UPDATING` and applies its existing shared backoff.
  Regression tests cover both locales, settings false positives, preserved
  cached SoC, persisted backoff and absence of immediate relaunches.
- Optional vehicle-software information includes installed version, release
  notes availability and update status. Implemented as cached
  `GET /software` with authenticated read-only `POST /action/software/refresh`.
  It uses the existing action budget, minimum intervals and cooldown, without
  adding background polling. The settings navigation does not require charging
  controls. Missing menu entries return `available: false`; version and update
  availability remain null. No installation or release-notes-opening action is
  added. Availability depends on the vehicle and account.
- Details now expose `batteryHealthPercent` and the localized display date
  `batteryHealthCalculatedOn` from a labelled report card, including bounded
  scrolling. The existing weighted detail budget remains three. Missing cards
  remain unknown; health is never inferred from charge SoC or explanatory text.
  Home Assistant MQTT discovery exposes both fields and software information,
  with unavailable handling for missing/stale values and expiry for software.
- Digital-key support already existed in the previous baseline. Its presence
  does not make it an equivalent replacement for existing remote lock actions.

## Follow-up priorities

1. Deploy the prepared changes only when requested, verify health and affected
   endpoints, and update any explicitly configured old write baseline as part
   of that deployment. Departure-editor, charge-target and charging-mode writes
   were not repeated in this audit; their previous safeguards remain.
2. Obtain an actual battery-health card and software-update page on an eligible
   account/vehicle to validate positive real-device values. Optional fields and
   absent-feature handling are implemented and tested; this vehicle/account
   exposed neither feature. Positive German/English parser fixtures are based
   on sanitized localized examples, not an observed live percentage or software
   version.
3. Obtain an actual empty-PHEV observation for issue #19 and transport/UI
   evidence during a recurrence of issue #20. Neither issue can be closed on
   the strength of this compatibility check.

## Existing issues

- [Issue #19](https://github.com/ckomma/charge-app-connector-vw/issues/19): the
  numeric overview SoC fallback already handles an empty PHEV detail arc (`--`),
  including `0%`, while preserving numeric detail values as authoritative.
  German/English regressions pass; an actual empty-PHEV verification remains
  outstanding. That vehicle-specific display still requires live verification.
- [Issue #20](https://github.com/ckomma/charge-app-connector-vw/issues/20): its
  latest discussion distinguishes demonstrated ADB transport loss from a
  separate overview failure. The new data-saving screen is an additional
  recognizable UI state, not a proven explanation of those historical logs.
  Existing bounded ADB recovery, source-stale protection and the one-time
  charging-to-idle follow-up remain in place.
- [Issue #16](https://github.com/ckomma/charge-app-connector-vw/issues/16): data
  saving after inactivity is distinct from intelligent vehicle power saving and
  the explicit server request limit. UI wording alone does not establish
  a 12-volt battery fault or a causal explanation for backend outages.
- [Issue #31](https://github.com/ckomma/charge-app-connector-vw/issues/31): the
  linked Skoda API is architectural reference material. It does not establish a
  supported public Volkswagen API or authorize replacing this UI connector
  with private Volkswagen backend requests.

No issue has been closed or commented on as part of this audit.

## Vehicle-action verification and lock correction

German and English (`en-AU`) tests confirmed climate start/stop and charging
stop/start. Charging state `C` was corroborated by approximately 10.7 kW of
wallbox power after temporarily selecting immediate charging. The original
Smart mode was restored. Location reads returned address and coordinates in
both languages; their values are deliberately omitted. Optional software and
battery-health features remained absent with the user profile active.

Earlier lock/unlock API responses were invalidated by physical vehicle
feedback: the user could not open the door although the API reported unlocked.
The previous parser classified pending labels (“Wird entriegelt”, “Is being
unlocked”) as completed states, and the fixed-delay verification restarted the
app before its asynchronous command completed. Those observations do not
establish successful actions or automatic re-locking.

Pending labels now yield null. Lock actions keep the app alive for bounded
completion feedback, surface Volkswagen's unstable-connection failure, and
require a matching final read. Idempotent actions also verify their readback.
The corrected German unlock was physically confirmed by the user. The user
subsequently confirmed actual automatic re-locking before a door was opened.
The corrected English unlock/lock/unlock cycle passed with fresh readbacks.
The user physically confirmed both English unlocks, opening and closing the
door. The prepared default is now 4.6.4; production code and configuration
have not been promoted or deployed.

The full suite passes 244 regressions. All live tests retain persisted usage
counters, cooldowns and minimum intervals. Exact production environment and
language restoration are checked after each isolated run. The daily action
counter exceeds its ordinary limit as a result of explicitly authorized tests;
it is never reset. No GitHub issue has been closed or commented on.


## Final restoration

A delayed fresh read confirmed the requested unlocked final state after the
user opened and closed the door. Climate was off, charging stopped (`B`), and
the connected wallbox remained in its original Smart mode at about 1 W idle.
Phone language was restored exactly to `de-DE`. The production environment was
restored byte-for-byte with mode 0600, the temporary restore file removed and
the service active. Health and cached charge/details/location returned HTTP
200. Limits were restored to 180 background units and 20 actions, with counters
167/49 retained and no cooldown or shared backoff. Production still requires
4.3.2 and blocks writes from installed 4.6.4 until an authorized deployment.
The test runtime explicitly verified the installed 4.6.4 version.


## Targeted Pixel 10 verification

The same prepared runtime hashes were tested on a USB-connected Pixel 10
(Android SDK 37), with Volkswagen 4.6.4 and German (`de-DE`) UI. Overview/charge
reads, vehicle details (odometer and two departure rows), and location (address
and coordinates present) passed. Battery health remained absent. Climate
start/stop and lock/unlock returned matching fresh UI readbacks. The user
physically confirmed the final unlock by opening and closing the door.
Charging controls and English were not repeated in this targeted Pixel test;
the earlier German/English Redmi verification remains the evidence for those.

The first detail attempt was interrupted before vehicle writes because the
Pixel's secure screen lock became active after the connector put the display
to sleep. Exact production configuration was restored. After manual unlocking,
the isolated repeat used the existing `SLEEP_AFTER_OPERATION=false` option and
completed the listed checks. This is a device-lock prerequisite, not evidence
of a new Volkswagen selector defect. Production settings were not changed.


The delayed Pixel read confirmed unlocked, climate off and connected-idle `B`,
with the wallbox in its original Smart mode (connected, not charging, about
1 W). Exact environment restoration succeeded, mode 0600, restore file removed
and production service active. Health and cached charge/details/location
returned HTTP 200; production limits 180/20, retained counters 169/63, cooldown
and shared backoff zero. The Pixel language remained de-DE. Production code
and write baseline remain unchanged. The Redmi was reconnected and its authorized USB transport verified. The
production service and existing endpoints remained healthy after the swap.
