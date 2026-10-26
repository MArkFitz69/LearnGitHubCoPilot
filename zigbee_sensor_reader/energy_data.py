"""Energy measurements and device counters, separate from sensor battery health."""

from datetime import date, datetime, time, timedelta, timezone
import json
import math
from zoneinfo import ZoneInfo

UTC = timezone.utc
LONDON = ZoneInfo("Europe/London")

LIVE_METRICS = {
    "grid_power_w": ("Grid exchange (+ import)", "W"),
    "battery_power_w": ("Battery power (+ discharge)", "W"),
    "battery_soc_pct": ("Battery state of charge", "%"),
    "battery_voltage_v": ("Battery voltage", "V"),
    "pv_power_w": ("Solar power", "W"),
    "mppt1_voltage_v": ("MPPT 1 voltage", "V"),
    "mppt2_voltage_v": ("MPPT 2 voltage", "V"),
    "load_power_w": ("Grid-tied load (not necessarily whole house)", "W"),
    "device_power_w": ("Device power", "W"),
}
DAILY_METRICS = {
    "grid_import_kwh": "Grid imported",
    "solar_kwh": "Solar generated",
    "battery_charge_kwh": "Battery charged",
    "battery_discharge_kwh": "Battery discharged",
    "device_kwh": "Device energy",
}


def utc_now():
    return datetime.now(UTC)


def stamp(value):
    if value.tzinfo is None:
        raise ValueError("Energy timestamps must be timezone aware")
    return value.astimezone(UTC).isoformat(timespec="seconds")


def day_bounds(day):
    """UTC boundaries of a London calendar day (23, 24 or 25 hours)."""
    start = datetime.combine(day, time.min, LONDON).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, LONDON).astimezone(UTC)
    return start, end


def tariff_context(now, day):
    now = now.astimezone(UTC)
    start, end = day_bounds(day)
    # All cheap intervals intersecting the chosen local calendar day.
    intervals = []
    cursor = start.date() - timedelta(days=1)
    while cursor <= end.date():
        cheap_start = datetime.combine(cursor, time(1), UTC)
        cheap_end = datetime.combine(cursor, time(8), UTC)
        if cheap_start < end and cheap_end > start:
            intervals.append({
                "start": stamp(max(start, cheap_start)),
                "end": stamp(min(end, cheap_end)),
                "label": (
                    f"{cheap_start.astimezone(LONDON):%H:%M %Z} - "
                    f"{cheap_end.astimezone(LONDON):%H:%M %Z}"
                ),
                "left_pct": 100 * (max(start, cheap_start) - start) / (end - start),
                "width_pct": 100 * (min(end, cheap_end) - max(start, cheap_start)) / (end - start),
            })
        cursor += timedelta(days=1)
    return {
        "cheap_now": 1 <= now.hour < 8,
        "fixed_window": "01:00-08:00 GMT / UTC",
        "local_now": now.astimezone(LONDON).isoformat(timespec="seconds"),
        "intervals": intervals,
    }


