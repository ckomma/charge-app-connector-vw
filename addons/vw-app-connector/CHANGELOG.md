# Changelog

## 0.1.10

- Reduce expected Volkswagen cooldown/stale-state log noise by logging
  `UsageLimit` refresh failures as concise warnings instead of stack traces.
- Build the add-on image from the packaged add-on files instead of downloading
  connector sources from a fixed GitHub raw URL.
