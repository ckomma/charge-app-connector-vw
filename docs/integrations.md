# Integrations

## Home Assistant

### MQTT Discovery

Install the dependencies from `requirements.txt` or the distribution package
for `paho-mqtt`, then configure `MQTT_HOST` and optional credentials. Home
Assistant creates one `Volkswagen App Connector` device with charge, range,
charging, climate, lock, vehicle-detail, health, usage and location entities.

MQTT publishes retained copies only when caches update or the client connects.
It never triggers a Volkswagen app operation and is intentionally read-only.
Vehicle actions remain authenticated REST calls.

Connector health, Volkswagen source age/staleness and background backoff are
separate entities. The lock entity follows Home Assistant convention:
`locked: true` maps to binary-sensor `OFF`, while `locked: false` maps to `ON`.
The GPS tracker lets Home Assistant derive its zone state. Do not enable
location publishing on an untrusted broker.

### REST Package

[`examples/vw_app_connector.yaml`](../examples/vw_app_connector.yaml) provides
REST sensors, a location tracker and authenticated controls. Replace
`CONNECTOR_HOST` and add the configured API key to `secrets.yaml`:

```yaml
vw_app_connector_api_key: replace-with-the-connector-api-key
```

The package assumes Home Assistant packages are enabled:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

The package exposes `binary_sensor.volkswagen_app_actions_ready` and keeps
writes unavailable when cache data is failed, stale, source-stale or too old,
the installed app version is unverified, or a Volkswagen cooldown is active.
Preserve the same guard in automations that call REST commands directly.

### Automation Policy

Use one authoritative writer for charging. For PV charging, let evcc control
the wallbox and avoid a competing Home Assistant automation that issues
opposite vehicle commands. If manual vehicle charging must take precedence,
model it explicitly with an override helper and recheck that override, current
PV conditions and `volkswagen_app_actions_ready` immediately before every
write. Prefer explicit start/stop actions over a toggle.

## Home Assistant App

The app packages and runs the same connector; it does not create Home Assistant
entities by itself. Use MQTT discovery or the REST package after installation.
See [`deploy/home-assistant/README.md`](../deploy/home-assistant/README.md).

## openHAB

openHAB 5 can consume the Home Assistant MQTT discovery messages. Install the
MQTT and Home Assistant bindings, connect to the same broker and approve the
discovered Thing. The Thing is read-only and does not cause additional app
operations.

[`examples/openhab/README.md`](../examples/openhab/README.md) includes setup and
optional Rules DSL examples for charging and climate actions. Lock/unlock is
intentionally omitted from unattended examples.

## evcc

[`examples/evcc.yaml`](../examples/evcc.yaml) provides a custom vehicle with
state of charge, connection/charging state and estimated range. Its jq
expressions reject both `stale` and `sourceStale` responses.

Replace `CONNECTOR_HOST`, merge the vehicle into `evcc.yaml` and assign it to a
loadpoint:

```yaml
loadpoints:
  - title: Garage
    charger: your_charger
    vehicle: volkswagen_app
```

The example reads cached endpoints only. Connector refresh intervals and usage
limits remain authoritative.
