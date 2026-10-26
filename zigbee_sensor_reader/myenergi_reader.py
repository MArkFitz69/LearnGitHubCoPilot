"""myenergi (zappi, eddi, harvi) reader using the hub's cloud status API.

Credentials come from the environment (set on the collector host only):
MYENERGI_HUB_SERIAL and MYENERGI_API_KEY (the hub password set in the app).
"""

import asyncio
import logging
import os

import requests
from requests.auth import HTTPDigestAuth

from .config import SENSOR_STORE_INTERVAL_SECONDS
from .database import get_connection, insert_reading, upsert_sensor

logger = logging.getLogger(__name__)

MYENERGI_HUB_SERIAL = os.environ.get("MYENERGI_HUB_SERIAL", "")
MYENERGI_API_KEY = os.environ.get("MYENERGI_API_KEY", "")
DIRECTOR_HOST = "director.myenergi.net"
REQUEST_TIMEOUT_SECONDS = 20

ZAPPI_MODES = {1: "FAST", 2: "ECO", 3: "ECO+", 4: "OFF"}
EDDI_STATES = {
    1: "PAUSED", 3: "DIVERTING", 4: "BOOST", 5: "MAX_TEMP", 6: "STOPPED",
}
ZAPPI_STATES = {1: "PAUSED", 3: "DIVERTING", 5: "COMPLETE"}


def parse_status(payload) -> list[dict]:
    """Convert a /cgi-jstatus-* payload into flat reading dicts."""
    readings: list[dict] = []
    grid_seen = False
    for block in payload if isinstance(payload, list) else [payload]:
        if not isinstance(block, dict):
            continue
        for kind in ("zappi", "eddi", "harvi"):
            for dev in block.get(kind) or []:
                sno = dev.get("sno")
                if sno is None:
                    continue
                reading = {
                    "ieee_address": f"myenergi:{kind}:{sno}",
                    "friendly_name": f"myenergi {kind.capitalize()}",
                    "model": f"myenergi {kind.capitalize()}",
                    "temperature_c": None,
                    "power_w": None,
                    "energy_kwh": None,
                    "state": None,
                    "heating_mode": None,
                }
                if kind == "zappi":
                    reading["power_w"] = float(dev.get("div", 0))
                    reading["energy_kwh"] = dev.get("che")
                    reading["state"] = dev.get("pst") or ZAPPI_STATES.get(
                        dev.get("sta")
                    )
                    reading["heating_mode"] = ZAPPI_MODES.get(dev.get("zmo"))
                elif kind == "eddi":
                    reading["power_w"] = float(dev.get("div", 0))
                    reading["energy_kwh"] = dev.get("che")
                    reading["state"] = EDDI_STATES.get(dev.get("sta"))
                    probe = dev.get("tp1", dev.get("tp"))
                    if probe is not None and probe >= 0:
                        reading["temperature_c"] = float(probe)
                else:
                    reading["power_w"] = float(dev.get("ectp1", 0))
                    reading["state"] = dev.get("ectt1")
                readings.append(reading)

                if kind in ("zappi", "eddi") and not grid_seen and (
                    "grd" in dev or "gen" in dev
                ):
                    grid_seen = True
                    for key, name in (("grd", "Grid"), ("gen", "Generation")):
                        readings.append({
                            "ieee_address": f"myenergi:{key}",
                            "friendly_name": f"myenergi {name}",
                            "model": "myenergi CT",
                            "temperature_c": None,
                            "power_w": float(dev.get(key, 0)),
                            "energy_kwh": None,
                            "state": None,
                            "heating_mode": None,
                        })
    return readings


def _fetch_status_sync() -> list:
    auth = HTTPDigestAuth(MYENERGI_HUB_SERIAL, MYENERGI_API_KEY)
    headers = {"Accept": "application/json"}
    host = DIRECTOR_HOST
    for _ in range(2):
        resp = requests.get(
            f"https://{host}/cgi-jstatus-*",
            auth=auth,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        asn = resp.headers.get("x_myenergi-asn")
        if asn and asn != host:
            host = asn
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("myenergi director redirect loop")


async def fetch_myenergi_data() -> list[dict]:
    payload = await asyncio.to_thread(_fetch_status_sync)
    return parse_status(payload)


def store_myenergi_readings(readings: list[dict]) -> None:
    conn = get_connection()
    try:
        for reading in readings:
            upsert_sensor(
                conn,
                ieee_address=reading["ieee_address"],
                friendly_name=reading["friendly_name"],
                model=reading["model"],
            )
            insert_reading(
                conn,
                ieee_address=reading["ieee_address"],
                temperature_c=reading["temperature_c"],
                humidity_pct=None,
                state=reading["state"],
                power_w=reading["power_w"],
                energy_kwh=reading["energy_kwh"],
                heating_mode=reading["heating_mode"],
                source="myenergi",
                min_interval_seconds=SENSOR_STORE_INTERVAL_SECONDS,
            )
    finally:
        conn.close()


async def poll_myenergi() -> list[dict]:
    """Fetch and store myenergi readings; returns what was fetched."""
    readings = await fetch_myenergi_data()
    if readings:
        store_myenergi_readings(readings)
        logger.info("myenergi: %d readings processed", len(readings))
    return readings
