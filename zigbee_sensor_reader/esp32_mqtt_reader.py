"""MQTT reader for ESP32 heating probes and a forwarded Shelly BTHome feed."""

import asyncio
import json
import logging
import math
import re
from typing import Callable

from .config import ESP32_SENSOR_NAMES, ESP32_SENSOR_ZONES, ESP32_TOPIC_PREFIX
from .shelly_ble_reader import BTHomePayloadError, decode_bthome_v2_hex
from .mqtt_config import MQTTSettings
from .z2m_reader import Z2M_MQTT_SETTINGS

logger = logging.getLogger(__name__)

ESP32_MQTT_SETTINGS = MQTTSettings.from_env(
    "ESP32", defaults=Z2M_MQTT_SETTINGS
)
ESP32_MQTT_HOST = ESP32_MQTT_SETTINGS.host
ESP32_MQTT_PORT = ESP32_MQTT_SETTINGS.port
ESP32_MQTT_USER = ESP32_MQTT_SETTINGS.username
ESP32_MQTT_PASS = ESP32_MQTT_SETTINGS.password
ESP32_MQTT_TRANSPORT = ESP32_MQTT_SETTINGS.transport

TOPIC_KEY_ALIASES = {
    "boiler1_out": "boiler1_out",
    "bolier_1_return": "boiler1_return",
    "boiler1_return": "boiler1_return",
    "boiler2_out": "boiler2_out",
    "boiler2_return": "boiler2_return",
    "shelly_raw_payload": "shelly_raw_payload",
    "outdoorh_t_json": "outdoorh_t_json",
}

DEFAULT_OUTDOOR_MAC = "94:B2:16:08:82:98"
MAC_PATTERN = re.compile(r"^[0-9A-Fa-f]{2}(?::?[0-9A-Fa-f]{2}){5}$")


class ESP32SensorReading:
    """Reading compatible with the application's shared collector handler."""

    def __init__(
        self,
        ieee_address: str,
        friendly_name: str,
        model: str,
        temperature_c: float | None = None,
        humidity_pct: float | None = None,
        battery_pct: float | None = None,
        packet_id: int | None = None,
        zone: str | None = None,
    ):
        self.ieee_address = ieee_address
        self.friendly_name = friendly_name
        self.model = model
        self.temperature_c = temperature_c
        self.humidity_pct = humidity_pct
        self.battery_pct = battery_pct
        self.packet_id = packet_id
        self.zone = zone
        self.link_quality = None
        self.battery_voltage_mv = None
        self.state = None
        self.power_w = None
        self.energy_kwh = None
        self.device_min_temp_c = None
        self.device_max_temp_c = None
        self.device_min_humidity_pct = None
        self.device_max_humidity_pct = None
        self.source = "esp32"


ReadingCallback = Callable[[ESP32SensorReading], object]


def parse_numeric_temperature(payload: str) -> float:
    """Parse a finite Celsius value from an MQTT text payload."""
    text = payload.strip()
    if not text:
        raise ValueError("temperature payload is empty")
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"temperature payload is not numeric: {text!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"temperature payload is not finite: {text!r}")
    return value


def normalise_mac(value: str) -> str:
    """Validate and normalize a Bluetooth MAC address."""
    raw = str(value).strip()
    if not MAC_PATTERN.fullmatch(raw):
        raise ValueError(f"invalid Bluetooth MAC address: {value!r}")
    compact = raw.replace(":", "").upper()
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def sensor_identity(sensor_key: str, mac: str | None = None) -> str:
    """Return a stable identity that does not change with the MQTT node name."""
    if sensor_key in {"shelly_raw_payload", "outdoorh_t_json"}:
        return f"shelly:{normalise_mac(mac or DEFAULT_OUTDOOR_MAC)}"
    return f"esp32:{sensor_key}"


