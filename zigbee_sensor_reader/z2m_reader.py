"""
Zigbee2MQTT MQTT reader for sensor data.

Subscribes to the MQTT broker and handles two topic families:
  - zigbee2mqtt/bridge/devices  → sync friendly names + zones (from description)
  - zigbee2mqtt/<friendly_name> → temperature, humidity, battery readings

This replaces the bellows/zigpy direct-dongle approach.  Zigbee2MQTT already
owns the coordinator; this module just reads what it publishes.

Environment variables (all optional, same defaults as z2m_sync.py):
    Z2M_MQTT_HOST      default: home-logger
    Z2M_MQTT_PORT      default: 8081
    Z2M_MQTT_USER      default: (empty)
    Z2M_MQTT_PASS      default: (empty)
    Z2M_MQTT_TRANSPORT default: websockets when port != 1883, else tcp
    Z2M_TOPIC_PREFIX   default: zigbee2mqtt
"""

import asyncio
import json
import logging
import os
import sqlite3
from typing import Callable

logger = logging.getLogger(__name__)

Z2M_MQTT_HOST = os.environ.get("Z2M_MQTT_HOST", "home-logger")
Z2M_MQTT_PORT = int(os.environ.get("Z2M_MQTT_PORT", "8081"))
Z2M_MQTT_USER = os.environ.get("Z2M_MQTT_USER", "")
Z2M_MQTT_PASS = os.environ.get("Z2M_MQTT_PASS", "")
Z2M_TOPIC_PREFIX = os.environ.get("Z2M_TOPIC_PREFIX", "zigbee2mqtt")
Z2M_MQTT_TRANSPORT = os.environ.get(
    "Z2M_MQTT_TRANSPORT",
    "tcp" if Z2M_MQTT_PORT == 1883 else "websockets",
)


def _normalise_ieee(ieee_raw: str) -> str:
    """Convert 0xf4b3b1fffe60ae82 → f4:b3:b1:ff:fe:60:ae:82."""
    addr = ieee_raw.lower().strip()
    if addr.startswith("0x"):
        addr = addr[2:]
    if ":" not in addr and len(addr) == 16:
        addr = ":".join(addr[i:i + 2] for i in range(0, 16, 2))
    return addr


def _looks_like_ieee(value: str) -> bool:
    addr = value.lower().strip()
    if addr.startswith("0x"):
        addr = addr[2:]
    if ":" in addr:
        parts = addr.split(":")
        return len(parts) == 8 and all(len(p) == 2 for p in parts)
    return len(addr) == 16


class Z2MSensorReading:
    """Sensor reading parsed from a zigbee2mqtt MQTT message."""

    def __init__(
        self,
        ieee_address: str,
        friendly_name: str,
        model: str | None = None,
        temperature_c: float | None = None,
        humidity_pct: float | None = None,
        battery_pct: float | None = None,
        link_quality: int | None = None,
        battery_voltage_mv: float | None = None,
    ):
        self.ieee_address = ieee_address
        self.friendly_name = friendly_name
        self.model = model
        self.temperature_c = temperature_c
        self.humidity_pct = humidity_pct
        self.battery_pct = battery_pct
        self.link_quality = link_quality
        self.battery_voltage_mv = battery_voltage_mv
        # z2m doesn't expose device-reported daily min/max — computed from readings instead
        self.device_min_temp_c = None
        self.device_max_temp_c = None
        self.device_min_humidity_pct = None
        self.device_max_humidity_pct = None


ReadingCallback = Callable[[Z2MSensorReading], None]


