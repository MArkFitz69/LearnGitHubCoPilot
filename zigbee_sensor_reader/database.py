"""SQLite persistence, schema migrations, and atomic sensor writes."""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import (
    DATABASE_PATH,
    SHELLY_COUNTER_RESET_SECONDS,
    SHELLY_HEARTBEAT_REPEAT_SECONDS,
    SHELLY_SOURCE_FALLBACK_SECONDS,
)

SCHEMA_VERSION = 5
logger = logging.getLogger(__name__)


def get_connection() -> sqlite3.Connection:
    """Return an initialized connection owned by the calling thread."""
    db_path = Path(DATABASE_PATH).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        _create_tables(conn)
        return conn
    except Exception:
        conn.close()
        raise


def _create_tables(conn: sqlite3.Connection) -> None:
    """Create the active schema and run each data migration once."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sensors (
            ieee_address TEXT PRIMARY KEY,
            friendly_name TEXT,
            model TEXT,
            zone TEXT,
            zone_override TEXT,
            name_source TEXT DEFAULT 'config',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ieee_address TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            reading_date TEXT,
            reading_time TEXT,
            temperature_c REAL,
            humidity_pct REAL,
            battery_pct REAL,
            link_quality INTEGER,
            zone TEXT,
            state TEXT,
            power_w REAL,
            energy_kwh REAL,
            heating_on INTEGER,
            boost_on INTEGER,
            target_temp_c REAL,
            heating_mode TEXT,
            device_min_temp_c REAL,
            device_max_temp_c REAL,
            device_min_humidity_pct REAL,
            device_max_humidity_pct REAL,
            battery_voltage_mv REAL,
            rssi INTEGER,
            FOREIGN KEY (ieee_address) REFERENCES sensors(ieee_address)
        );
        CREATE INDEX IF NOT EXISTS idx_readings_sensor_time
            ON readings (ieee_address, timestamp);
        CREATE INDEX IF NOT EXISTS idx_readings_timestamp ON readings (timestamp);
        CREATE TABLE IF NOT EXISTS mqtt_packet_state (
            ieee_address TEXT PRIMARY KEY,
            packet_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            source TEXT,
            FOREIGN KEY (ieee_address) REFERENCES sensors(ieee_address)
        );
        CREATE TABLE IF NOT EXISTS sensor_source_state (
            ieee_address TEXT NOT NULL,
            source TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            PRIMARY KEY (ieee_address, source),
            FOREIGN KEY (ieee_address) REFERENCES sensors(ieee_address)
        );
        """
    )
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return

    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_legacy_columns(conn)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_readings_zone ON readings (zone)"
        )
        if version < 1:
            conn.execute(
                """
                UPDATE readings
                SET reading_date = COALESCE(reading_date, substr(timestamp, 1, 10)),
                    reading_time = COALESCE(reading_time, substr(timestamp, 12, 8))
                WHERE reading_date IS NULL OR reading_time IS NULL
                """
            )
        if version < 2:
            _migrate_normalise_ieee(conn)
            _migrate_outdoor_shelly(conn)
        if version < 3:
            _migrate_attic_shelly(conn)
        if version < 5:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mqtt_device_health (
                    device_key TEXT PRIMARY KEY,
                    topic_prefix TEXT NOT NULL,
                    reported_status TEXT,
                    status_retained INTEGER,
                    status_observed_at TEXT,
                    last_communication_at TEXT,
                    last_live_topic TEXT,
                    last_offline_at TEXT
                )
                """
            )
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _ensure_legacy_columns(conn: sqlite3.Connection) -> None:
    columns = {
        "sensors": {
            "zone": "TEXT",
            "zone_override": "TEXT",
            "name_source": "TEXT DEFAULT 'config'",
        },
        "readings": {
            "zone": "TEXT",
            "heating_on": "INTEGER",
            "boost_on": "INTEGER",
            "target_temp_c": "REAL",
            "heating_mode": "TEXT",
            "device_min_temp_c": "REAL",
            "device_max_temp_c": "REAL",
            "device_min_humidity_pct": "REAL",
            "device_max_humidity_pct": "REAL",
            "battery_voltage_mv": "REAL",
            "rssi": "INTEGER",
            "reading_date": "TEXT",
            "reading_time": "TEXT",
            "state": "TEXT",
            "power_w": "REAL",
            "energy_kwh": "REAL",
        },
        "mqtt_packet_state": {"source": "TEXT"},
    }
    for table, required in columns.items():
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        for name, declaration in required.items():
            if name not in existing:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                )


def _normalise_ieee(raw: str) -> str:
    addr = raw.lower().strip()
    if addr.startswith("0x"):
        addr = addr[2:]
    if ":" not in addr and len(addr) == 16:
        addr = ":".join(addr[index:index + 2] for index in range(0, 16, 2))
    return addr


def _migrate_normalise_ieee(conn: sqlite3.Connection) -> None:
    commit_when_done = not conn.in_transaction
    for row in conn.execute(
        "SELECT ieee_address FROM sensors WHERE ieee_address LIKE '0x%'"
    ).fetchall():
        old = row["ieee_address"]
        canonical = _normalise_ieee(old)
        if canonical == old:
            continue
        exists = conn.execute(
            "SELECT 1 FROM sensors WHERE ieee_address=?", (canonical,)
        ).fetchone()
        if not exists:
            conn.execute(
                """
                INSERT INTO sensors (
                    ieee_address, friendly_name, model, zone, zone_override,
                    name_source, first_seen, last_seen
                )
                SELECT ?, friendly_name, model, zone, zone_override,
                       name_source, first_seen, last_seen
                FROM sensors WHERE ieee_address=?
                """,
                (canonical, old),
            )
        conn.execute(
            "UPDATE readings SET ieee_address=? WHERE ieee_address=?",
            (canonical, old),
        )
        conn.execute("DELETE FROM sensors WHERE ieee_address=?", (old,))
    if commit_when_done:
        conn.commit()


def _migrate_outdoor_shelly(conn: sqlite3.Connection) -> None:
    commit_when_done = not conn.in_transaction
    canonical = "shelly:94:B2:16:08:82:98"
    legacy = "shelly:esp32:outdoor_ht"
    legacy_row = conn.execute(
        "SELECT 1 FROM sensors WHERE ieee_address=?", (legacy,)
    ).fetchone()
    canonical_row = conn.execute(
        "SELECT 1 FROM sensors WHERE ieee_address=?", (canonical,)
    ).fetchone()
    if legacy_row:
        if not canonical_row:
            conn.execute(
                """
                INSERT INTO sensors (
                    ieee_address, friendly_name, model, zone, zone_override,
                    name_source, first_seen, last_seen
                )
                SELECT ?, friendly_name, model, zone, zone_override,
                       name_source, first_seen, last_seen
                FROM sensors WHERE ieee_address=?
                """,
                (canonical, legacy),
            )
        conn.execute(
            "UPDATE readings SET ieee_address=? WHERE ieee_address=?",
            (canonical, legacy),
        )
        packet = conn.execute(
            """
            SELECT packet_id, updated_at, source FROM mqtt_packet_state
            WHERE ieee_address IN (?, ?)
            ORDER BY updated_at DESC LIMIT 1
            """,
            (canonical, legacy),
        ).fetchone()
        conn.execute(
            "DELETE FROM mqtt_packet_state WHERE ieee_address IN (?, ?)",
            (canonical, legacy),
        )
        conn.execute("DELETE FROM sensors WHERE ieee_address=?", (legacy,))
        if packet:
            conn.execute(
                """
                INSERT OR REPLACE INTO mqtt_packet_state
                    (ieee_address, packet_id, updated_at, source)
                VALUES (?, ?, ?, ?)
                """,
                (canonical, packet["packet_id"], packet["updated_at"], packet["source"]),
            )
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        """
        INSERT INTO sensors
            (ieee_address, friendly_name, model, zone, name_source, first_seen, last_seen)
        VALUES (?, 'Outdoor', 'Shelly Blu H&T', 'Zone 4', 'config', ?, ?)
        ON CONFLICT(ieee_address) DO UPDATE SET
            friendly_name='Outdoor', zone='Zone 4',
            zone_override=CASE
                WHEN sensors.zone_override IN ('Zone 5', 'Attic') THEN NULL
                ELSE sensors.zone_override END,
            name_source='config'
        """,
        (canonical, now, now),
    )
    if commit_when_done:
        conn.commit()


def _migrate_attic_shelly(conn: sqlite3.Connection) -> None:
    commit_when_done = not conn.in_transaction
    canonical = "shelly:FC:4D:6A:1D:1D:FB"
    alias = "fc:4d:6a:ff:fe:1d:1d:fb"
    rows = conn.execute(
        """
        SELECT ieee_address, model, first_seen, last_seen FROM sensors
        WHERE ieee_address IN (?, ?)
        """,
        (canonical, alias),
    ).fetchall()
    reading_range = conn.execute(
        """
        SELECT MIN(timestamp) first_reading, MAX(timestamp) last_reading
        FROM readings WHERE ieee_address IN (?, ?)
        """,
        (canonical, alias),
    ).fetchone()
    first_values = [row["first_seen"] for row in rows if row["first_seen"]]
    last_values = [row["last_seen"] for row in rows if row["last_seen"]]
    if reading_range["first_reading"]:
        first_values.append(reading_range["first_reading"])
    if reading_range["last_reading"]:
        last_values.append(reading_range["last_reading"])
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    first_seen = min(first_values) if first_values else now
    last_seen = max(last_values) if last_values else now
    models = [row["model"] for row in rows if row["model"]]
    model = next(
        (value for value in models if value.upper() == "SBHT-203C"),
        models[0] if models else "Shelly Blu H&T",
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO sensors (
            ieee_address, friendly_name, model, zone, zone_override,
            name_source, first_seen, last_seen
        ) VALUES (?, 'Attic', ?, 'Zone 5', NULL, 'config', ?, ?)
        """,
        (canonical, model, first_seen, last_seen),
    )
    conn.execute(
        "UPDATE readings SET ieee_address=?, zone='Zone 5' WHERE ieee_address=?",
        (canonical, alias),
    )
    conn.execute(
        "UPDATE readings SET zone='Zone 5' WHERE ieee_address=?",
        (canonical,),
    )
    packet = conn.execute(
        """
        SELECT packet_id, updated_at, source FROM mqtt_packet_state
        WHERE ieee_address IN (?, ?) ORDER BY updated_at DESC LIMIT 1
        """,
        (canonical, alias),
    ).fetchone()
    conn.execute(
        "DELETE FROM mqtt_packet_state WHERE ieee_address IN (?, ?)",
        (canonical, alias),
    )
    conn.execute("DELETE FROM sensors WHERE ieee_address=?", (alias,))
    conn.execute(
        """
        INSERT INTO sensors (
            ieee_address, friendly_name, model, zone, zone_override,
            name_source, first_seen, last_seen
        ) VALUES (?, 'Attic', ?, 'Zone 5', NULL, 'config', ?, ?)
        ON CONFLICT(ieee_address) DO UPDATE SET
            friendly_name='Attic', model=excluded.model, zone='Zone 5',
            zone_override=CASE
                WHEN sensors.zone_override='Zone 4' THEN NULL
                ELSE sensors.zone_override END,
            name_source='config', first_seen=excluded.first_seen,
            last_seen=excluded.last_seen
        """,
        (canonical, model, first_seen, last_seen),
    )
    if packet:
        conn.execute(
            """
            INSERT INTO mqtt_packet_state (ieee_address, packet_id, updated_at, source)
            VALUES (?, ?, ?, ?)
            """,
            (canonical, packet["packet_id"], packet["updated_at"], packet["source"]),
        )
    if commit_when_done:
        conn.commit()


