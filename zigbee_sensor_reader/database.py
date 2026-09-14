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

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
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
            state         TEXT,
            power_w       REAL,
            energy_kwh    REAL,
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

        CREATE TABLE IF NOT EXISTS mqtt_packet_state (
            ieee_address TEXT PRIMARY KEY,
            packet_id    INTEGER NOT NULL,
            updated_at   TEXT NOT NULL,
            FOREIGN KEY (ieee_address) REFERENCES sensors(ieee_address)
        );

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
        ("readings", "state", "TEXT"),
        ("readings", "power_w", "REAL"),
        ("readings", "energy_kwh", "REAL"),
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
    _migrate_normalise_ieee(conn)
    _migrate_outdoor_shelly(conn)
    _migrate_attic_shelly(conn)


def _normalise_ieee(raw: str) -> str:
    """Convert 0xaabbccddeeff0011 → aa:bb:cc:dd:ee:ff:00:11."""
    addr = raw.lower().strip()
    if addr.startswith("0x"):
        addr = addr[2:]
    if ":" not in addr and len(addr) == 16:
        addr = ":".join(addr[i:i + 2] for i in range(0, 16, 2))
    return addr


def _migrate_normalise_ieee(conn: sqlite3.Connection) -> None:
    """One-time migration: normalise raw 0x... IEEE addresses to colon format.

    Old bellows-era rows may have been stored as '0xf4b3b1fffe60ae82' while
    new z2m rows use 'f4:b3:b1:ff:fe:60:ae:82'.  This merges duplicate rows
    and re-points all readings to the canonical colon-format key.
    """
    raw_rows = conn.execute(
        "SELECT ieee_address FROM sensors WHERE ieee_address LIKE '0x%'"
    ).fetchall()
    if not raw_rows:
        return

    for row in raw_rows:
        raw_ieee = row["ieee_address"]
        canon = _normalise_ieee(raw_ieee)
        if canon == raw_ieee:
            continue  # already normalised (shouldn't happen if LIKE '0x%')

        existing = conn.execute(
            "SELECT ieee_address FROM sensors WHERE ieee_address = ?", (canon,)
        ).fetchone()

        if existing:
            # Canonical form already exists — re-point readings then drop the raw row
            conn.execute(
                "UPDATE readings SET ieee_address = ? WHERE ieee_address = ?",
                (canon, raw_ieee),
            )
            conn.execute("DELETE FROM sensors WHERE ieee_address = ?", (raw_ieee,))
        else:
            # Canonical form doesn't exist — just rename in place
            conn.execute(
                "UPDATE sensors SET ieee_address = ? WHERE ieee_address = ?",
                (canon, raw_ieee),
            )
            conn.execute(
                "UPDATE readings SET ieee_address = ? WHERE ieee_address = ?",
                (canon, raw_ieee),
            )
    conn.commit()


def _migrate_outdoor_shelly(conn: sqlite3.Connection) -> None:
    """Consolidate the transitional ESP identity into the physical BLE MAC."""
    canonical = "shelly:94:B2:16:08:82:98"
    legacy = "shelly:esp32:outdoor_ht"
    canonical_row = conn.execute(
        "SELECT ieee_address FROM sensors WHERE ieee_address = ?",
        (canonical,),
    ).fetchone()
    legacy_row = conn.execute(
        "SELECT ieee_address FROM sensors WHERE ieee_address = ?",
        (legacy,),
    ).fetchone()

    if legacy_row:
        if canonical_row:
            conn.execute(
                "UPDATE readings SET ieee_address = ? WHERE ieee_address = ?",
                (canonical, legacy),
            )
            packet_rows = conn.execute(
                """
                SELECT packet_id, updated_at FROM mqtt_packet_state
                WHERE ieee_address IN (?, ?)
                ORDER BY updated_at DESC LIMIT 1
                """,
                (canonical, legacy),
            ).fetchone()
            conn.execute("DELETE FROM mqtt_packet_state WHERE ieee_address = ?", (legacy,))
            if packet_rows:
                conn.execute(
                    """
                    INSERT INTO mqtt_packet_state (ieee_address, packet_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(ieee_address) DO UPDATE SET
                        packet_id = excluded.packet_id,
                        updated_at = excluded.updated_at
                    """,
                    (canonical, packet_rows["packet_id"], packet_rows["updated_at"]),
                )
            conn.execute("DELETE FROM sensors WHERE ieee_address = ?", (legacy,))
        else:
            conn.execute(
                "UPDATE sensors SET ieee_address = ? WHERE ieee_address = ?",
                (canonical, legacy),
            )
            conn.execute(
                "UPDATE readings SET ieee_address = ? WHERE ieee_address = ?",
                (canonical, legacy),
            )
            conn.execute(
                "UPDATE mqtt_packet_state SET ieee_address = ? WHERE ieee_address = ?",
                (canonical, legacy),
            )

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        """
        INSERT INTO sensors (
            ieee_address, friendly_name, model, zone, name_source, first_seen, last_seen
        )
        VALUES (?, 'Outdoor', 'Shelly Blu H&T', 'Zone 4', 'config', ?, ?)
        ON CONFLICT(ieee_address) DO UPDATE SET
            friendly_name = 'Outdoor',
            zone = 'Zone 4',
            zone_override = CASE
                WHEN sensors.zone_override IN ('Zone 5', 'Attic') THEN NULL
                ELSE sensors.zone_override
            END,
            name_source = 'config'
        """,
        (canonical, now, now),
    )
    conn.commit()