class Z2MReader:
    """
    Reads sensor data and device names from Zigbee2MQTT via MQTT.

    Maintains an internal map of friendly_name → IEEE address built from
    the zigbee2mqtt/bridge/devices topic.
    """

    def __init__(
        self,
        on_reading: ReadingCallback | None = None,
        get_conn_fn: Callable[[], sqlite3.Connection] | None = None,
    ):
        self._on_reading = on_reading
        self._get_conn = get_conn_fn
        self._ieee_by_name: dict[str, str] = {}  # friendly_name → ieee
        self._model_by_ieee: dict[str, str] = {}

    def handle_message(self, topic: str, payload: str) -> None:
        """Dispatch an incoming MQTT message."""
        prefix = Z2M_TOPIC_PREFIX + "/"
        if not topic.startswith(prefix):
            return

        remainder = topic[len(prefix):]

        if remainder == "bridge/devices":
            self._handle_bridge_devices(payload)
        elif remainder == "bridge/response/devices":
            # Response to our bridge/request/devices — contains a 'data' wrapper
            try:
                resp = json.loads(payload)
                devices_payload = resp.get("data", payload) if isinstance(resp, dict) else payload
                if isinstance(devices_payload, list):
                    self._handle_bridge_devices(json.dumps(devices_payload))
                elif isinstance(devices_payload, str):
                    self._handle_bridge_devices(devices_payload)
            except Exception:
                pass
        elif remainder.startswith("bridge/"):
            pass  # ignore other bridge messages
        else:
            self._handle_sensor_message(remainder, payload)

    def _handle_bridge_devices(self, payload: str) -> None:
        """Sync friendly names, models and zones from the device list."""
        try:
            devices = json.loads(payload)
        except json.JSONDecodeError as exc:
            logger.warning("z2m bridge/devices JSON error: %s", exc)
            return
        if not isinstance(devices, list):
            return

        updated = 0
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            ieee_raw = dev.get("ieee_address") or dev.get("ieeeAddr", "")
            if not ieee_raw:
                continue
            ieee = _normalise_ieee(ieee_raw)
            name = dev.get("friendly_name") or dev.get("friendlyName", "")
            # Skip devices that still use the raw IEEE as their name
            if not name or name == ieee_raw or name == ieee:
                continue

            model = (
                dev.get("definition", {}).get("model")
                or dev.get("modelID")
                or dev.get("model_id")
            )
            zone_from_desc = dev.get("description") or None

            self._ieee_by_name[name] = ieee
            if model:
                self._model_by_ieee[ieee] = model

            logger.info(
                "z2m device: raw_ieee=%s → %s  name=%r  zone=%r",
                ieee_raw, ieee, name, zone_from_desc,
            )

            if self._get_conn:
                try:
                    from .database import upsert_sensor
                    conn = self._get_conn()
                    upsert_sensor(
                        conn,
                        ieee_address=ieee,
                        friendly_name=name,
                        model=model or None,
                        zone=zone_from_desc or None,
                        name_source="z2m",
                    )
                    updated += 1
                except Exception as exc:
                    logger.warning("z2m DB sync failed for %s: %s", ieee, exc)

        logger.info("z2m bridge/devices: synced %d device(s)", updated)

    def _handle_sensor_message(self, friendly_name: str, payload: str) -> None:
        """Parse a sensor reading from a zigbee2mqtt/<name> message."""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return

        # Must have at least temperature or humidity to be a sensor reading
        temp = data.get("temperature")
        humidity = data.get("humidity")
        if temp is None and humidity is None:
            return

        ieee = self._ieee_by_name.get(friendly_name)
        if not ieee:
            raw_ieee = (data.get("device") or {}).get("ieee_address")
            if raw_ieee:
                ieee = _normalise_ieee(str(raw_ieee))
                self._ieee_by_name[friendly_name] = ieee
            elif _looks_like_ieee(friendly_name):
                ieee = _normalise_ieee(friendly_name)
            else:
                logger.debug(
                    "z2m skipping reading for unknown device name %r until bridge/devices maps it",
                    friendly_name,
                )
                return
        model = (
            self._model_by_ieee.get(ieee)
            or (data.get("device") or {}).get("model")
        )

        battery = data.get("battery")
        lqi = data.get("linkquality") or data.get("link_quality")
        # z2m reports Sonoff voltage in mV directly
        voltage_mv = data.get("voltage")

        reading = Z2MSensorReading(
            ieee_address=ieee,
            friendly_name=friendly_name,
            model=model,
            temperature_c=float(temp) if temp is not None else None,
            humidity_pct=float(humidity) if humidity is not None else None,
            battery_pct=float(battery) if battery is not None else None,
            link_quality=int(lqi) if lqi is not None else None,
            battery_voltage_mv=float(voltage_mv) if voltage_mv is not None else None,
        )

        logger.info(
            "z2m reading [%s]: temp=%s°C  hum=%s%%  battery=%s%%",
            friendly_name,
            f"{temp:.1f}" if temp is not None else "-",
            f"{humidity:.1f}" if humidity is not None else "-",
            f"{battery:.0f}" if battery is not None else "-",
        )

        if self._on_reading:
            self._on_reading(reading)


async def run_z2m_reader(
    on_reading: ReadingCallback | None = None,
    get_conn_fn: Callable[[], sqlite3.Connection] | None = None,
) -> None:
    """
    Long-running asyncio task: connect to the MQTT broker and read all
    Zigbee2MQTT sensor messages.  Reconnects automatically on failure.
    """
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        logger.error(
            "paho-mqtt is not installed — Zigbee sensor collection disabled. "
            "Install with: pip install paho-mqtt"
        )
        return

    reader = Z2MReader(on_reading=on_reading, get_conn_fn=get_conn_fn)
    devices_topic = f"{Z2M_TOPIC_PREFIX}/bridge/devices"
    sensor_topic = f"{Z2M_TOPIC_PREFIX}/#"

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            logger.info(
                "z2m MQTT connected to %s:%d (%s) — subscribing",
                Z2M_MQTT_HOST, Z2M_MQTT_PORT, Z2M_MQTT_TRANSPORT,
            )
            client.subscribe(sensor_topic, qos=0)
            # Request z2m to re-publish the full device list so we get names/zones
            client.publish(f"{Z2M_TOPIC_PREFIX}/bridge/request/devices", "", qos=0)
        else:
            logger.warning(
                "z2m MQTT connection refused rc=%d (host=%s port=%d transport=%s)",
                rc, Z2M_MQTT_HOST, Z2M_MQTT_PORT, Z2M_MQTT_TRANSPORT,
            )

    def on_message(client, userdata, msg):
        try:
            reader.handle_message(msg.topic, msg.payload.decode("utf-8", errors="replace"))
        except Exception as exc:
            logger.warning("z2m message handler error: %s", exc)

    def on_disconnect(client, userdata, rc):
        if rc != 0:
            logger.warning("z2m MQTT disconnected unexpectedly (rc=%d)", rc)

    while True:
        client = mqtt.Client(transport=Z2M_MQTT_TRANSPORT)
        client.on_connect = on_connect
        client.on_message = on_message
        client.on_disconnect = on_disconnect

        if Z2M_MQTT_USER:
            client.username_pw_set(Z2M_MQTT_USER, Z2M_MQTT_PASS)

        try:
            client.connect_async(Z2M_MQTT_HOST, Z2M_MQTT_PORT, keepalive=60)
            client.loop_start()
            logger.info(
                "z2m MQTT connecting to %s:%d (transport=%s)",
                Z2M_MQTT_HOST, Z2M_MQTT_PORT, Z2M_MQTT_TRANSPORT,
            )
            while True:
                await asyncio.sleep(30)
        except Exception as exc:
            logger.warning(
                "z2m MQTT error (%s:%d): %s — retrying in 60s",
                Z2M_MQTT_HOST, Z2M_MQTT_PORT, exc,
            )
        finally:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass

        await asyncio.sleep(60)
