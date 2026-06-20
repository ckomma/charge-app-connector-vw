import importlib
import json
import logging
import os
from dataclasses import asdict, is_dataclass
from typing import Callable


LOG = logging.getLogger(__name__)


class MqttPublisher:
    """Optional MQTT state publisher with Home Assistant discovery."""

    def __init__(
        self,
        state_provider: Callable[[], dict[str, object]],
        mqtt_module: object | None = None,
    ) -> None:
        self.state_provider = state_provider
        self.host = os.environ["MQTT_HOST"]
        self.port = int(os.getenv("MQTT_PORT", "1883"))
        self.topic_prefix = os.getenv("MQTT_TOPIC_PREFIX", "vw_app_connector").strip("/")
        self.discovery_prefix = os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant").strip("/")
        self.client_id = os.getenv("MQTT_CLIENT_ID", "vw-app-connector")
        self.availability_topic = f"{self.topic_prefix}/availability"
        mqtt = mqtt_module or importlib.import_module("paho.mqtt.client")
        self.client = mqtt.Client(client_id=self.client_id)
        username = os.getenv("MQTT_USERNAME", "")
        if username:
            self.client.username_pw_set(username, os.getenv("MQTT_PASSWORD", ""))
        if os.getenv("MQTT_TLS", "false").casefold() in ("1", "true", "yes", "on"):
            self.client.tls_set()
        self.client.will_set(self.availability_topic, "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    @classmethod
    def from_environment(
        cls, state_provider: Callable[[], dict[str, object]]
    ) -> "MqttPublisher | None":
        if not os.getenv("MQTT_HOST", "").strip():
            return None
        try:
            return cls(state_provider)
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "MQTT_HOST is configured but paho-mqtt is not installed"
            ) from exc

    def start(self) -> None:
        self.client.connect_async(self.host, self.port, keepalive=60)
        self.client.loop_start()
        LOG.info("MQTT enabled for %s:%s at %s", self.host, self.port, self.topic_prefix)

    def stop(self) -> None:
        self.client.publish(self.availability_topic, "offline", qos=1, retain=True)
        self.client.disconnect()
        self.client.loop_stop()

    def publish_state(self, name: str, value: object) -> None:
        if is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)
        self.client.publish(
            f"{self.topic_prefix}/{name}",
            json.dumps(value, ensure_ascii=False),
            qos=1,
            retain=True,
        )

    def _on_connect(
        self, client: object, userdata: object, flags: object, reason_code: object,
        properties: object | None = None,
    ) -> None:
        if reason_code != 0:
            LOG.error("MQTT connection failed: %s", reason_code)
            return
        LOG.info("MQTT connected")
        self._publish_discovery()
        for name, value in self.state_provider().items():
            if value is not None:
                self.publish_state(name, value)
        self.client.publish(self.availability_topic, "online", qos=1, retain=True)

    @staticmethod
    def _on_disconnect(
        client: object, userdata: object, disconnect_flags: object,
        reason_code: object = 0, properties: object | None = None,
    ) -> None:
        if reason_code != 0:
            LOG.warning("Unexpected MQTT disconnect: %s", reason_code)

    def _publish_discovery(self) -> None:
        device = {
            "identifiers": [self.client_id],
            "name": "Volkswagen App Connector",
            "manufacturer": "Volkswagen",
            "model": "Android App Connector",
        }
        entities = [
            ("sensor", "soc", "State of charge", "charge", "soc", "%", "battery", "measurement", None),
            ("sensor", "range", "Range", "charge", "range", "km", "distance", "measurement", None),
            ("sensor", "status", "Charging status", "charge", "status", None, None, None, "mdi:ev-station"),
            ("sensor", "remaining_charge", "Remaining charge time", "charge", "remainingChargeMinutes", "min", "duration", "measurement", None),
            ("sensor", "charge_power", "Charge power", "charge", "chargePowerKw", "kW", "power", "measurement", None),
            ("sensor", "charge_rate", "Charge rate", "charge", "chargeRateKmH", "km/h", "speed", "measurement", None),
            ("sensor", "target_soc", "Target state of charge", "charge", "targetSoc", "%", "battery", "measurement", None),
            ("sensor", "charging_mode", "Charging mode", "charge", "chargingMode", None, None, None, None),
            ("binary_sensor", "climater", "Climate", "charge", "climater", None, "running", None, None),
            ("binary_sensor", "locked", "Locked", "charge", "locked", None, "lock", None, None),
            ("binary_sensor", "charge_stale", "Charge data stale", "charge", "stale", None, "problem", None, None),
            ("binary_sensor", "action_available", "Vehicle actions available", "health", "actionAvailable", None, None, None, "mdi:shield-check-outline"),
            ("binary_sensor", "automatic_window_heating", "Automatic window heating", "details", "automaticWindowHeating", None, None, None, "mdi:car-defrost-front"),
            ("binary_sensor", "climate_zone_front_left", "Front-left climate zone", "details", "climateZoneFrontLeft", None, None, None, "mdi:car-seat-heater"),
            ("binary_sensor", "climate_zone_front_right", "Front-right climate zone", "details", "climateZoneFrontRight", None, None, None, "mdi:car-seat-heater"),
            ("sensor", "target_temperature", "Target temperature", "details", "targetTemperatureC", "°C", "temperature", "measurement", None),
            ("sensor", "odometer", "Odometer", "details", "odometerKm", "km", "distance", "total_increasing", None),
            ("sensor", "service_days", "Service due", "details", "serviceDays", "d", "duration", "measurement", None),
            ("sensor", "warning_status", "Warning status", "details", "warningStatus", None, None, None, "mdi:alert-circle-outline"),
            ("sensor", "address", "Vehicle address", "location", "address", None, None, None, "mdi:map-marker"),
            ("sensor", "parked_duration", "Parked duration", "location", "parkedDuration", None, None, None, "mdi:timer-outline"),
            ("sensor", "connector_status", "Connector status", "health", "status", None, None, None, "mdi:connection"),
            ("sensor", "phone_battery", "Phone battery", "health", "phoneBatteryLevel", "%", "battery", "measurement", None),
            ("sensor", "phone_battery_temperature", "Phone battery temperature", "health", "phoneBatteryTemperatureC", "°C", "temperature", "measurement", None),
            ("sensor", "background_usage", "Background usage", "health", "usageBackgroundUsed", None, None, "total_increasing", "mdi:counter"),
            ("sensor", "action_usage", "Action usage", "health", "usageActionsUsed", None, None, "total_increasing", "mdi:counter"),
            ("sensor", "cooldown", "Rate limit cooldown", "health", "usageCooldownSeconds", "s", "duration", "measurement", None),
            ("binary_sensor", "phone_powered", "Phone powered", "health", "phonePowered", None, "power", None, None),
        ]
        for component, object_id, name, state_name, key, unit, device_class, state_class, icon in entities:
            config = {
                "name": name,
                "unique_id": f"{self.client_id}_{object_id}",
                "state_topic": f"{self.topic_prefix}/{state_name}",
                "value_template": (
                    "{{ 'ON' if value_json.%s else 'OFF' }}" % key
                    if component == "binary_sensor" else "{{ value_json.%s }}" % key
                ),
                "availability_topic": self.availability_topic,
                "device": device,
            }
            if unit:
                config["unit_of_measurement"] = unit
            if device_class:
                config["device_class"] = device_class
            if state_class:
                config["state_class"] = state_class
            if icon:
                config["icon"] = icon
            topic = f"{self.discovery_prefix}/{component}/{self.client_id}/{object_id}/config"
            self.client.publish(topic, json.dumps(config, ensure_ascii=False), qos=1, retain=True)

        tracker = {
            "name": "Vehicle location",
            "unique_id": f"{self.client_id}_location",
            "state_topic": f"{self.topic_prefix}/location",
            "value_template": "{{ 'not_home' if value_json.latitude is not none else 'unknown' }}",
            "json_attributes_topic": f"{self.topic_prefix}/location",
            "availability_topic": self.availability_topic,
            "source_type": "gps",
            "device": device,
        }
        topic = f"{self.discovery_prefix}/device_tracker/{self.client_id}/location/config"
        self.client.publish(topic, json.dumps(tracker, ensure_ascii=False), qos=1, retain=True)
