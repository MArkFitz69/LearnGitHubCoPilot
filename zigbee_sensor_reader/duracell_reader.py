"""Opt-in read-only Duracell energy collection, with explicit verification quality."""

import asyncio
import logging
import os
import time

from pymodbus.client import ModbusTcpClient

from .config import SENSOR_STORE_INTERVAL_SECONDS
from .database import get_connection
from .duracell_diagnostic import DiagnosticError, ENERGY_BLOCKS, energy_values, read_block
from .energy_data import (
    DAILY_METRICS, LIVE_METRICS, LONDON, record_health, store_daily, store_sample, utc_now,
)

logger = logging.getLogger(__name__)
DURACELL_HOST = os.environ.get("DURACELL_HOST", "").strip()
DURACELL_PORT = int(os.environ.get("DURACELL_PORT", "502"))
DURACELL_UNIT = int(os.environ.get("DURACELL_UNIT", "1"))
DURACELL_VERIFIED_METRICS = {
    key.strip() for key in os.environ.get("DURACELL_VERIFIED_METRICS", "").split(",")
    if key.strip()
}
DURACELL_COUNTER_DAY_TIMEZONE = os.environ.get("DURACELL_COUNTER_DAY_TIMEZONE", "")


def fetch_snapshot():
    if not DURACELL_HOST or not 1 <= DURACELL_UNIT <= 247 or not 1 <= DURACELL_PORT <= 65535:
        raise ValueError("Duracell requires an explicit host and valid port/unit (1..247)")
    unknown = DURACELL_VERIFIED_METRICS - (set(LIVE_METRICS) | set(DAILY_METRICS))
    if unknown:
        raise ValueError(f"Unknown DURACELL_VERIFIED_METRICS: {', '.join(sorted(unknown))}")
    registers = {}
    client = ModbusTcpClient(
        DURACELL_HOST, port=DURACELL_PORT, timeout=3, retries=0,
    )
    try:
        if not client.connect():
            raise DiagnosticError("Duracell connection failed")
        for index, (address, count) in enumerate(ENERGY_BLOCKS):
            if index:
                time.sleep(0.25)
            registers.update({
                address + offset: value
                for offset, value in enumerate(read_block(client, address, count, DURACELL_UNIT))
            })
    finally:
        client.close()
    live, daily = energy_values(registers)
    if not 0 <= live["battery_soc_pct"] <= 100:
        raise DiagnosticError("Duracell SOC outside 0..100; candidate register map mismatch")
    return live, daily


def collect_snapshot():
    conn = get_connection()
    try:
        live, daily = fetch_snapshot()
        now = utc_now()
        qualities = {
            key: "verified" if key in DURACELL_VERIFIED_METRICS else "provisional"
            for key in live
        }
        stored = store_sample(
            conn, "duracell", "inverter", live, qualities,
            now, SENSOR_STORE_INTERVAL_SECONDS,
        )
        # Daily counters are stored with explicit provenance and never promoted
        # merely because the snapshot looks plausible.
        if stored:
            for metric, value in daily.items():
                verified = (
                    metric in DURACELL_VERIFIED_METRICS
                    and DURACELL_COUNTER_DAY_TIMEZONE == "Europe/London"
                )
                store_daily(
                    conn, "duracell", "inverter", now.astimezone(LONDON).date(),
                    metric, value, "verified" if verified else "provisional",
                    "partial", (
                        "Device day-to-date counter; last observation, not a final daily total"
                        if verified else
                        "Counter scale/reset timezone needs comparison with Dura-i"
                    ), now, monotonic=True,
                )
        record_health(conn, "duracell", now=now)
        logger.info("Duracell energy snapshot processed (%s)", "stored" if stored else "throttled")
    except Exception as exc:
        record_health(conn, "duracell", error=str(exc))
        raise
    finally:
        conn.close()


async def poll_duracell():
    await asyncio.to_thread(collect_snapshot)
