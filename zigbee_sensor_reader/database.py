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
            zone_override TEXT,
            name_source   TEXT DEFAULT 'config',
            first_seen    TEXT NOT NULL,
            last_seen     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS readings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ieee_address  TEXT    NOT NULL,
            timestamp     TEXT    NOT NULL,
            reading_date  TEXT,
            reading_time  TEXT,
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

        CREATE TABLE IF NOT EXISTS onboarding_state (
            id                INTEGER PRIMARY KEY CHECK (id = 1),
            pairing_active    INTEGER NOT NULL DEFAULT 0,
            pairing_started_at TEXT,
            pairing_ends_at   TEXT,
            candidate_ieee    TEXT,
            candidate_model   TEXT,
            candidate_joined_at TEXT,
            first_reading_at  TEXT,
            metadata_saved    INTEGER NOT NULL DEFAULT 0,
            tcp_precheck_ok   INTEGER,
            tcp_postcheck_ok  INTEGER,
            last_error        TEXT,
            updated_at        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS onboarding_commands (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            command       TEXT NOT NULL,
            payload       TEXT,
            status        TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT NOT NULL,
            processed_at  TEXT,
            error         TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_onboarding_commands_status
            ON onboarding_commands (status, id);

        CREATE TABLE IF NOT EXISTS onboarding_temp_codes (
            code_hash     TEXT PRIMARY KEY,
            created_at    TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            revoked_at    TEXT
        );
        """
    )
    # Add columns to existing databases (safe to run multiple times)
    migrations = [
        ("sensors", "zone", "TEXT"),
        ("sensors", "zone_override", "TEXT"),
        ("sensors", "name_source", "TEXT"),
        ("readings", "zone", "TEXT"),
        ("readings", "heating_on", "INTEGER"),
        ("readings", "boost_on", "INTEGER"),
        ("readings", "target_temp_c", "REAL"),
        ("readings", "heating_mode", "TEXT"),
        ("readings", "device_min_temp_c", "REAL"),
        ("readings", "device_max_temp_c", "REAL"),
        ("readings", "device_min_humidity_pct", "REAL"),
        ("readings", "device_max_humidity_pct", "REAL"),
        ("readings", "battery_voltage_mv", "REAL"),
        ("readings", "rssi", "INTEGER"),
        ("readings", "reading_date", "TEXT"),
        ("readings", "reading_time", "TEXT"),
    ]
    for table, col, col_type in migrations:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # column already exists

    # Backfill derived date/time columns for older rows.
    conn.execute(
        """
        UPDATE readings
        SET reading_date = COALESCE(reading_date, substr(timestamp, 1, 10)),
            reading_time = COALESCE(reading_time, substr(timestamp, 12, 8))
        WHERE reading_date IS NULL OR reading_time IS NULL
        """
    )
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        """
        INSERT OR IGNORE INTO onboarding_state (id, updated_at)
        VALUES (1, ?)
        """,
        (now,),
    )
    conn.commit()


def upsert_sensor(
    conn: sqlite3.Connection,
    ieee_address: str,
    friendly_name: str | None = None,
    model: str | None = None,
    zone: str | None = None,
    name_source: str = "config",
) -> None:
    """Insert or update a sensor in the registry.

    ``name_source`` should be ``"z2m"`` when the name comes from Zigbee2MQTT,
    or ``"config"`` (default) when it comes from config.py.

    z2m names always overwrite; config names only write when no z2m name
    has been set yet (i.e. when name_source is not already 'z2m').
    """
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if name_source == "z2m":
        # z2m always wins — unconditionally update name, model, and source
        conn.execute(
            """
            INSERT INTO sensors (ieee_address, friendly_name, model, zone, name_source, first_seen, last_seen)
            VALUES (?, ?, ?, ?, 'z2m', ?, ?)
            ON CONFLICT(ieee_address) DO UPDATE SET
                friendly_name = COALESCE(excluded.friendly_name, sensors.friendly_name),
                model         = COALESCE(excluded.model, sensors.model),
                zone          = COALESCE(excluded.zone, sensors.zone),
                name_source   = 'z2m',
                last_seen     = excluded.last_seen
            """,
            (ieee_address, friendly_name, model, zone, now, now),
        )
    else:
        # config.py — only set the name if z2m hasn't already claimed it
        conn.execute(
            """
            INSERT INTO sensors (ieee_address, friendly_name, model, zone, name_source, first_seen, last_seen)
            VALUES (?, ?, ?, ?, 'config', ?, ?)
            ON CONFLICT(ieee_address) DO UPDATE SET
                friendly_name = CASE
                    WHEN sensors.name_source = 'z2m' THEN sensors.friendly_name
                    ELSE COALESCE(excluded.friendly_name, sensors.friendly_name)
                END,
                model         = COALESCE(excluded.model, sensors.model),
                zone          = COALESCE(excluded.zone, sensors.zone),
                name_source   = CASE
                    WHEN sensors.name_source = 'z2m' THEN 'z2m'
                    ELSE 'config'
                END,
                last_seen     = excluded.last_seen
            """,
            (ieee_address, friendly_name, model, zone, now, now),
        )
    conn.commit()


def set_sensor_zone_override(
    conn: sqlite3.Connection,
    ieee_address: str,
    zone_override: str | None,
) -> None:
    """Set (or clear) a dashboard zone override for a sensor.

    ``zone_override=None`` clears the override so the config.py zone is used.
    """
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        """
        UPDATE sensors SET zone_override = ?, last_seen = ?
        WHERE ieee_address = ?
        """,
        (zone_override, now, ieee_address),
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
    device_min_humidity_pct: float | None = None,
    device_max_humidity_pct: float | None = None,
    battery_voltage_mv: float | None = None,
    rssi: int | None = None,
) -> None:
    """Store a single sensor reading."""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    reading_date = now[:10]
    reading_time = now[11:19]
    heating_int = int(heating_on) if heating_on is not None else None
    boost_int = int(boost_on) if boost_on is not None else None
    conn.execute(
        """
        INSERT INTO readings (ieee_address, timestamp, reading_date, reading_time, temperature_c, humidity_pct,
            battery_pct, link_quality, zone, heating_on, boost_on, target_temp_c,
            heating_mode, device_min_temp_c, device_max_temp_c,
            device_min_humidity_pct, device_max_humidity_pct,
            battery_voltage_mv, rssi)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ieee_address, now, reading_date, reading_time, temperature_c, humidity_pct, battery_pct, link_quality,
         zone, heating_int, boost_int, target_temp_c, heating_mode,
         device_min_temp_c, device_max_temp_c,
         device_min_humidity_pct, device_max_humidity_pct,
         battery_voltage_mv, rssi),
    )
    # Also touch the sensor's last_seen
    conn.execute(
        "UPDATE sensors SET last_seen = ? WHERE ieee_address = ?",
        (now, ieee_address),
    )
    conn.commit()