def create_energy_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS energy_samples (
            source TEXT NOT NULL, device TEXT NOT NULL, observed_at TEXT NOT NULL,
            metrics TEXT NOT NULL, quality TEXT NOT NULL,
            PRIMARY KEY (source, device, observed_at)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS energy_daily (
            source TEXT NOT NULL, device TEXT NOT NULL, day TEXT NOT NULL,
            metric TEXT NOT NULL, value REAL NOT NULL, quality TEXT NOT NULL,
            coverage TEXT NOT NULL, reason TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (source, device, day, metric)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS energy_health (
            source TEXT PRIMARY KEY, last_attempt TEXT NOT NULL,
            last_success TEXT, error TEXT
        )
    """)


def validate_numbers(metrics):
    for name, value in metrics.items():
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"Invalid numeric energy measurement: {name}")


def store_sample(conn, source, device, metrics, quality, now=None, interval=900):
    """Store unchanged snapshots too, once interval has elapsed per device."""
    validate_numbers(metrics)
    timestamp = stamp(now or utc_now())
    if not metrics:
        raise ValueError("An energy sample must contain measurements")
    if set(quality) != set(metrics) or any(
        value not in ("verified", "provisional") for value in quality.values()
    ):
        raise ValueError("Each energy metric needs explicit verification quality")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        last = conn.execute("""
            SELECT observed_at FROM energy_samples WHERE source=? AND device=?
            ORDER BY observed_at DESC LIMIT 1
        """, (source, device)).fetchone()
        if last and 0 <= (
            datetime.fromisoformat(timestamp) - datetime.fromisoformat(last["observed_at"])
        ).total_seconds() < interval:
            return False
        conn.execute("""
            INSERT OR IGNORE INTO energy_samples VALUES (?, ?, ?, ?, ?)
        """, (source, device, timestamp, json.dumps(metrics), json.dumps(quality)))
    return True


def store_daily(conn, source, device, day, metric, value, quality, coverage,
                reason, now=None, *, monotonic=False):
    """Replace a reported daily total, never add repeated totals together."""
    validate_numbers({metric: value})
    if value < 0 or metric not in DAILY_METRICS:
        raise ValueError("Invalid daily energy counter")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute("""
            SELECT value, quality FROM energy_daily
            WHERE source=? AND device=? AND day=? AND metric=?
        """, (source, device, day.isoformat(), metric)).fetchone()
        if monotonic and previous and (
            value < previous["value"] or previous["quality"] == "reset"
        ):
            quality = "reset"
            reason = "Counter decreased within the day; total needs validation"
        conn.execute("""
            INSERT INTO energy_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, device, day, metric) DO UPDATE SET
                value=excluded.value, quality=excluded.quality,
                coverage=excluded.coverage, reason=excluded.reason,
                updated_at=excluded.updated_at
        """, (
            source, device, day.isoformat(), metric, value, quality,
            coverage, reason, stamp(now or utc_now()),
        ))


def record_health(conn, source, error=None, now=None):
    timestamp = stamp(now or utc_now())
    with conn:
        conn.execute("""
            INSERT INTO energy_health VALUES (?, ?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                last_attempt=excluded.last_attempt,
                last_success=COALESCE(excluded.last_success, energy_health.last_success),
                error=excluded.error
        """, (source, timestamp, timestamp if error is None else None, error))


def history_totals(payload, serial, kind, day, now=None):
    """myenergi UTC hourly joule history, clipped to one UK local day.

    Upstream pymyenergi uses UTC requests and sums h1d/h1b through h3d/h3b.
    Sparse absent phase/boost fields are zero only when some device-energy
    fields exist. An entire response with no device-energy fields is unknown.
    """
    if kind not in ("eddi", "zappi"):
        raise ValueError("Unsupported history device")
    now = now or utc_now()
    start, end = day_bounds(day)
    rows = payload.get(f"U{serial}") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("myenergi returned no hourly history")
    fields = [f"h{phase}{suffix}" for phase in range(1, 4) for suffix in ("d", "b")]
    unique = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Malformed myenergi history row")
        timestamp = datetime(
            int(row["yr"]), int(row["mon"]), int(row["dom"]),
            int(row.get("hr", 0)), tzinfo=UTC,
        )
        if not start <= timestamp < min(end, now):
            continue
        validate_numbers({key: row[key] for key in fields if key in row})
        if any(row.get(key, 0) < 0 for key in fields):
            raise ValueError("Negative myenergi history energy")
        if timestamp in unique and unique[timestamp] != row:
            raise ValueError("Conflicting duplicate myenergi history hour")
        unique[timestamp] = row
    if not unique or not any(key in row for row in unique.values() for key in fields):
        raise ValueError("No device-energy fields in history; consumption is unknown")
    expected_end = min(end, now.replace(minute=0, second=0, microsecond=0))
    expected = {
        start + timedelta(hours=index)
        for index in range(max(0, int((expected_end - start).total_seconds() // 3600)))
    }
    complete = expected.issubset(unique) and now >= end
    value = sum(row.get(key, 0) for row in unique.values() for key in fields) / 3600000
    return value, "complete" if complete else "partial", (
        f"{len(unique)} hourly records; UTC history mapped to Europe/London. "
        + ("Full day present." if complete else "Day in progress or missing hours.")
    )
