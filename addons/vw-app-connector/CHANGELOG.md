# Changelog

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
