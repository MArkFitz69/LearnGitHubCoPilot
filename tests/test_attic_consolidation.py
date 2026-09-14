import json
import os
import sqlite3
import tempfile
import unittest

from zigbee_sensor_reader import web_server
from zigbee_sensor_reader.__main__ import handle_reading
from zigbee_sensor_reader.config import (
    SHELLY_IDENTITY_ALIASES,
    SHELLY_SENSORS,
    ZONES,
)
from zigbee_sensor_reader.database import (
    _create_tables,
    _migrate_attic_shelly,
    insert_reading,
    upsert_sensor,
)
from zigbee_sensor_reader.web_server import _build_dashboard_snapshot, app
from zigbee_sensor_reader.z2m_reader import Z2MReader
from zigbee_sensor_reader.z2m_sync import sync_z2m_devices


ATTIC = "shelly:FC:4D:6A:1D:1D:FB"
ATTIC_ALIAS = "fc:4d:6a:ff:fe:1d:1d:fb"
OUTDOOR = "shelly:94:B2:16:08:82:98"


class AtticMigrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _create_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def _seed_duplicate_attic(self):
        self.conn.execute("DELETE FROM mqtt_packet_state WHERE ieee_address IN (?, ?)", (ATTIC, ATTIC_ALIAS))
        self.conn.execute("DELETE FROM readings WHERE ieee_address IN (?, ?)", (ATTIC, ATTIC_ALIAS))
        self.conn.execute("DELETE FROM sensors WHERE ieee_address IN (?, ?)", (ATTIC, ATTIC_ALIAS))
        self.conn.execute(
            """
            INSERT INTO sensors (
                ieee_address, friendly_name, model, zone, zone_override,
                name_source, first_seen, last_seen
            ) VALUES (?, 'Attic', 'Shelly Blu H&T', 'Zone 5', 'Zone 4',
                      'config', '2026-01-02T00:00:00', '2026-01-03T00:00:00')
            """,
            (ATTIC,),
        )
        self.conn.execute(
            """
            INSERT INTO sensors (
                ieee_address, friendly_name, model, zone, zone_override,
                name_source, first_seen, last_seen
            ) VALUES (?, 'Shelly Attic', 'SBHT-203C', 'Zone 4', 'Zone 4',
                      'z2m', '2026-01-01T00:00:00', '2026-01-04T00:00:00')
            """,
            (ATTIC_ALIAS,),
        )
        for identity, timestamp, temperature in (
            (ATTIC, "2026-01-02T12:00:00", 18.0),
            (ATTIC_ALIAS, "2026-01-05T12:00:00", 19.0),
        ):
            self.conn.execute(
                """
                INSERT INTO readings (
                    ieee_address, timestamp, reading_date, reading_time,
                    temperature_c, humidity_pct, battery_pct
                ) VALUES (?, ?, substr(?, 1, 10), substr(?, 12, 8), ?, 55, 90)
                """,
                (identity, timestamp, timestamp, timestamp, temperature),
            )
        self.conn.execute(
            "INSERT INTO mqtt_packet_state VALUES (?, 10, '2026-01-03T00:00:00')",
            (ATTIC,),
        )
        self.conn.execute(
            "INSERT INTO mqtt_packet_state VALUES (?, 11, '2026-01-06T00:00:00')",
            (ATTIC_ALIAS,),
        )
        self.conn.commit()

    def test_config_contains_two_physical_shellys_and_attic_alias(self):
        self.assertEqual(SHELLY_SENSORS["94:B2:16:08:82:98"], "Outdoor")
        self.assertEqual(ZONES["94:B2:16:08:82:98"], "Zone 4")
        self.assertEqual(SHELLY_SENSORS["FC:4D:6A:1D:1D:FB"], "Attic")
        self.assertEqual(ZONES["FC:4D:6A:1D:1D:FB"], "Zone 5")
        self.assertEqual(SHELLY_IDENTITY_ALIASES[ATTIC_ALIAS], ATTIC)

    def test_migration_merges_history_metadata_packet_state_and_is_idempotent(self):
        self._seed_duplicate_attic()

        _migrate_attic_shelly(self.conn)
        _migrate_attic_shelly(self.conn)

        sensors = self.conn.execute(
            """
            SELECT ieee_address, friendly_name, model, zone, zone_override,
                   name_source, first_seen, last_seen
            FROM sensors
            WHERE ieee_address IN (?, ?)
            """,
            (ATTIC, ATTIC_ALIAS),
        ).fetchall()
        self.assertEqual(len(sensors), 1)
        sensor = sensors[0]
        self.assertEqual(sensor["ieee_address"], ATTIC)
        self.assertEqual(sensor["friendly_name"], "Attic")
        self.assertEqual(sensor["model"], "SBHT-203C")
        self.assertEqual(sensor["zone"], "Zone 5")
        self.assertIsNone(sensor["zone_override"])
        self.assertEqual(sensor["name_source"], "config")
        self.assertEqual(sensor["first_seen"], "2026-01-01T00:00:00")
        self.assertEqual(sensor["last_seen"], "2026-01-05T12:00:00")

        readings = self.conn.execute(
            "SELECT ieee_address, timestamp, temperature_c, zone FROM readings ORDER BY timestamp"
        ).fetchall()
        self.assertEqual(len(readings), 2)
        self.assertEqual({row["ieee_address"] for row in readings}, {ATTIC})
        self.assertEqual(
            [(row["timestamp"], row["temperature_c"]) for row in readings],
            [
                ("2026-01-02T12:00:00", 18.0),
                ("2026-01-05T12:00:00", 19.0),
            ],
        )
        self.assertEqual({row["zone"] for row in readings}, {"Zone 5"})

        packet = self.conn.execute(
            "SELECT ieee_address, packet_id, updated_at FROM mqtt_packet_state"
        ).fetchone()
        self.assertEqual(dict(packet), {
            "ieee_address": ATTIC,
            "packet_id": 11,
            "updated_at": "2026-01-06T00:00:00",
        })


