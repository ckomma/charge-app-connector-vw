# Volkswagen App Connector

Run the Volkswagen App Connector as a Home Assistant add-on.

The add-on uses ADB to read the Volkswagen Android app on a connected phone.
It exposes the connector REST API on port `9920` and can publish read-only
MQTT discovery/state messages to Home Assistant.

## Quick Start

1. Add this repository as a Home Assistant add-on repository.
2. Install `Volkswagen App Connector`.
3. Configure `adb_serial`, `adb_mode`, `api_key`, and optionally MQTT.
4. Start the add-on.
5. Check `/health` and `/charge`.

USB ADB works when Home Assistant OS can see the phone. If USB ADB is not
visible inside the add-on, keep the phone powered over USB and use ADB over
Wi-Fi for the transport.

The add-on stores usage counters, caches, diagnostics, and generated ADB keys
below Home Assistant Supervisor-managed `/data`.

Do not expose port `9920` to untrusted networks. Location data can include
address and coordinates.
