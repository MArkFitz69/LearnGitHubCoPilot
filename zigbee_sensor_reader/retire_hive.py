"""Explicit, backed-up cleanup of the four confirmed retired Hive identities."""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from .config import DATABASE_PATH, RETIRED_HIVE_IDS


def retired_rows(conn):
    rows = []
    for device_id in sorted(RETIRED_HIVE_IDS):
        address = f"hive:{device_id}"
        row = conn.execute("""
            SELECT s.friendly_name, s.last_seen, MAX(r.timestamp), COUNT(r.id)
            FROM sensors s LEFT JOIN readings r ON r.ieee_address=s.ieee_address
            WHERE s.ieee_address=? GROUP BY s.ieee_address
        """, (address,)).fetchone()
        if row:
            rows.append((address, *row))
    return rows


def purge_retired(conn, now=None):
    """Delete only the allowlisted IDs, refusing any with recent activity."""
    now = now or datetime.now(timezone.utc)
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = retired_rows(conn)
        for address, name, last_seen, last_reading, count in rows:
            for stamp in (last_seen, last_reading):
                if stamp:
                    observed = datetime.fromisoformat(stamp)
                    if observed.tzinfo is None:
                        observed = observed.astimezone()
                    if observed > now - timedelta(days=1):
                        raise ValueError(f"Refusing to remove recently active device {address}")
        for address, *_ in rows:
            for table in ("readings", "mqtt_packet_state", "sensor_source_state", "sensors"):
                conn.execute(f"DELETE FROM {table} WHERE ieee_address=?", (address,))
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Back up then delete; default is preview only")
    args = parser.parse_args()
    path = Path(DATABASE_PATH).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as conn:
        for address, name, seen, reading, count in retired_rows(conn):
            print(f"{address} | {name} | last_seen={seen} | latest_reading={reading} | rows={count}")
        if not args.apply:
            print("Preview only. Stop the collector and run with --apply to back up and remove these IDs.")
            return
        backup = path.with_name(path.name + ".before-retired-hive-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        with closing(sqlite3.connect(backup)) as destination:
            conn.backup(destination)
        print(f"Backup: {backup}")
        removed = purge_retired(conn)
        print(f"Removed {len(removed)} retired device records and their readings; other devices preserved.")


if __name__ == "__main__":
    main()
