# openHAB

Tested with openHAB 5.1.4, the MQTT binding and the Home Assistant binding.

## Read-only Thing

1. Configure an online MQTT broker Thing in openHAB.
2. Enable discovery on the broker Thing.
3. Install the Home Assistant binding.
4. Configure the connector to publish to the same broker with the default
   `homeassistant` discovery prefix.
5. Approve the discovered `Volkswagen App Connector` Thing from the Inbox.

The discovered Thing contains retained charge, climate, vehicle-detail,
connector-health and location channels. MQTT reads existing connector caches
and never starts a Volkswagen app refresh.

The Home Assistant binding represents percent values as
`Number:Dimensionless`. Depending on Item metadata, the REST representation can
be a factor such as `1` for 100 percent. Configure the Item unit as `%` for a
percentage display.

## Authenticated actions

Vehicle actions remain HTTP-only. Do not place the connector API key in this
repository or directly in a rule file. On a systemd installation, create a
root-readable environment file outside the openHAB configuration repository:

```text
VW_APP_CONNECTOR_API_KEY=replace-with-the-connector-api-key
```

Reference it from an openHAB systemd service override:

```ini
[Service]
EnvironmentFile=/etc/openhab/vw-app-connector.env
```

Set the environment file to mode `0600`, restart openHAB, copy the example
Items and rule into the openHAB configuration and replace `CONNECTOR_HOST`.
The rule uses synchronous requests because the connector response contains the
verified vehicle state. Connector-side action budgets, minimum intervals,
cooldowns and app-version quarantine remain authoritative.

Lock and unlock are not included. The Volkswagen app lock gesture changed on
the live-tested phone, and unlock did not open the expected S-PIN dialog.