def upsert_sensor(
    conn: sqlite3.Connection,
    ieee_address: str,
    friendly_name: str | None = None,
    model: str | None = None,
    zone: str | None = None,
    name_source: str = "config",
    zone_override: str | None = None,
) -> None:
    """Insert or update registry metadata with configured Shellys authoritative."""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    configured_shelly = ieee_address in {
        "shelly:94:B2:16:08:82:98",
        "shelly:FC:4D:6A:1D:1D:FB",
    }
    if configured_shelly:
        name_source = "config"
    conn.execute(
        """
        INSERT INTO sensors (
            ieee_address, friendly_name, model, zone, zone_override,
            name_source, first_seen, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ieee_address) DO UPDATE SET
            friendly_name=CASE
                WHEN ? OR excluded.name_source='z2m'
                    THEN COALESCE(excluded.friendly_name, sensors.friendly_name)
                WHEN sensors.name_source='z2m' THEN sensors.friendly_name
                ELSE COALESCE(excluded.friendly_name, sensors.friendly_name) END,
            model=COALESCE(excluded.model, sensors.model),
            zone=CASE
                WHEN ? THEN COALESCE(excluded.zone, sensors.zone)
                WHEN excluded.name_source='z2m' THEN excluded.zone
                ELSE COALESCE(excluded.zone, sensors.zone) END,
            zone_override=CASE
                WHEN ? THEN NULL
                WHEN excluded.name_source='z2m' THEN excluded.zone_override
                ELSE sensors.zone_override END,
            name_source=CASE
                WHEN ? THEN 'config'
                WHEN excluded.name_source='z2m' THEN 'z2m'
                ELSE sensors.name_source END,
            last_seen=excluded.last_seen
        """,
        (
            ieee_address, friendly_name, model, zone, zone_override,
            name_source, now, now,
            int(configured_shelly), int(configured_shelly),
            int(configured_shelly), int(configured_shelly),
        ),
    )
    conn.commit()