def _migrate_attic_shelly(conn: sqlite3.Connection) -> None:
    """Merge the Attic Z2M EUI-64 into the physical Shelly BLE identity."""
    canonical = "shelly:FC:4D:6A:1D:1D:FB"
    alias = "fc:4d:6a:ff:fe:1d:1d:fb"
    rows = conn.execute(
        """
        SELECT ieee_address, model, first_seen, last_seen
        FROM sensors
        WHERE ieee_address IN (?, ?)
        """,
        (canonical, alias),
    ).fetchall()
    by_identity = {row["ieee_address"]: row for row in rows}
    canonical_row = by_identity.get(canonical)
    alias_row = by_identity.get(alias)

    timestamps_first = [
        row["first_seen"] for row in rows if row["first_seen"]
    ]
    timestamps_last = [
        row["last_seen"] for row in rows if row["last_seen"]
    ]
    reading_range = conn.execute(
        """
        SELECT MIN(timestamp) AS first_reading, MAX(timestamp) AS last_reading
        FROM readings
        WHERE ieee_address IN (?, ?)
        """,
        (canonical, alias),
    ).fetchone()
    if reading_range["first_reading"]:
        timestamps_first.append(reading_range["first_reading"])
    if reading_range["last_reading"]:
        timestamps_last.append(reading_range["last_reading"])
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    first_seen = min(timestamps_first) if timestamps_first else now
    last_seen = max(timestamps_last) if timestamps_last else now
    models = [
        row["model"] for row in (canonical_row, alias_row)
        if row and row["model"]
    ]
    model = next(
        (value for value in models if value.upper() == "SBHT-203C"),
        models[0] if models else "Shelly Blu H&T",
    )

    conn.execute(
        "UPDATE readings SET ieee_address = ? WHERE ieee_address = ?",
        (canonical, alias),
    )
    conn.execute(
        "UPDATE readings SET zone = 'Zone 5' WHERE ieee_address = ?",
        (canonical,),
    )

    packet_row = conn.execute(
        """
        SELECT packet_id, updated_at
        FROM mqtt_packet_state
        WHERE ieee_address IN (?, ?)
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (canonical, alias),
    ).fetchone()
    conn.execute(
        "DELETE FROM mqtt_packet_state WHERE ieee_address IN (?, ?)",
        (canonical, alias),
    )
    conn.execute("DELETE FROM sensors WHERE ieee_address = ?", (alias,))

    conn.execute(
        """
        INSERT INTO sensors (
            ieee_address, friendly_name, model, zone, zone_override,
            name_source, first_seen, last_seen
        )
        VALUES (?, 'Attic', ?, 'Zone 5', NULL, 'config', ?, ?)
        ON CONFLICT(ieee_address) DO UPDATE SET
            friendly_name = 'Attic',
            model = excluded.model,
            zone = 'Zone 5',
            zone_override = CASE
                WHEN sensors.zone_override = 'Zone 4' THEN NULL
                ELSE sensors.zone_override
            END,
            name_source = 'config',
            first_seen = excluded.first_seen,
            last_seen = excluded.last_seen
        """,
        (canonical, model, first_seen, last_seen),
    )
    if packet_row:
        conn.execute(
            """
            INSERT INTO mqtt_packet_state (ieee_address, packet_id, updated_at)
            VALUES (?, ?, ?)
            """,
            (canonical, packet_row["packet_id"], packet_row["updated_at"]),
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
    state: str | None = None,
    power_w: float | None = None,
    energy_kwh: float | None = None,
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
    packet_id: int | None = None,
) -> bool:
    """Store a sensor reading, returning False for a duplicate MQTT packet."""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if packet_id is not None:
        previous = conn.execute(
            "SELECT packet_id FROM mqtt_packet_state WHERE ieee_address = ?",
            (ieee_address,),
        ).fetchone()
        if previous and previous["packet_id"] == packet_id:
            return False

    reading_date = now[:10]
    reading_time = now[11:19]
    heating_int = int(heating_on) if heating_on is not None else None
    boost_int = int(boost_on) if boost_on is not None else None
    conn.execute(
        """
        INSERT INTO readings (ieee_address, timestamp, reading_date, reading_time, temperature_c, humidity_pct,
            battery_pct, link_quality, zone, state, power_w, energy_kwh, heating_on, boost_on, target_temp_c,
            heating_mode, device_min_temp_c, device_max_temp_c,
            device_min_humidity_pct, device_max_humidity_pct,
            battery_voltage_mv, rssi)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ieee_address, now, reading_date, reading_time, temperature_c, humidity_pct, battery_pct, link_quality,
         zone, state, power_w, energy_kwh, heating_int, boost_int, target_temp_c, heating_mode,
         device_min_temp_c, device_max_temp_c,
         device_min_humidity_pct, device_max_humidity_pct,
         battery_voltage_mv, rssi),
    )
    # Also touch the sensor's last_seen
    conn.execute(
        "UPDATE sensors SET last_seen = ? WHERE ieee_address = ?",
        (now, ieee_address),
    )
    if packet_id is not None:
        conn.execute(
            """
            INSERT INTO mqtt_packet_state (ieee_address, packet_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(ieee_address) DO UPDATE SET
                packet_id = excluded.packet_id,
                updated_at = excluded.updated_at
            """,
            (ieee_address, packet_id, now),
        )
    conn.commit()
    return True
