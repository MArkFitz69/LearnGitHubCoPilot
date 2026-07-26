"""
Zigbee2MQTT MQTT listener for Sonoff sensor telemetry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import (
    Z2M_MQTT_CLIENT_ID,
    Z2M_MQTT_HOST,
    Z2M_MQTT_PASSWORD,
    Z2M_MQTT_PORT,
    Z2M_MQTT_TOPIC_PREFIX,
    Z2M_MQTT_USERNAME,
)

logger = logging.getLogger(__name__)

IEEE_RE_COLON = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){7}$", re.IGNORECASE)
IEEE_RE_HEX = re.compile(r"^[0-9a-f]{16}$", re.IGNORECASE)


@dataclass
class MqttSensorReading:
    ieee_address: str
    friendly_name: str
    model: str | None = None
    temperature_c: float | None = None
    humidity_pct: float | None = None
    battery_pct: float | None = None
    link_quality: int | None = None
    device_min_temp_c: float | None = None
    device_max_temp_c: float | None = None
    device_min_humidity_pct: float | None = None
    device_max_humidity_pct: float | None = None
    battery_voltage_mv: float | None = None


class Zigbee2MqttListener:
    def __init__(self, topic_prefix: str | None = None):
        self.topic_prefix = (topic_prefix or Z2M_MQTT_TOPIC_PREFIX).strip("/")
        self._event_queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._lock = threading.Lock()
        self._latest_by_ieee: dict[str, MqttSensorReading] = {}
        self._device_by_friendly: dict[str, dict[str, str | None]] = {}
        self._requested_snapshots: set[str] = set()
        self._client = None
        self._connected = False
        self._last_message_at: str | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_message_at(self) -> str | None:
        return self._last_message_at

    def _coerce_float(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _coerce_int(self, value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _normalize_ieee(self, value: str | None) -> str | None:
        if not value:
            return None
        raw = value.strip().lower()
        if raw.startswith("0x"):
            raw = raw[2:]
        raw = raw.replace("-", "").replace(":", "")
        if IEEE_RE_HEX.match(raw):
            return ":".join(raw[i : i + 2] for i in range(0, 16, 2))
        if IEEE_RE_COLON.match(value.strip().lower()):
            return value.strip().lower()
        return None

    def _is_generated_friendly_name(self, friendly_name: str, ieee_address: str) -> bool:
        normalized_friendly = self._normalize_ieee(friendly_name)
        return normalized_friendly is not None and normalized_friendly == ieee_address

    def _extract_ieee(self, payload: dict[str, Any], friendly_name: str) -> str:
        candidates = [
            payload.get("ieee_address"),
            payload.get("ieee"),
            payload.get("device_ieee"),
            (payload.get("device") or {}).get("ieee_address") if isinstance(payload.get("device"), dict) else None,
            self._device_by_friendly.get(friendly_name, {}).get("ieee_address"),
        ]
        for candidate in candidates:
            ieee = self._normalize_ieee(candidate if isinstance(candidate, str) else None)
            if ieee:
                return ieee
        fallback_ieee = self._normalize_ieee(friendly_name)
        if fallback_ieee:
            return fallback_ieee
        return friendly_name

    def _extract_model(self, payload: dict[str, Any], friendly_name: str) -> str | None:
        model = payload.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
        device = payload.get("device")
        if isinstance(device, dict):
            definition = device.get("definition")
            if isinstance(definition, dict):
                model = definition.get("model")
                if isinstance(model, str) and model.strip():
                    return model.strip()
        mapped = self._device_by_friendly.get(friendly_name, {})
        return mapped.get("model")

    def _record_device_mapping(self, devices: list[dict[str, Any]]) -> None:
        mapped: dict[str, dict[str, str | None]] = {}
        for device in devices:
            if not isinstance(device, dict):
                continue
            friendly = device.get("friendly_name")
            ieee = self._normalize_ieee(device.get("ieee_address"))
            if not isinstance(friendly, str) or not friendly:
                continue
            model = None
            definition = device.get("definition")
            if isinstance(definition, dict):
                raw_model = definition.get("model")
                if isinstance(raw_model, str) and raw_model.strip():
                    model = raw_model.strip()
            mapped[friendly] = {"ieee_address": ieee, "model": model}
            display_name = friendly
            if ieee and self._is_generated_friendly_name(friendly, ieee):
                display_name = None
            self._event_queue.put(
                (
                    "device",
                    {
                        "ieee_address": ieee or friendly,
                        "friendly_name": display_name,
                        "model": model,
                    },
                )
            )
            # Proactively request current state so dashboard/status are refreshed
            # even if retained device telemetry is disabled in Zigbee2MQTT.
            if model and model.startswith("SNZB-02") and display_name and display_name not in self._requested_snapshots:
                self._requested_snapshots.add(display_name)
                if self._client:
                    self._client.publish(
                        f"{self.topic_prefix}/{display_name}/get",
                        payload=json.dumps(
                            {
                                "temperature": "",
                                "humidity": "",
                                "battery": "",
                                "voltage": "",
                                "linkquality": "",
                            }
                        ),
                        qos=0,
                        retain=False,
                    )
                    logger.info("Requested initial Zigbee2MQTT snapshot for %s", display_name)
        self._device_by_friendly.update(mapped)

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties=None) -> None:
        rc_ok = False
        try:
            rc_ok = int(reason_code) == 0
        except Exception:
            if hasattr(reason_code, "is_failure"):
                rc_ok = not bool(reason_code.is_failure)
        self._connected = rc_ok
        if not self._connected:
            logger.error("MQTT connect failed: rc=%s", reason_code)
            return
        logger.info("Connected to Zigbee2MQTT broker at %s:%s", Z2M_MQTT_HOST, Z2M_MQTT_PORT)
        client.subscribe(f"{self.topic_prefix}/bridge/devices", qos=0)
        client.subscribe(f"{self.topic_prefix}/+", qos=0)
        client.publish(
            f"{self.topic_prefix}/bridge/request/devices",
            payload="{}",
            qos=0,
            retain=False,
        )

    def _on_disconnect(self, _client, _userdata, reason_code, _properties=None) -> None:
        self._connected = False
        logger.warning("Disconnected from MQTT broker: rc=%s", reason_code)

    def _on_message(self, _client, _userdata, message) -> None:
        self._last_message_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        topic = message.topic
        if not topic.startswith(f"{self.topic_prefix}/"):
            return

        suffix = topic[len(self.topic_prefix) + 1:]
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except Exception:
            return

        if suffix == "bridge/devices" and isinstance(payload, list):
            self._record_device_mapping(payload)
            return
        if suffix.startswith("bridge/") or not isinstance(payload, dict):
            return

        friendly_name = suffix
        ieee_address = self._extract_ieee(payload, friendly_name)
        display_name = friendly_name
        if self._is_generated_friendly_name(friendly_name, ieee_address):
            display_name = ieee_address
        model = self._extract_model(payload, friendly_name)

        link_quality = self._coerce_int(payload.get("linkquality"))
        reading = MqttSensorReading(
            ieee_address=ieee_address,
            friendly_name=display_name,
            model=model,
            temperature_c=self._coerce_float(payload.get("temperature")),
            humidity_pct=self._coerce_float(payload.get("humidity")),
            battery_pct=self._coerce_float(payload.get("battery")),
            link_quality=link_quality,
            device_min_temp_c=self._coerce_float(payload.get("temperature_min")),
            device_max_temp_c=self._coerce_float(payload.get("temperature_max")),
            device_min_humidity_pct=self._coerce_float(payload.get("humidity_min")),
            device_max_humidity_pct=self._coerce_float(payload.get("humidity_max")),
            battery_voltage_mv=self._coerce_float(payload.get("voltage")),
        )
        if reading.battery_voltage_mv is not None and reading.battery_voltage_mv < 100:
            reading.battery_voltage_mv = reading.battery_voltage_mv * 1000.0

        with self._lock:
            previous = self._latest_by_ieee.get(ieee_address)
            if previous:
                if reading.temperature_c is None:
                    reading.temperature_c = previous.temperature_c
                if reading.humidity_pct is None:
                    reading.humidity_pct = previous.humidity_pct
                if reading.battery_pct is None:
                    reading.battery_pct = previous.battery_pct
                if reading.link_quality is None:
                    reading.link_quality = previous.link_quality
                if reading.device_min_temp_c is None:
                    reading.device_min_temp_c = previous.device_min_temp_c
                if reading.device_max_temp_c is None:
                    reading.device_max_temp_c = previous.device_max_temp_c
                if reading.device_min_humidity_pct is None:
                    reading.device_min_humidity_pct = previous.device_min_humidity_pct
                if reading.device_max_humidity_pct is None:
                    reading.device_max_humidity_pct = previous.device_max_humidity_pct
                if reading.battery_voltage_mv is None:
                    reading.battery_voltage_mv = previous.battery_voltage_mv
                if not reading.model:
                    reading.model = previous.model
            self._latest_by_ieee[ieee_address] = reading

        self._event_queue.put(
            (
                "frame",
                {
                    "ieee_address": ieee_address,
                    "friendly_name": display_name,
                    "endpoint_id": None,
                    "cluster_id": None,
                    "attribute_id": None,
                    "value_text": json.dumps(payload, separators=(",", ":"), default=str)[:1000],
                    "aps_timestamp": payload.get("last_seen") or payload.get("timestamp"),
                    "zigbee_sequence": None,
                    "lqi": link_quality,
                    "rssi": self._coerce_int(payload.get("rssi")),
                    "source": "mqtt_message",
                },
            )
        )
        if reading.temperature_c is not None or reading.humidity_pct is not None:
            self._event_queue.put(("reading", {"reading": reading}))

    async def start(self) -> None:
        import paho.mqtt.client as mqtt

        self._client = mqtt.Client(client_id=Z2M_MQTT_CLIENT_ID, protocol=mqtt.MQTTv5)
        if Z2M_MQTT_USERNAME:
            self._client.username_pw_set(Z2M_MQTT_USERNAME, Z2M_MQTT_PASSWORD or "")
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.connect(Z2M_MQTT_HOST, Z2M_MQTT_PORT, keepalive=60)
        self._client.loop_start()

        deadline = time.monotonic() + 10
        while not self._connected and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if not self._connected:
            raise RuntimeError(
                f"Could not connect to MQTT broker at {Z2M_MQTT_HOST}:{Z2M_MQTT_PORT}"
            )

    async def stop(self) -> None:
        if not self._client:
            return
        self._client.loop_stop()
        self._client.disconnect()
        self._client = None
        self._connected = False

    async def permit_join(self, duration: int = 120) -> None:
        if not self._client:
            raise RuntimeError("MQTT client not started")
        topic = f"{self.topic_prefix}/bridge/request/permit_join"
        payload = json.dumps({"value": True, "time": int(duration)})
        info = self._client.publish(topic, payload=payload, qos=0, retain=False)
        info.wait_for_publish(timeout=3)
        logger.info("Requested Zigbee2MQTT permit_join for %ds", duration)

    def read_cached_sensors(self) -> list[MqttSensorReading]:
        with self._lock:
            return list(self._latest_by_ieee.values())

    def drain_events(self) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        while True:
            try:
                events.append(self._event_queue.get_nowait())
            except queue.Empty:
                break
        return events