class AtticZ2MTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _create_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def _device_payload():
        return json.dumps([
            {
                "ieee_address": "0xfc4d6afffe1d1dfb",
                "friendly_name": "Shelly Attic",
                "description": "Zone 4",
                "definition": {"model": "SBHT-203C"},
            }
        ])

    def test_bridge_and_reading_use_canonical_attic_without_recreation(self):
        reader = Z2MReader(
            on_reading=lambda reading: handle_reading(reading, self.conn),
            get_conn_fn=lambda: self.conn,
        )
        reader.handle_message("zigbee2mqtt/bridge/devices", self._device_payload())
        reader.handle_message(
            "zigbee2mqtt/Shelly Attic",
            json.dumps({
                "temperature": 18.5,
                "humidity": 61,
                "battery": 88,
                "device": {
                    "ieee_address": "0xfc4d6afffe1d1dfb",
                    "model": "SBHT-203C",
                    "description": "Zone 4",
                },
            }),
        )
        reader.handle_message("zigbee2mqtt/bridge/devices", self._device_payload())

        rows = self.conn.execute(
            "SELECT ieee_address, friendly_name, model, zone, zone_override FROM sensors WHERE ieee_address IN (?, ?)",
            (ATTIC, ATTIC_ALIAS),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ieee_address"], ATTIC)
        self.assertEqual(rows[0]["friendly_name"], "Attic")
        self.assertEqual(rows[0]["model"], "SBHT-203C")
        self.assertEqual(rows[0]["zone"], "Zone 5")
        self.assertIsNone(rows[0]["zone_override"])
        reading = self.conn.execute(
            "SELECT ieee_address, zone, temperature_c FROM readings"
        ).fetchone()
        self.assertEqual(reading["ieee_address"], ATTIC)
        self.assertEqual(reading["zone"], "Zone 5")
        self.assertEqual(reading["temperature_c"], 18.5)

    def test_legacy_z2m_sync_uses_same_canonical_mapping(self):
        self.assertEqual(sync_z2m_devices(self._device_payload(), self.conn), 1)
        row = self.conn.execute(
            "SELECT ieee_address, friendly_name, zone, zone_override FROM sensors WHERE friendly_name = 'Attic'"
        ).fetchone()
        self.assertEqual(row["ieee_address"], ATTIC)
        self.assertEqual(row["zone"], "Zone 5")
        self.assertIsNone(row["zone_override"])
        self.assertIsNone(
            self.conn.execute(
                "SELECT ieee_address FROM sensors WHERE ieee_address = ?",
                (ATTIC_ALIAS,),
            ).fetchone()
        )

    def test_unrelated_z2m_device_keeps_normal_name_and_zone_behavior(self):
        payload = json.dumps([
            {
                "ieee_address": "0x00124b0025e7a1c3",
                "friendly_name": "Utility Sensor",
                "description": "Zone 2",
                "definition": {"model": "SNZB-02D"},
            }
        ])
        reader = Z2MReader(get_conn_fn=lambda: self.conn)
        reader.handle_message("zigbee2mqtt/bridge/devices", payload)
        row = self.conn.execute(
            """
            SELECT ieee_address, friendly_name, zone_override, name_source
            FROM sensors WHERE friendly_name = 'Utility Sensor'
            """
        ).fetchone()
        self.assertEqual(row["ieee_address"], "00:12:4b:00:25:e7:a1:c3")
        self.assertEqual(row["zone_override"], "Zone 2")
        self.assertEqual(row["name_source"], "z2m")


class AtticSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "sensor_data.db")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        _create_tables(conn)
        upsert_sensor(conn, OUTDOOR, "Outdoor", "Shelly Blu H&T", "Zone 4")
        insert_reading(conn, OUTDOOR, 12.5, 70, battery_pct=95, packet_id=1)
        upsert_sensor(conn, ATTIC, "Attic", "SBHT-203C", "Zone 5")
        insert_reading(conn, ATTIC, 18.5, 61, battery_pct=88)
        upsert_sensor(conn, ATTIC_ALIAS, "Shelly Attic", "SBHT-203C", "Zone 4")
        conn.execute(
            """
            INSERT INTO readings (
                ieee_address, timestamp, reading_date, reading_time,
                temperature_c, humidity_pct, battery_pct, zone
            ) VALUES (?, '2026-01-01T00:00:00', '2026-01-01', '00:00:00',
                      17.0, 60, 87, 'Zone 4')
            """,
            (ATTIC_ALIAS,),
        )
        conn.commit()
        _migrate_attic_shelly(conn)
        conn.close()
        self.original_db_path = web_server.DATABASE_PATH
        web_server.DATABASE_PATH = self.db_path
        self.client = app.test_client()

    def tearDown(self):
        web_server.DATABASE_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_dashboard_groups_known_shellys_once(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        with app.test_request_context("/api/dashboard"):
            snapshot = _build_dashboard_snapshot(conn)
        conn.close()

        self.assertEqual([row["ieee_address"] for row in snapshot["outdoor"]], [OUTDOOR])
        self.assertEqual([row["ieee_address"] for row in snapshot["attic"]], [ATTIC])
        self.assertEqual(snapshot["sonoff"], [])
        self.assertEqual(snapshot["shelly"], [])

        html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertEqual(html.count("<h2>Outdoor</h2>"), 1)
        self.assertEqual(html.count("<h2>Attic</h2>"), 1)
        self.assertNotIn("Other Shelly Blu H&amp;T Sensors", html)
        self.assertNotIn("Shelly Attic", html)

    def test_system_and_data_apis_expose_only_canonical_known_shellys(self):
        sensors = self.client.get("/api/sensors").get_json()
        attic_sensors = [row for row in sensors if row["friendly_name"] == "Attic"]
        outdoor_sensors = [row for row in sensors if row["friendly_name"] == "Outdoor"]
        self.assertEqual(len(attic_sensors), 1)
        self.assertEqual(attic_sensors[0]["ieee_address"], ATTIC)
        self.assertEqual(attic_sensors[0]["zone"], "Zone 5")
        self.assertEqual(len(outdoor_sensors), 1)
        self.assertEqual(outdoor_sensors[0]["ieee_address"], OUTDOOR)
        self.assertEqual(outdoor_sensors[0]["zone"], "Zone 4")

        latest = self.client.get("/api/readings/latest").get_json()
        self.assertEqual({row["ieee_address"] for row in latest}, {ATTIC, OUTDOOR})
        readings = self.client.get("/api/readings").get_json()
        self.assertEqual({row["ieee_address"] for row in readings}, {ATTIC, OUTDOOR})
        attic_readings = [row for row in readings if row["ieee_address"] == ATTIC]
        self.assertEqual(len(attic_readings), 2)
        self.assertEqual(
            {row["temperature_c"] for row in attic_readings},
            {17.0, 18.5},
        )
        self.assertEqual({row["zone"] for row in attic_readings}, {"Zone 5"})
        csv_data = self.client.get("/api/readings?format=csv").data
        export_data = self.client.get("/api/export/csv").data
        for payload in (csv_data, export_data):
            self.assertIn(ATTIC.encode(), payload)
            self.assertIn(OUTDOOR.encode(), payload)
            self.assertNotIn(ATTIC_ALIAS.encode(), payload)

        system = self.client.get("/api/system").get_json()
        attic_health = [
            row for row in system["sensor_status"]
            if row["friendly_name"] == "Attic"
        ]
        outdoor_health = [
            row for row in system["sensor_status"]
            if row["friendly_name"] == "Outdoor"
        ]
        self.assertEqual(len(attic_health), 1)
        self.assertEqual(attic_health[0]["ieee_address"], ATTIC)
        self.assertEqual(attic_health[0]["zone"], "Zone 5")
        expected_attic_last = max(
            row["timestamp"] for row in readings if row["ieee_address"] == ATTIC
        )
        self.assertEqual(attic_health[0]["last_ts"], expected_attic_last)
        self.assertEqual(len(outdoor_health), 1)
        self.assertEqual(outdoor_health[0]["ieee_address"], OUTDOOR)
        self.assertEqual(outdoor_health[0]["zone"], "Zone 4")
        self.assertIsNotNone(outdoor_health[0]["last_ts"])


if __name__ == "__main__":
    unittest.main()