class ESP32MQTTReader:
    """Dispatch only the known state topics below one ESPHome node prefix."""

    def __init__(
        self,
        on_reading: ReadingCallback | None = None,
        topic_prefix: str = ESP32_TOPIC_PREFIX,
    ):
        self._on_reading = on_reading
        self.topic_prefix = topic_prefix.strip("/") or "heating-esp"
        self._last_packet_ids: dict[str, int] = {}

    @property
    def subscription_topics(self) -> list[str]:
        root = f"{self.topic_prefix}/sensor"
        return [
            f"{self.topic_prefix}/status",
            f"{root}/boiler1_out/state",
            f"{root}/bolier_1_return/state",
            f"{root}/boiler1_return/state",
            f"{root}/boiler2_out/state",
            f"{root}/boiler2_return/state",
            f"{root}/outdoorh_t_json/state",
            f"{root}/shelly_raw_payload/state",
        ]

    def handle_message(self, topic: str, payload: str) -> None:
        """Dispatch status and known sensor state topics; ignore everything else."""
        if topic == f"{self.topic_prefix}/status":
            status = payload.strip().lower()
            if status in {"online", "offline"}:
                logger.info("ESP32 %s is %s", self.topic_prefix, status)
            else:
                logger.warning("ESP32 %s published unknown status %r", self.topic_prefix, payload)
            return

        root = f"{self.topic_prefix}/sensor/"
        suffix = "/state"
        if not topic.startswith(root) or not topic.endswith(suffix):
            return
        topic_key = topic[len(root):-len(suffix)]
        sensor_key = TOPIC_KEY_ALIASES.get(topic_key)
        if sensor_key is None:
            return

        if sensor_key == "outdoorh_t_json":
            self._handle_shelly_json_payload(topic, payload)
        elif sensor_key == "shelly_raw_payload":
            self._handle_shelly_payload(topic, payload, DEFAULT_OUTDOOR_MAC)
        else:
            self._handle_probe_payload(topic, sensor_key, payload)

    def _handle_probe_payload(self, topic: str, sensor_key: str, payload: str) -> None:
        try:
            temperature = parse_numeric_temperature(payload)
        except ValueError as exc:
            logger.warning("Ignoring ESP32 temperature on %s: %s", topic, exc)
            return

        reading = ESP32SensorReading(
            ieee_address=sensor_identity(sensor_key),
            friendly_name=ESP32_SENSOR_NAMES[sensor_key],
            model="ESP32 Dallas Temperature",
            temperature_c=temperature,
            zone=ESP32_SENSOR_ZONES.get(sensor_key),
        )
        if self._on_reading:
            self._on_reading(reading)

    def _handle_shelly_json_payload(self, topic: str, payload: str) -> None:
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            logger.warning("Ignoring ESP32 Outdoor JSON on %s: invalid JSON: %s", topic, exc)
            return
        if not isinstance(value, dict):
            logger.warning("Ignoring ESP32 Outdoor JSON on %s: object required", topic)
            return
        try:
            mac = normalise_mac(value.get("mac", ""))
        except ValueError as exc:
            logger.warning("Ignoring ESP32 Outdoor JSON on %s: %s", topic, exc)
            return
        bthome_payload = value.get("payload")
        if not isinstance(bthome_payload, str):
            logger.warning("Ignoring ESP32 Outdoor JSON on %s: string payload required", topic)
            return
        self._handle_shelly_payload(topic, bthome_payload, mac)

    def _handle_shelly_payload(self, topic: str, payload: str, mac: str) -> None:
        try:
            data = decode_bthome_v2_hex(payload)
        except BTHomePayloadError as exc:
            logger.warning("Ignoring ESP32 BTHome payload on %s: %s", topic, exc)
            return

        packet_id = data.get("packet_id")
        if packet_id is None:
            logger.warning("Ignoring ESP32 BTHome payload on %s: packet id is missing", topic)
            return
        packet_id = int(packet_id)
        identity = sensor_identity("shelly_raw_payload", mac)
        if packet_id == self._last_packet_ids.get(identity):
            logger.debug("Ignoring repeated ESP32 BTHome packet id %d for %s", packet_id, identity)
            return
        if "temperature" not in data and "humidity" not in data:
            logger.warning("Ignoring ESP32 BTHome payload on %s: no temperature or humidity", topic)
            return

        reading = ESP32SensorReading(
            ieee_address=identity,
            friendly_name=ESP32_SENSOR_NAMES["shelly_raw_payload"],
            model="Shelly Blu H&T",
            temperature_c=data.get("temperature"),
            humidity_pct=data.get("humidity"),
            battery_pct=data.get("battery"),
            packet_id=packet_id,
            zone=ESP32_SENSOR_ZONES.get("shelly_raw_payload"),
        )
        if self._on_reading:
            self._on_reading(reading)
        self._last_packet_ids[identity] = packet_id


async def run_esp32_mqtt_reader(
    on_reading: ReadingCallback | None = None,
    topic_prefix: str = ESP32_TOPIC_PREFIX,
) -> None:
    """Long-running ESP32 MQTT task with the same reconnect pattern as Z2M."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        logger.error(
            "paho-mqtt is not installed - ESP32 sensor collection disabled. "
            "Install with: pip install paho-mqtt"
        )
        return

    reader = ESP32MQTTReader(on_reading=on_reading, topic_prefix=topic_prefix)

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            logger.info(
                "ESP32 MQTT connected to %s:%d (%s) - subscribing to %s",
                ESP32_MQTT_HOST,
                ESP32_MQTT_PORT,
                ESP32_MQTT_TRANSPORT,
                reader.topic_prefix,
            )
            for topic in reader.subscription_topics:
                client.subscribe(topic, qos=0)
        else:
            logger.warning(
                "ESP32 MQTT connection refused rc=%d (host=%s port=%d transport=%s)",
                rc,
                ESP32_MQTT_HOST,
                ESP32_MQTT_PORT,
                ESP32_MQTT_TRANSPORT,
            )

    def on_message(client, userdata, msg):
        try:
            reader.handle_message(msg.topic, msg.payload.decode("utf-8", errors="replace"))
        except Exception as exc:
            logger.warning("ESP32 MQTT message handler error: %s", exc)

    def on_disconnect(client, userdata, rc):
        if rc != 0:
            logger.warning("ESP32 MQTT disconnected unexpectedly (rc=%d)", rc)

    while True:
        client = mqtt.Client(transport=ESP32_MQTT_TRANSPORT)
        client.on_connect = on_connect
        client.on_message = on_message
        client.on_disconnect = on_disconnect
        ESP32_MQTT_SETTINGS.configure_client(client)

        try:
            client.connect_async(ESP32_MQTT_HOST, ESP32_MQTT_PORT, keepalive=60)
            client.loop_start()
            logger.info(
                "ESP32 MQTT connecting to %s:%d (transport=%s)",
                ESP32_MQTT_HOST,
                ESP32_MQTT_PORT,
                ESP32_MQTT_TRANSPORT,
            )
            while True:
                await asyncio.sleep(30)
        except Exception as exc:
            logger.warning(
                "ESP32 MQTT error (%s:%d): %s - retrying in 60s",
                ESP32_MQTT_HOST,
                ESP32_MQTT_PORT,
                exc,
            )
        finally:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
        await asyncio.sleep(60)
