"""
Hive thermostat integration.

Pulls temperature data from Hive thermostats via the Hive cloud API.
Requires your Hive account credentials (set via environment variables).

Environment variables:
    HIVE_USERNAME - Your Hive account email
    HIVE_PASSWORD - Your Hive account password
"""

import asyncio
import logging
import os
from datetime import datetime

from .database import get_connection, upsert_sensor, insert_reading

logger = logging.getLogger(__name__)

# Hive credentials from environment
HIVE_USERNAME = os.environ.get("HIVE_USERNAME", "")
HIVE_PASSWORD = os.environ.get("HIVE_PASSWORD", "")


async def fetch_hive_data() -> list[dict]:
    """
    Connect to the Hive API and fetch thermostat data.

    Returns a list of dicts with keys:
        name, temperature_c, target_temp_c, mode, heating_on, battery_pct
    """
    from apyhiveapi import Auth, Hive

    if not HIVE_USERNAME or not HIVE_PASSWORD:
        logger.error(
            "Hive credentials not set. Set HIVE_USERNAME and HIVE_PASSWORD "
            "environment variables."
        )
        return []

    try:
        # Authenticate
        auth = Auth(username=HIVE_USERNAME, password=HIVE_PASSWORD)
        tokens = await auth.login()

        # Start session
        hive = Hive(username=HIVE_USERNAME, password=HIVE_PASSWORD)
        await hive.startSession({"tokens": tokens})

        results = []

        # Get heating devices
        for device_id, device in hive.session.data.devices.items():
            device_type = device.get("type", "")
            device_name = device.get("state", {}).get("name", device_id)

            # Look for thermostat/heating devices
            if device_type in ("heating", "thermostatui", "thermostat"):
                try:
                    heating_data = {
                        "name": device_name,
                        "device_id": device_id,
                        "temperature_c": None,
                        "target_temp_c": None,
                        "mode": None,
                        "heating_on": None,
                    }

                    # Try to get current temperature
                    if hasattr(hive, "heating"):
                        temp = await hive.heating.current_temperature(device)
                        if temp is not None:
                            heating_data["temperature_c"] = float(temp)

                        target = await hive.heating.target_temperature(device)
                        if target is not None:
                            heating_data["target_temp_c"] = float(target)

                        mode = await hive.heating.get_mode(device)
                        heating_data["mode"] = mode

                    results.append(heating_data)
                    logger.info(
                        "Hive %s: %.1f°C (target: %.1f°C, mode: %s)",
                        device_name,
                        heating_data["temperature_c"] or 0,
                        heating_data["target_temp_c"] or 0,
                        heating_data["mode"],
                    )
                except Exception as e:
                    logger.warning("Failed to read Hive device %s: %s", device_name, e)

        return results

    except Exception as e:
        logger.error("Hive API error: %s", e)
        return []


def store_hive_readings(readings: list[dict]) -> None:
    """Store Hive thermostat readings in the same SQLite database."""
    conn = get_connection()

    for reading in readings:
        # Use "hive:" prefix to distinguish from Zigbee sensors
        ieee_address = f"hive:{reading['device_id']}"
        friendly_name = f"Hive {reading['name']}"

        upsert_sensor(
            conn,
            ieee_address=ieee_address,
            friendly_name=friendly_name,
            model="Hive Thermostat",
        )

        insert_reading(
            conn,
            ieee_address=ieee_address,
            temperature_c=reading.get("temperature_c"),
            humidity_pct=None,  # Hive doesn't report humidity
            battery_pct=None,
        )

    conn.close()


async def poll_hive() -> list[dict]:
    """Fetch and store Hive thermostat data."""
    readings = await fetch_hive_data()
    if readings:
        store_hive_readings(readings)
    return readings


def run_hive_poll() -> list[dict]:
    """Synchronous wrapper for poll_hive."""
    return asyncio.run(poll_hive())
