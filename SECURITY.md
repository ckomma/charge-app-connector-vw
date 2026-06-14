# Security

Do not commit Volkswagen credentials, S-PINs, API keys, ADB private keys,
device serial numbers, vehicle identifiers, location data, UI dumps or
screenshots.

Keep `/etc/default/vw-app-connector` readable only by root. Use the committed
deployment file only as a template and replace all placeholder values locally.

If sensitive data is committed, rotate the affected credential and remove it
from the complete Git history before publishing or sharing the repository.
