# Changelog

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
