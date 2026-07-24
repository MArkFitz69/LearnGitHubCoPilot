"""
SQLite database layer for storing Zigbee sensor readings.

Tables:
  sensors  – registry of discovered sensors with friendly names
  readings – timestamped temperature & humidity readings
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from .config import DATABASE_PATH


def get_connection() -> sqlite3.Connection:
    """Return a connection to the SQLite database, creating it if needed."""
    db_path = Path(DATABASE_PATH).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # better concurrent read perf
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    """Create tables if they don't already exist."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sensors (
            ieee_address  TEXT PRIMARY KEY,
            friendly_name TEXT,
            model         TEXT,
            zone          TEXT,
            first_seen    TEXT NOT NULL,
            last_seen     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS readings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ieee_address  TEXT    NOT NULL,
            timestamp     TEXT    NOT NULL,
            temperature_c REAL,
            humidity_pct  REAL,
            battery_pct   REAL,
            link_quality  INTEGER,
            zone          TEXT,
            heating_on    INTEGER,
            boost_on      INTEGER,
            target_temp_c REAL,
            heating_mode  TEXT,
            FOREIGN KEY (ieee_address) REFERENCES sensors(ieee_address)
        );

        CREATE INDEX IF NOT EXISTS idx_readings_sensor_time
            ON readings (ieee_address, timestamp);

        CREATE INDEX IF NOT EXISTS idx_readings_timestamp
            ON readings (timestamp);

        CREATE INDEX IF NOT EXISTS idx_readings_zone
            ON readings (zone);
        """
    )
    # Add columns to existing databases (safe to run multiple times)
    migrations = [
        ("sensors", "zone", "TEXT"),
        ("readings", "zone", "TEXT"),
        ("readings", "heating_on", "INTEGER"),
        ("readings", "boost_on", "INTEGER"),
        ("readings", "target_temp_c", "REAL"),
        ("readings", "heating_mode", "TEXT"),
        ("readings", "device_min_temp_c", "REAL"),
        ("readings", "device_max_temp_c", "REAL"),
    ]
    for table, col, col_type in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()


def upsert_sensor(
    conn: sqlite3.Connection,
    ieee_address: str,
    friendly_name: str | None = None,
    model: str | None = None,
    zone: str | None = None,
) -> None:
    """Insert or update a sensor in the registry."""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        """
        INSERT INTO sensors (ieee_address, friendly_name, model, zone, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(ieee_address) DO UPDATE SET
            friendly_name = COALESCE(excluded.friendly_name, sensors.friendly_name),
            model         = COALESCE(excluded.model, sensors.model),
            zone          = COALESCE(excluded.zone, sensors.zone),
            last_seen     = excluded.last_seen
        """,
        (ieee_address, friendly_name, model, zone, now, now),
    )
    conn.commit()


def insert_reading(
    conn: sqlite3.Connection,
    ieee_address: str,
    temperature_c: float | None,
    humidity_pct: float | None,
    battery_pct: float | None = None,
    link_quality: int | None = None,
    zone: str | None = None,
    heating_on: bool | None = None,
    boost_on: bool | None = None,
    target_temp_c: float | None = None,
    heating_mode: str | None = None,
    device_min_temp_c: float | None = None,
    device_max_temp_c: float | None = None,
) -> None:
    """Store a single sensor reading."""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    heating_int = int(heating_on) if heating_on is not None else None
    boost_int = int(boost_on) if boost_on is not None else None
    conn.execute(
        """
        INSERT INTO readings (ieee_address, timestamp, temperature_c, humidity_pct,
            battery_pct, link_quality, zone, heating_on, boost_on, target_temp_c,
            heating_mode, device_min_temp_c, device_max_temp_c)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ieee_address, now, temperature_c, humidity_pct, battery_pct, link_quality,
         zone, heating_int, boost_int, target_temp_c, heating_mode,
         device_min_temp_c, device_max_temp_c),
    )
    # Also touch the sensor's last_seen
    conn.execute(
        "UPDATE sensors SET last_seen = ? WHERE ieee_address = ?",
        (now, ieee_address),
    )
    conn.commit()
