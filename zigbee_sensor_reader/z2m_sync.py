"""
Zigbee2MQTT device name sync.

Subscribes to the MQTT broker on the ``zigbee2mqtt/bridge/devices`` topic,
which Zigbee2MQTT publishes on startup and whenever the device list changes.
Friendly names from z2m always take priority over the names in config.py.

Environment variables (all optional):
    Z2M_MQTT_HOST  - MQTT broker hostname (default: home-logger)
    Z2M_MQTT_PORT  - MQTT broker port     (default: 8081)
    Z2M_MQTT_USER  - MQTT username        (default: empty)
    Z2M_MQTT_PASS  - MQTT password        (default: empty)
    Z2M_TOPIC_PREFIX - z2m topic prefix   (default: zigbee2mqtt)

NOTE: Port 8081 is unusual for raw MQTT (Mosquitto default is 1883; 8081 is
often the Zigbee2MQTT web UI or Mosquitto WebSocket port).  If the connection
fails, check the port and set Z2M_MQTT_PORT accordingly.
"""

import asyncio
import json
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

Z2M_MQTT_HOST = os.environ.get("Z2M_MQTT_HOST", "home-logger")
Z2M_MQTT_PORT = int(os.environ.get("Z2M_MQTT_PORT", "8081"))
Z2M_MQTT_USER = os.environ.get("Z2M_MQTT_USER", "")
Z2M_MQTT_PASS = os.environ.get("Z2M_MQTT_PASS", "")
Z2M_TOPIC_PREFIX = os.environ.get("Z2M_TOPIC_PREFIX", "zigbee2mqtt")
# Transport: "websockets" for port 8081 (Mosquitto WS), "tcp" for port 1883 (raw MQTT)
# Defaults to "websockets" because port 1883 is not open on this installation.
Z2M_MQTT_TRANSPORT = os.environ.get(
    "Z2M_MQTT_TRANSPORT",
    "tcp" if Z2M_MQTT_PORT == 1883 else "websockets",
)


def _normalise_ieee(ieee_raw: str) -> str:
    """
    Normalise a z2m IEEE address to the colon-separated lowercase format
    used by zigpy / this codebase.

    z2m stores IEEE addresses as ``0x00124b0025e7a1c3`` (hex string).
    We convert to ``00:12:4b:00:25:e7:a1:c3``.
    """
    addr = ieee_raw.lower().strip()
    if addr.startswith("0x"):
        addr = addr[2:]
    if ":" not in addr and len(addr) == 16:
        addr = ":".join(addr[i:i+2] for i in range(0, 16, 2))
    return addr


def sync_z2m_devices(devices_payload: str, conn: sqlite3.Connection) -> int:
    """
    Parse a zigbee2mqtt/bridge/devices JSON payload and upsert friendly names.

    Returns the number of sensors updated.
    """
    try:
        devices = json.loads(devices_payload)
    except json.JSONDecodeError as exc:
        logger.warning("z2m devices payload is not valid JSON: %s", exc)
        return 0

    if not isinstance(devices, list):
        logger.warning("z2m devices payload is not a list, skipping")
        return 0

    updated = 0
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        ieee_raw = dev.get("ieee_address") or dev.get("ieeeAddr", "")
        if not ieee_raw:
            continue
        ieee = _normalise_ieee(ieee_raw)
        friendly_name = dev.get("friendly_name") or dev.get("friendlyName", "")
        if not friendly_name or friendly_name == ieee_raw:
            # z2m uses the raw IEEE as the name when no name has been set
            continue
        model = (
            dev.get("definition", {}).get("model")
            or dev.get("modelID")
            or dev.get("model_id")
        )

        try:
            from .database import upsert_sensor
            upsert_sensor(
                conn,
                ieee_address=ieee,
                friendly_name=friendly_name,
                model=model or None,
                name_source="z2m",
            )
            updated += 1
            logger.debug("z2m sync: %s → %s", ieee, friendly_name)
        except Exception as exc:
            logger.warning("z2m sync failed for %s: %s", ieee, exc)

    if updated:
        logger.info("z2m sync: updated %d device name(s)", updated)
    return updated


async def run_z2m_sync(get_conn_fn) -> None:
    """
    Long-running asyncio task: connects to the MQTT broker and subscribes to
    the z2m bridge/devices topic.  Reconnects automatically on failure.

    ``get_conn_fn`` is a zero-argument callable that returns an open
    sqlite3.Connection (the main collector's shared connection).
    """
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        logger.warning(
            "paho-mqtt is not installed — z2m name sync disabled. "
            "Install with: pip install paho-mqtt"
        )
        return

    topic = f"{Z2M_TOPIC_PREFIX}/bridge/devices"
    loop = asyncio.get_event_loop()

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            logger.info(
                "z2m MQTT connected to %s:%d, subscribing to %s",
                Z2M_MQTT_HOST, Z2M_MQTT_PORT, topic,
            )
            client.subscribe(topic, qos=0)
        else:
            logger.warning(
                "z2m MQTT connection refused (rc=%d) — "
                "check host/port (Z2M_MQTT_HOST=%s, Z2M_MQTT_PORT=%d). "
                "Note: raw MQTT default port is 1883; port 8081 is usually "
                "the z2m web UI or Mosquitto WebSocket port.",
                rc, Z2M_MQTT_HOST, Z2M_MQTT_PORT,
            )

    def on_message(client, userdata, msg):
        try:
            conn = get_conn_fn()
            sync_z2m_devices(msg.payload.decode("utf-8"), conn)
        except Exception as exc:
            logger.warning("z2m on_message error: %s", exc)

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
            logger.info(
                "z2m MQTT connecting to %s:%d (transport=%s)",
                Z2M_MQTT_HOST, Z2M_MQTT_PORT, Z2M_MQTT_TRANSPORT,
            )
            client.connect_async(Z2M_MQTT_HOST, Z2M_MQTT_PORT, keepalive=60)
            client.loop_start()
            # Keep the task alive; the paho background thread handles I/O
            while True:
                await asyncio.sleep(30)
        except Exception as exc:
            logger.warning(
                "z2m MQTT connection to %s:%d failed: %s — retrying in 60s",
                Z2M_MQTT_HOST, Z2M_MQTT_PORT, exc,
            )
        finally:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass

        await asyncio.sleep(60)
