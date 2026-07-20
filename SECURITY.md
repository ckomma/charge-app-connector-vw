# Security

Do not commit Volkswagen credentials, S-PINs, API keys, ADB private keys,
device serial numbers, vehicle identifiers, location data, UI dumps or
screenshots.

Protect both `/action/*` and `/admin/*` with a non-empty `API_KEY`. The cooldown
probe is an administrative recovery operation even though it performs only a
read: it can deliberately bypass one active rate-limit cooldown and must not be
exposed to an untrusted network.

Keep `/etc/default/vw-app-connector` readable only by root. Use the committed
deployment file only as a template and replace all placeholder values locally.
API-visible ADB failures are redacted before they reach health or cache
responses. Treat service logs and diagnostics as sensitive anyway: they may
contain device-specific operational context needed for troubleshooting.

MQTT state can include vehicle location and lock status. Use broker ACLs so the
configured connector account can publish only below its state topic and Home
Assistant discovery topic. Protect remote broker connections with TLS. MQTT
credentials belong only in the root-readable environment file.

If sensitive data is committed, rotate the affected credential and remove it
from the complete Git history before publishing or sharing the repository.
