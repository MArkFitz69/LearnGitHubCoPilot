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


async def fetch_hive_data() -> dict:
    """
    Connect to the Hive API and fetch thermostat + hot water data.

    Returns a dict with keys:
        "heating": list of heating dicts
        "hotwater": list of hot water dicts
    """
    from apyhiveapi import Hive

    if not HIVE_USERNAME or not HIVE_PASSWORD:
        logger.error(
            "Hive credentials not set. Set HIVE_USERNAME and HIVE_PASSWORD "
            "environment variables."
        )
        return {"heating": [], "hotwater": []}

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
            return {"heating": [], "hotwater": []}

        session = await hive.startSession()

        heating_results = []
        hotwater_results = []

        # ── Heating thermostats ─────────────────────────────────────────────
        for dev in session.get("climate", []):
            hive_name = dev.get("hiveName", "Unknown")
            device_id = dev.get("hiveID", dev.get("device_id", "unknown"))
            friendly_name = HIVE_NAMES.get(hive_name, hive_name)

            try:
                temp = await hive.heating.getCurrentTemperature(dev)
                target = await hive.heating.getTargetTemperature(dev)
                mode = await hive.heating.getMode(dev)
                state = await hive.heating.getState(dev)
                boost = await hive.heating.getBoostStatus(dev)
                battery = dev.get("deviceData", {}).get("battery")
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
                heating_results.append(heating_data)
                heat_status = "HEATING" if heating_on else "off"
                logger.info(
                    "Hive heating %s: %.1f°C (target: %.1f°C, mode: %s, heating: %s)",
                    friendly_name,
                    heating_data["temperature_c"] or 0,
                    heating_data["target_temp_c"] or 0,
                    heating_data["mode"],
                    heat_status,
                )
            except Exception as e:
                logger.warning("Failed to read Hive heating %s: %s", friendly_name, e)

        # ── Hot water ───────────────────────────────────────────────────────
        # Log the full session key list so we can see exactly where hot water lives
        logger.info("Hive session keys: %s", list(session.keys()))

        # Try all known key names across library versions
        hw_devices = (
            session.get("water_heater")
            or session.get("hotWater")
            or session.get("hotwater")
            or getattr(hive, "device_list", {}).get("water_heater")
            or getattr(hive, "device_list", {}).get("hotWater")
            or []
        )
        logger.info("Hive hot water devices found: %d", len(hw_devices))
        for dev in hw_devices:
            hive_name = dev.get("hiveName", dev.get("hive_name", "Hot Water"))
            device_id = dev.get("hiveID", dev.get("device_id", "hotwater"))
            friendly_name = HIVE_NAMES.get(hive_name, hive_name)

            # Discover available methods on hive.hotwater at runtime
            hw_api = hive.hotwater
            def _try_hw(method_names, arg):
                for name in method_names:
                    fn = getattr(hw_api, name, None)
                    if fn is not None:
                        return fn, name
                logger.warning(
                    "hive.hotwater has none of %s — available: %s",
                    method_names,
                    [m for m in dir(hw_api) if not m.startswith("_")],
                )
                return None, None

            try:
                get_mode_fn, _ = _try_hw(["getMode", "get_mode", "mode"], dev)
                get_state_fn, _ = _try_hw(["getState", "get_state", "state"], dev)
                get_boost_fn, _ = _try_hw(["getBoostStatus", "get_boost_status", "boostStatus"], dev)

                mode = await get_mode_fn(dev) if get_mode_fn else None
                state = await get_state_fn(dev) if get_state_fn else None
                boost = await get_boost_fn(dev) if get_boost_fn else None

                hw_on = state not in ("OFF", None, False)
                boost_active = boost not in ("OFF", None, False)

                hw_data = {
                    "name": friendly_name,
                    "device_id": device_id,
                    "hw_on": hw_on,
                    "mode": mode,
                    "boost": boost_active,
                    "zone": ZONES.get(hive_name),
                }
                hotwater_results.append(hw_data)
                logger.info(
                    "Hive hot water %s: state=%s, mode=%s, boost=%s",
                    friendly_name, "ON" if hw_on else "OFF", mode, boost,
                )
            except Exception as e:
                logger.warning("Failed to read Hive hot water %s: %s", friendly_name, e)

        return {"heating": heating_results, "hotwater": hotwater_results}

    except Exception as e:
        logger.error("Hive API error: %s", e)
        return {"heating": [], "hotwater": []}


def store_hive_readings(data: dict) -> None:
    """Store Hive heating + hot water readings in SQLite."""
    conn = get_connection()

    for reading in data.get("heating", []):
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
            boost_on=reading.get("boost") not in ("OFF", None, False),
            target_temp_c=reading.get("target_temp_c"),
            heating_mode=reading.get("mode"),
        )

    for hw in data.get("hotwater", []):
        # "hive-hw:" prefix distinguishes hot water from heating thermostats
        ieee_address = f"hive-hw:{hw['device_id']}"
        friendly_name = f"Hive Hot Water"
        zone = hw.get("zone")

        upsert_sensor(
            conn,
            ieee_address=ieee_address,
            friendly_name=friendly_name,
            model="Hive Hot Water",
            zone=zone,
        )

        insert_reading(
            conn,
            ieee_address=ieee_address,
            temperature_c=None,  # hot water cylinder has no temp sensor
            humidity_pct=None,
            zone=zone,
            heating_on=hw.get("hw_on"),    # True when actively heating water
            boost_on=hw.get("boost"),
            heating_mode=hw.get("mode"),   # SCHEDULE / ON / OFF / BOOST
        )

    conn.close()


async def poll_hive() -> dict:
    """Fetch and store Hive heating + hot water data."""
    data = await fetch_hive_data()
    if data["heating"] or data["hotwater"]:
        store_hive_readings(data)
    return data


def run_hive_poll() -> dict:
    """Synchronous wrapper for poll_hive."""
    return asyncio.run(poll_hive())
