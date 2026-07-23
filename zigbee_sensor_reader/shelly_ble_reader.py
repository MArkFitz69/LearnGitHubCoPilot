"""
Shelly Blu H&T sensor integration via BLE (Bluetooth Low Energy).

Scans for Shelly Blu H&T BTHome v2 advertisements and extracts
temperature, humidity, and battery data.

Requires:
    - Raspberry Pi with Bluetooth (Pi 3B+ or newer)
    - bleak Python package (pip install bleak)

The Shelly Blu H&T broadcasts data using the BTHome v2 protocol
(service UUID 0xFCD2). No pairing or gateway required — the Pi
listens passively to BLE advertisements.
"""

import asyncio
import logging
import struct
from datetime import datetime, timedelta

from .config import SHELLY_SENSORS, ZONES
from .database import get_connection, upsert_sensor, insert_reading

logger = logging.getLogger(__name__)

# BTHome v2 service UUID used by Shelly Blu devices
BTHOME_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"

# BTHome v2 object IDs and their (length, scale, signed) properties
BTHOME_OBJECTS = {
    0x00: ("packet_id", 1, 1, False),      # uint8, rolling packet counter
    0x01: ("battery", 1, 1, False),        # uint8, 1%
    0x02: ("temperature", 2, 0.01, True),  # sint16, 0.01°C
    0x03: ("humidity", 2, 0.01, False),    # uint16, 0.01%
    0x09: ("battery", 1, 1, False),        # uint8, 1% (alt ID)
    0x0A: ("voltage", 2, 0.001, False),    # uint16, 0.001V
    0x2E: ("humidity", 1, 1, False),       # uint8, 1% (short form)
    0x45: ("temperature", 2, 0.1, True),   # sint16, 0.1°C (alt)
}


def parse_bthome_payload(data: bytes) -> dict:
    """
    Parse BTHome v2 service data payload.

    The first byte is the device info byte (encryption flag, version).
    Remaining bytes are object_id + value pairs.
    """
    if len(data) < 2:
        return {}

    # First byte: device info (bit 0 = encryption, bits 5-7 = version)
    device_info = data[0]
    encrypted = bool(device_info & 0x01)
    if encrypted:
        logger.debug("Encrypted BTHome payload, skipping")
        return {}

    results = {}
    i = 1  # skip device info byte

    while i < len(data):
        obj_id = data[i]
        i += 1

        if obj_id not in BTHOME_OBJECTS:
            # Unknown object — try to skip (can't know length, so stop)
            break

        name, length, scale, signed = BTHOME_OBJECTS[obj_id]

        if i + length > len(data):
            break

        if length == 1:
            value = data[i]
            if signed and value > 127:
                value -= 256
        elif length == 2:
            fmt = "<h" if signed else "<H"
            value = struct.unpack_from(fmt, data, i)[0]
        else:
            i += length
            continue

        results[name] = value * scale
        i += length

    return results


async def discover_shelly_sensors(scan_duration: float = 30.0) -> list[dict]:
    """
    Scan for Shelly Blu H&T sensors and return their MAC addresses and data.

    Args:
        scan_duration: How long to scan in seconds.

    Returns:
        List of dicts with keys: mac, name, temperature, humidity, battery
    """
    from bleak import BleakScanner

    discovered = {}

    def detection_callback(device, advertising_data):
        # Check for BTHome service data
        service_data = advertising_data.service_data
        if BTHOME_UUID not in service_data:
            return

        payload = service_data[BTHOME_UUID]
        parsed = parse_bthome_payload(payload)

        if "temperature" in parsed or "humidity" in parsed:
            mac = device.address.upper()
            name = advertising_data.local_name or device.name or "Shelly Blu"
            discovered[mac] = {
                "mac": mac,
                "name": name,
                "rssi": advertising_data.rssi,
                **parsed,
            }

    scanner = BleakScanner(detection_callback=detection_callback)
    logger.info("Scanning for Shelly Blu sensors (%ds)...", scan_duration)
    await scanner.start()
    await asyncio.sleep(scan_duration)
    await scanner.stop()

    results = list(discovered.values())
    if results:
        logger.info("Found %d Shelly Blu sensor(s):", len(results))
        for s in results:
            logger.info(
                "  %s (%s): %.1f°C, %.1f%% RH, battery %d%%",
                s.get("name", "?"),
                s["mac"],
                s.get("temperature", 0),
                s.get("humidity", 0),
                s.get("battery", 0),
            )
    else:
        logger.warning("No Shelly Blu sensors found. Is the sensor nearby and awake?")

    return results


async def poll_shelly_ble(scan_duration: float = 60.0) -> list[dict]:
    """
    Scan for Shelly Blu H&T advertisements and store readings.

    The sensor advertises roughly every 3-10 minutes, so we scan for
    a longer window to catch at least one advertisement per sensor.

    Args:
        scan_duration: How long to listen for advertisements (seconds).

    Returns:
        List of reading dicts that were stored.
    """
    from bleak import BleakScanner

    readings = {}

    def detection_callback(device, advertising_data):
        service_data = advertising_data.service_data
        if BTHOME_UUID not in service_data:
            return

        payload = service_data[BTHOME_UUID]
        parsed = parse_bthome_payload(payload)

        if "temperature" in parsed or "humidity" in parsed:
            mac = device.address.upper()
            name = advertising_data.local_name or device.name or "Shelly Blu"
            readings[mac] = {
                "mac": mac,
                "name": name,
                "rssi": advertising_data.rssi,
                **parsed,
            }

    scanner = BleakScanner(detection_callback=detection_callback)
    await scanner.start()
    await asyncio.sleep(scan_duration)
    await scanner.stop()

    if not readings:
        logger.debug("No Shelly Blu advertisements received this cycle")
        return []

    # Store readings
    conn = get_connection()
    stored = []

    for mac, data in readings.items():
        # Use configured name or fall back to advertised name
        friendly_name = SHELLY_SENSORS.get(mac, data["name"])
        ieee_address = f"shelly:{mac}"
        zone = ZONES.get(mac)

        upsert_sensor(
            conn,
            ieee_address=ieee_address,
            friendly_name=friendly_name,
            model="Shelly Blu H&T",
            zone=zone,
        )

        insert_reading(
            conn,
            ieee_address=ieee_address,
            temperature_c=data.get("temperature"),
            humidity_pct=data.get("humidity"),
            battery_pct=data.get("battery"),
            zone=zone,
        )

        stored.append(data)
        logger.info(
            "Shelly %s: %.1f°C, %.1f%% RH (battery: %d%%)",
            friendly_name,
            data.get("temperature", 0),
            data.get("humidity", 0),
            data.get("battery", 0),
        )

    conn.close()
    return stored
