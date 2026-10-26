from datetime import datetime, timezone
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from zigbee_sensor_reader import database, hive_reader
from zigbee_sensor_reader.config import RETIRED_HIVE_IDS
from zigbee_sensor_reader.retire_hive import purge_retired, retired_rows
from zigbee_sensor_reader import retire_hive


class RetiredHiveTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        database._create_tables(self.conn)
        self.active = "hive:f0d0f1e4-4d5f-46de-b64a-5cd67065e24f"
        for address in [self.active, *(f"hive:{item}" for item in RETIRED_HIVE_IDS)]:
            database.upsert_sensor(self.conn, address, "Hive Stella TRV", "Hive Thermostat")
            database.insert_reading(self.conn, address, 20, None)
        self.conn.execute("UPDATE sensors SET last_seen='2026-10-20T13:56:22'")
        self.conn.execute("UPDATE readings SET timestamp='2026-10-20T13:56:22'")
        self.conn.commit()
        self.original_ids = {row[0] for row in self.conn.execute("SELECT ieee_address FROM sensors")}

    def tearDown(self):
        self.conn.close()

    def test_preview_preserves_and_purge_removes_only_exact_retired_ids(self):
        self.assertEqual(len(retired_rows(self.conn)), 4)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sensors").fetchone()[0], len(self.original_ids))
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.assertEqual(len(purge_retired(self.conn, datetime(2026, 10, 26, tzinfo=timezone.utc))), 4)
        self.assertEqual(
            {row[0] for row in self.conn.execute("SELECT ieee_address FROM sensors")},
            self.original_ids - {f"hive:{item}" for item in RETIRED_HIVE_IDS},
        )
        self.assertEqual(self.conn.execute("SELECT ieee_address FROM readings").fetchone()[0], self.active)
        self.assertEqual(purge_retired(self.conn), [])

    def test_recent_reading_refuses_entire_cleanup(self):
        address = "hive:" + sorted(RETIRED_HIVE_IDS)[0]
        self.conn.execute("UPDATE readings SET timestamp='2026-10-26T11:00:00+00:00' WHERE ieee_address=?", (address,))
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "recently active"):
            purge_retired(self.conn, datetime(2026, 10, 26, 12, tzinfo=timezone.utc))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sensors").fetchone()[0], len(self.original_ids))

    def test_retired_ids_cannot_be_stored_again(self):
        data = {"heating": [{"device_id": item} for item in RETIRED_HIVE_IDS]}
        with mock.patch.object(hive_reader, "get_connection") as connection, \
                mock.patch.object(hive_reader, "upsert_sensor") as upsert, \
                mock.patch.object(hive_reader, "insert_reading") as insert:
            hive_reader.store_hive_readings(data)
        upsert.assert_not_called()
        insert.assert_not_called()

    def test_apply_command_creates_backup_before_deleting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sensors.db"
            destination = sqlite3.connect(path)
            self.conn.backup(destination)
            destination.close()
            with mock.patch.object(retire_hive, "DATABASE_PATH", str(path)), \
                    mock.patch("sys.argv", ["retire_hive", "--apply"]), \
                    mock.patch("builtins.print"):
                retire_hive.main()
            backups = list(Path(folder).glob("sensors.db.before-retired-hive-*"))
            self.assertEqual(len(backups), 1)
            for file, expected in ((backups[0], len(self.original_ids)), (path, len(self.original_ids) - 4)):
                connection = sqlite3.connect(file)
                try:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM sensors").fetchone()[0], expected)
                finally:
                    connection.close()
