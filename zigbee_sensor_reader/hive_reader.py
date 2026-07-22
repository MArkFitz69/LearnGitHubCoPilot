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

from .config import HIVE_NAMES, ZONES
from .database import get_connection, upsert_sensor, insert_reading

logger = logging.getLogger(__name__)

# Hive credentials from environment
HIVE_USERNAME = os.environ.get("HIVE_USERNAME", "")
HIVE_PASSWORD = os.environ.get("HIVE_PASSWORD", "")


async def fetch_hive_data() -> list[dict]:
    """
    Connect to the Hive API and fetch thermostat data.

    Returns a list of dicts with keys:
        name, device_id, temperature_c, target_temp_c, mode, battery_pct
    """
    from apyhiveapi import Hive

    if not HIVE_USERNAME or not HIVE_PASSWORD:
        logger.error(
            "Hive credentials not set. Set HIVE_USERNAME and HIVE_PASSWORD "
            "environment variables."
        )
        return []

    try:
        # Login and start session
        hive = Hive(username=HIVE_USERNAME, password=HIVE_PASSWORD)
        login_result = await hive.login()

        # Check for 2FA
        if isinstance(login_result, dict) and login_result.get("ChallengeName") == "SMS_MFA":
            logger.error(
                "Hive account requires SMS 2FA. This is not supported in "
                "unattended mode. Disable 2FA or use an app-specific password."
            )
            return []

        session = await hive.startSession()

        results = []

        # Iterate over climate (thermostat) devices
        for dev in session.get("climate", []):
            hive_name = dev.get("hiveName", "Unknown")
            device_id = dev.get("hiveID", dev.get("device_id", "unknown"))
            # Use friendly name from config, fall back to Hive's name
            friendly_name = HIVE_NAMES.get(hive_name, hive_name)

            try:
                # Get current and target temperature via the heating helper
                temp = await hive.heating.getCurrentTemperature(dev)
                target = await hive.heating.getTargetTemperature(dev)
                mode = await hive.heating.getMode(dev)
                state = await hive.heating.getState(dev)
                boost = await hive.heating.getBoostStatus(dev)

                # Battery from deviceData
                battery = dev.get("deviceData", {}).get("battery")

                # Heating is on if state is not OFF
                heating_on = state not in ("OFF", None, False)

                heating_data = {
                    "name": friendly_name,
                    "device_id": device_id,
                    "temperature_c": float(temp) if temp is not None else None,
                    "target_temp_c": float(target) if target is not None else None,
                    "mode": mode,
                    "heating_on": heating_on,
                    "boost": boost,
                    "battery_pct": battery,
                    "zone": ZONES.get(hive_name),
                }

                results.append(heating_data)
                heat_status = "HEATING" if heating_on else "off"
                logger.info(
                    "Hive %s: %.1f°C (target: %.1f°C, mode: %s, heating: %s)",
                    friendly_name,
                    heating_data["temperature_c"] or 0,
                    heating_data["target_temp_c"] or 0,
                    heating_data["mode"],
                    heat_status,
                )
            except Exception as e:
                logger.warning("Failed to read Hive device %s: %s", friendly_name, e)

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
        zone = reading.get("zone")

        upsert_sensor(
            conn,
            ieee_address=ieee_address,
            friendly_name=friendly_name,
            model="Hive Thermostat",
            zone=zone,
        )

        insert_reading(
            conn,
            ieee_address=ieee_address,
            temperature_c=reading.get("temperature_c"),
            humidity_pct=None,  # Hive doesn't report humidity
            battery_pct=reading.get("battery_pct"),
            zone=zone,
            heating_on=reading.get("heating_on"),
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
