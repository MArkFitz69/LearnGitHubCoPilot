"""myenergi (zappi, eddi, harvi) reader using the hub's cloud status API.

Credentials come from the environment (set on the collector host only):
MYENERGI_HUB_SERIAL and MYENERGI_API_KEY (generated in myenergi Products).
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

import requests
from requests.auth import HTTPDigestAuth

from .config import SENSOR_STORE_INTERVAL_SECONDS
from .database import get_connection, insert_reading, upsert_sensor
from .energy_data import (
    LONDON, day_bounds, history_totals, record_health, store_daily, store_sample, utc_now,
)

logger = logging.getLogger(__name__)

MYENERGI_HUB_SERIAL = os.environ.get("MYENERGI_HUB_SERIAL", "")
MYENERGI_API_KEY = os.environ.get("MYENERGI_API_KEY", "")
DIRECTOR_HOST = "director.myenergi.net"
REQUEST_TIMEOUT_SECONDS = 20
MYENERGI_HISTORY_VERIFIED = os.environ.get("MYENERGI_HISTORY_VERIFIED", "0") == "1"

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
                        if key not in dev:
                            continue
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


def _request_json(path):
    auth = HTTPDigestAuth(MYENERGI_HUB_SERIAL, MYENERGI_API_KEY)
    headers = {"Accept": "application/json"}
    host = DIRECTOR_HOST
    for _ in range(2):
        resp = requests.get(
            f"https://{host}{path}",
            auth=auth,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        asn = resp.headers.get("x_myenergi-asn")
        if asn and asn != "undefined" and asn != host:
            if not re.fullmatch(r"s[0-9]+\.myenergi\.net", asn):
                raise ValueError("Unexpected myenergi server hostname")
            host = asn
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("myenergi director redirect loop")


def _fetch_status_sync() -> list:
    return _request_json("/cgi-jstatus-*")


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
            identity = reading["ieee_address"]
            metrics = {}
            if identity == "myenergi:grd":
                metrics["grid_power_w"] = reading["power_w"]
            elif identity == "myenergi:gen":
                metrics["pv_power_w"] = reading["power_w"]
            elif identity.startswith(("myenergi:zappi:", "myenergi:eddi:")):
                metrics["device_power_w"] = reading["power_w"]
            if metrics:
                store_sample(
                    conn, "myenergi", identity, metrics,
                    {key: "verified" for key in metrics},
                    interval=SENSOR_STORE_INTERVAL_SECONDS,
                )
    finally:
        conn.close()


async def poll_myenergi() -> list[dict]:
    """Fetch and store myenergi readings; returns what was fetched."""
    try:
        readings = await fetch_myenergi_data()
        if not readings:
            raise ValueError("myenergi returned no supported devices")
        store_myenergi_readings(readings)
        conn = get_connection()
        try:
            record_health(conn, "myenergi")
        finally:
            conn.close()
        await asyncio.to_thread(collect_daily_history, readings)
        logger.info("myenergi: %d readings processed", len(readings))
        return readings
    except Exception as exc:
        conn = get_connection()
        try:
            record_health(conn, "myenergi", str(exc))
        finally:
            conn.close()
        raise


def fetch_device_history(kind, serial, day):
    if kind not in ("zappi", "eddi") or not str(serial).isdigit():
        raise ValueError("Expected a zappi/eddi serial")
    start, end = day_bounds(day)
    prefix = "Z" if kind == "zappi" else "E"
    hours = int((end - start).total_seconds() / 3600)
    return _request_json(
        f"/cgi-jdayhour-{prefix}{serial}-{start.year}-{start.month}-{start.day}-{start.hour}-{hours}"
    )


def collect_daily_history(readings):
    """Refresh current/previous UK day at most once per 15 minutes per device."""
    now = utc_now()
    today = now.astimezone(LONDON).date()
    conn = get_connection()
    try:
        for reading in readings:
            identity = reading["ieee_address"]
            if not identity.startswith(("myenergi:zappi:", "myenergi:eddi:")):
                continue
            _, kind, serial = identity.split(":")
            health_key = f"myenergi_history:{kind}:{serial}"
            previous = conn.execute(
                "SELECT last_attempt FROM energy_health WHERE source=?", (health_key,),
            ).fetchone()
            if previous:
                if (now - datetime.fromisoformat(previous["last_attempt"])).total_seconds() < 900:
                    continue
            errors = []
            for day in (today, today - timedelta(days=1)):
                try:
                    payload = fetch_device_history(kind, serial, day)
                    value, coverage, reason = history_totals(payload, serial, kind, day, now)
                    store_daily(
                        conn, "myenergi", identity, day, "device_kwh", value,
                        "verified" if MYENERGI_HISTORY_VERIFIED else "provisional",
                        coverage, reason + (
                            "" if MYENERGI_HISTORY_VERIFIED else
                            " Compare with app daily totals before verification."
                        ), now,
                    )
                except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                    # An empty day must not prevent recovery of the other day.
                    errors.append(f"{day.isoformat()}: {exc}")
                    logger.warning("myenergi %s history unavailable: %s", kind, exc)
            record_health(conn, health_key, "; ".join(errors) if errors else None, now)
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    import json
    from datetime import date

    parser = argparse.ArgumentParser(description="Read-only myenergi daily history check; no DB writes")
    parser.add_argument("--history-date", required=True, type=date.fromisoformat)
    parser.add_argument("--kind", required=True, choices=("zappi", "eddi"))
    parser.add_argument("--serial", required=True)
    args = parser.parse_args()
    if not MYENERGI_HUB_SERIAL or not MYENERGI_API_KEY:
        parser.error("Set MYENERGI_HUB_SERIAL and MYENERGI_API_KEY privately first")
    payload = fetch_device_history(args.kind, args.serial, args.history_date)
    total, coverage, reason = history_totals(
        payload, args.serial, args.kind, args.history_date,
    )
    print(json.dumps({"candidate_kwh": total, "coverage": coverage, "reason": reason}, indent=2))