def set_sensor_zone_override(
    conn: sqlite3.Connection,
    ieee_address: str,
    zone_override: str | None,
) -> None:
    """Compatibility helper for internal metadata sync; the web API is read-only."""
    conn.execute(
        "UPDATE sensors SET zone_override=? WHERE ieee_address=?",
        (zone_override, ieee_address),
    )
    conn.commit()


def record_mqtt_device_activity(
    conn: sqlite3.Connection,
    device_key: str,
    topic_prefix: str,
    topic: str,
    retained: bool,
    reported_status: str | None = None,
    is_sensor_publication: bool = False,
    observed_at: str | None = None,
) -> None:
    """Persist an MQTT status/activity event without creating a sensor reading."""
    now = observed_at or datetime.now().isoformat(timespec="microseconds")
    normalized_status = (
        reported_status
        if reported_status in {"online", "offline"}
        else None
    )
    confirms_live = (
        not retained
        and (is_sensor_publication or normalized_status == "online")
    )
    conn.execute(
        """
        INSERT INTO mqtt_device_health (
            device_key, topic_prefix, reported_status, status_retained,
            status_observed_at, last_communication_at, last_live_topic,
            last_offline_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_key) DO UPDATE SET
            topic_prefix=excluded.topic_prefix,
            reported_status=CASE
                WHEN excluded.reported_status IS NOT NULL
                    THEN excluded.reported_status
                ELSE mqtt_device_health.reported_status END,
            status_retained=CASE
                WHEN excluded.reported_status IS NOT NULL
                    THEN excluded.status_retained
                ELSE mqtt_device_health.status_retained END,
            status_observed_at=CASE
                WHEN excluded.reported_status IS NOT NULL
                    THEN excluded.status_observed_at
                ELSE mqtt_device_health.status_observed_at END,
            last_communication_at=CASE
                WHEN excluded.last_communication_at IS NOT NULL
                    THEN excluded.last_communication_at
                ELSE mqtt_device_health.last_communication_at END,
            last_live_topic=CASE
                WHEN excluded.last_communication_at IS NOT NULL
                    THEN excluded.last_live_topic
                ELSE mqtt_device_health.last_live_topic END,
            last_offline_at=COALESCE(
                excluded.last_offline_at,
                mqtt_device_health.last_offline_at
            )
        """,
        (
            device_key,
            topic_prefix,
            normalized_status,
            int(retained) if normalized_status is not None else None,
            now if normalized_status is not None else None,
            now if confirms_live else None,
            topic if confirms_live else None,
            now if normalized_status == "offline" else None,
        ),
    )
    conn.commit()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


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
    source: str = "unknown",
    authoritative_source: str | None = None,
) -> bool:
    """Atomically store a reading and reject duplicate/stale BTHome packets."""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    packet_decision = None
    packet_delta = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        if packet_id is not None:
            if authoritative_source and source != authoritative_source:
                preferred = conn.execute(
                    """
                    SELECT last_seen FROM sensor_source_state
                    WHERE ieee_address=? AND source=?
                    """,
                    (ieee_address, authoritative_source),
                ).fetchone()
                if preferred:
                    age = (
                        datetime.now() - _parse_timestamp(preferred["last_seen"])
                    ).total_seconds()
                    if age < SHELLY_SOURCE_FALLBACK_SECONDS:
                        logger.info(
                            "Rejected BTHome packet identity=%s packet_id=%d "
                            "delta=not-checked source=%s reason=non-authoritative "
                            "preferred_source=%s preferred_age_seconds=%.1f "
                            "fallback_seconds=%d",
                            ieee_address,
                            packet_id,
                            source,
                            authoritative_source,
                            age,
                            SHELLY_SOURCE_FALLBACK_SECONDS,
                        )
                        conn.rollback()
                        return False

            previous = conn.execute(
                """
                SELECT packet_id, updated_at, source FROM mqtt_packet_state
                WHERE ieee_address=?
                """,
                (ieee_address,),
            ).fetchone()
            if previous:
                source_takeover = (
                    authoritative_source
                    and source == authoritative_source
                    and previous["source"] != authoritative_source
                )
                packet_delta = (
                    int(packet_id) - int(previous["packet_id"])
                ) % 256
                packet_age = (
                    datetime.now() - _parse_timestamp(previous["updated_at"])
                ).total_seconds()
                if packet_delta == 0:
                    authoritative_repeat = (
                        authoritative_source
                        and source == authoritative_source
                        and previous["source"] == source
                    )
                    if (
                        not authoritative_repeat
                        or packet_age < SHELLY_HEARTBEAT_REPEAT_SECONDS
                    ):
                        logger.info(
                            "Rejected BTHome packet identity=%s packet_id=%d "
                            "delta=0 source=%s reason=%s age_seconds=%.1f "
                            "heartbeat_seconds=%d previous_source=%s "
                            "authoritative_source=%s",
                            ieee_address,
                            packet_id,
                            source,
                            (
                                "rapid-repeat"
                                if authoritative_repeat
                                else "duplicate"
                            ),
                            packet_age,
                            SHELLY_HEARTBEAT_REPEAT_SECONDS,
                            previous["source"],
                            authoritative_source,
                        )
                        conn.rollback()
                        return False
                    packet_decision = "authoritative-heartbeat"
                elif not source_takeover and not 1 <= packet_delta <= 127:
                    authoritative_reset = (
                        authoritative_source
                        and source == authoritative_source
                        and previous["source"] == source
                        and packet_age >= SHELLY_COUNTER_RESET_SECONDS
                    )
                    if authoritative_reset:
                        packet_decision = "authoritative-counter-reset"
                    else:
                        logger.warning(
                            "Rejected BTHome packet identity=%s packet_id=%d "
                            "delta=%d source=%s reason=stale-or-out-of-range "
                            "age_seconds=%.1f counter_reset_seconds=%d "
                            "previous_packet_id=%d previous_source=%s "
                            "authoritative_source=%s",
                            ieee_address,
                            packet_id,
                            packet_delta,
                            source,
                            packet_age,
                            SHELLY_COUNTER_RESET_SECONDS,
                            previous["packet_id"],
                            previous["source"],
                            authoritative_source,
                        )
                        conn.rollback()
                        return False
                else:
                    packet_decision = (
                        "authoritative-source-takeover"
                        if source_takeover
                        else "advancing-sequence"
                    )
            else:
                packet_decision = "first-packet"

        conn.execute(
            """
            INSERT INTO readings (
                ieee_address, timestamp, reading_date, reading_time,
                temperature_c, humidity_pct, battery_pct, link_quality, zone,
                state, power_w, energy_kwh, heating_on, boost_on, target_temp_c,
                heating_mode, device_min_temp_c, device_max_temp_c,
                device_min_humidity_pct, device_max_humidity_pct,
                battery_voltage_mv, rssi
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ieee_address, now, now[:10], now[11:19], temperature_c,
                humidity_pct, battery_pct, link_quality, zone, state, power_w,
                energy_kwh, int(heating_on) if heating_on is not None else None,
                int(boost_on) if boost_on is not None else None, target_temp_c,
                heating_mode, device_min_temp_c, device_max_temp_c,
                device_min_humidity_pct, device_max_humidity_pct,
                battery_voltage_mv, rssi,
            ),
        )
        conn.execute(
            "UPDATE sensors SET last_seen=? WHERE ieee_address=?",
            (now, ieee_address),
        )
        if packet_id is not None:
            conn.execute(
                """
                INSERT INTO mqtt_packet_state
                    (ieee_address, packet_id, updated_at, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ieee_address) DO UPDATE SET
                    packet_id=excluded.packet_id,
                    updated_at=excluded.updated_at,
                    source=excluded.source
                """,
                (ieee_address, int(packet_id), now, source),
            )
            conn.execute(
                """
                INSERT INTO sensor_source_state (ieee_address, source, last_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(ieee_address, source) DO UPDATE SET
                    last_seen=excluded.last_seen
                """,
                (ieee_address, source, now),
            )
        conn.commit()
        if packet_id is not None:
            logger.info(
                "Accepted BTHome packet identity=%s packet_id=%d delta=%s "
                "source=%s reason=%s",
                ieee_address,
                packet_id,
                packet_delta if packet_delta is not None else "new",
                source,
                packet_decision,
            )
        return True
    except Exception:
        conn.rollback()
        raise
