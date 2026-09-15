import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import ssl
import sys
import tempfile
import threading
import unittest
from unittest import mock

from zigbee_sensor_reader import database, export, hive_reader, web_server
from zigbee_sensor_reader.__main__ import handle_reading, main
from zigbee_sensor_reader.database import get_connection, insert_reading, upsert_sensor
from zigbee_sensor_reader.mqtt_config import MQTTSettings
from zigbee_sensor_reader.web_server import _calculate_hive_runtime_seconds, app
from zigbee_sensor_reader.z2m_reader import Z2MReader, _extract_energy_kwh


OUTDOOR = "shelly:94:B2:16:08:82:98"


class SecurityRegressionTests(unittest.TestCase):
    def test_removed_mutation_routes_and_unsafe_handlers(self):
        client = app.test_client()
        self.assertEqual(client.get("/onboarding").status_code, 404)
        self.assertEqual(client.post("/api/onboarding/auth").status_code, 404)
        self.assertIn(
            client.patch(f"/api/sensors/{OUTDOOR}/zone").status_code,
            (404, 405),
        )
        source = Path(web_server.__file__).read_text(encoding="utf-8-sig")
        self.assertNotIn("onclick=", source)
        self.assertNotIn("zoneEdit(", source)
        self.assertNotIn("innerHTML", source)

    def test_removed_modules_dependencies_and_service_secret(self):
        package = Path(web_server.__file__).parent
        for filename in ("onboarding.py", "zigbee_reader.py", "z2m_sync.py"):
            self.assertFalse((package / filename).exists())
        requirements = (package.parent / "requirements.txt").read_text()
        self.assertNotIn("bellows", requirements)
        self.assertNotIn("zigpy", requirements)
        service = (package / "sensor-data-api.service").read_text()
        self.assertNotIn("ONBOARDING_PASSCODE", service)


class MigrationAndConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "sensor_data.db")
        self.original_path = database.DATABASE_PATH
        database.DATABASE_PATH = self.db_path

    def tearDown(self):
        database.DATABASE_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_old_database_upgrades_once_and_preserves_legacy_tables(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE sensors (
                ieee_address TEXT PRIMARY KEY, friendly_name TEXT, model TEXT,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
            );
            CREATE TABLE readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ieee_address TEXT NOT NULL,
                timestamp TEXT NOT NULL, temperature_c REAL, humidity_pct REAL,
                battery_pct REAL, link_quality INTEGER
            );
            CREATE TABLE mqtt_packet_state (
                ieee_address TEXT PRIMARY KEY, packet_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE onboarding_state (id INTEGER PRIMARY KEY);
            INSERT INTO sensors VALUES (
                '0x00124b0025e7a1c3', 'Legacy', 'SNZB-02D',
                '2025-01-01T00:00:00', '2025-01-01T00:00:00'
            );
            INSERT INTO readings (
                ieee_address, timestamp, temperature_c, humidity_pct
            ) VALUES (
                '0x00124b0025e7a1c3', '2025-01-01T00:00:00', 20, 50
            );
            """
        )
        conn.close()

        first = get_connection()
        self.assertEqual(
            first.execute("PRAGMA user_version").fetchone()[0],
            database.SCHEMA_VERSION,
        )
        self.assertIsNotNone(
            first.execute(
                "SELECT 1 FROM sqlite_master WHERE name='onboarding_state'"
            ).fetchone()
        )
        self.assertEqual(
            first.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 1
        )
        self.assertEqual(
            first.execute("SELECT reading_date FROM readings").fetchone()[0],
            "2025-01-01",
        )
        first.close()

        second = get_connection()
        self.assertEqual(
            second.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 1
        )
        second.close()

    def test_independent_thread_connections_do_not_drop_writes(self):
        get_connection().close()
        errors = []

        def writer(worker: int):
            try:
                conn = get_connection()
                identity = f"thread:{worker}"
                upsert_sensor(conn, identity, identity, "test")
                for value in range(20):
                    insert_reading(conn, identity, float(value), None)
                conn.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        conn = get_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 80)
        conn.close()

    def test_attic_alias_only_upgrade_is_foreign_key_safe(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            PRAGMA user_version=2;
            CREATE TABLE sensors (
                ieee_address TEXT PRIMARY KEY, friendly_name TEXT, model TEXT,
                zone TEXT, zone_override TEXT, name_source TEXT,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
            );
            CREATE TABLE readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ieee_address TEXT NOT NULL,
                timestamp TEXT NOT NULL, reading_date TEXT, reading_time TEXT,
                temperature_c REAL, humidity_pct REAL, battery_pct REAL,
                link_quality INTEGER, zone TEXT,
                FOREIGN KEY (ieee_address) REFERENCES sensors(ieee_address)
            );
            CREATE TABLE mqtt_packet_state (
                ieee_address TEXT PRIMARY KEY, packet_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO sensors VALUES (
                'fc:4d:6a:ff:fe:1d:1d:fb', 'Shelly Attic', 'SBHT-203C',
                'Zone 4', 'Zone 4', 'z2m',
                '2025-01-01T00:00:00', '2025-01-02T00:00:00'
            );
            INSERT INTO readings (
                ieee_address, timestamp, temperature_c, zone
            ) VALUES (
                'fc:4d:6a:ff:fe:1d:1d:fb',
                '2025-01-02T00:00:00', 18.5, 'Zone 4'
            );
            """
        )
        conn.close()
        upgraded = get_connection()
        self.assertEqual(
            upgraded.execute(
                "SELECT ieee_address FROM readings"
            ).fetchone()[0],
            "shelly:FC:4D:6A:1D:1D:FB",
        )
        self.assertIsNone(
            upgraded.execute(
                """
                SELECT 1 FROM sensors
                WHERE ieee_address='fc:4d:6a:ff:fe:1d:1d:fb'
                """
            ).fetchone()
        )
        upgraded.close()


class PacketAndMQTTTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        database._create_tables(self.conn)
        upsert_sensor(self.conn, OUTDOOR, "Outdoor", "Shelly Blu H&T", "Zone 4")
        self.conn.execute("DELETE FROM readings WHERE ieee_address=?", (OUTDOOR,))
        self.conn.execute("DELETE FROM mqtt_packet_state WHERE ieee_address=?", (OUTDOOR,))
        self.conn.execute("DELETE FROM sensor_source_state WHERE ieee_address=?", (OUTDOOR,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_packet_sequence_wrap_and_stale_rejection(self):
        self.assertTrue(insert_reading(self.conn, OUTDOOR, 10, 50, packet_id=255, source="esp32"))
        self.assertTrue(insert_reading(self.conn, OUTDOOR, 11, 50, packet_id=0, source="esp32"))
        self.assertFalse(insert_reading(self.conn, OUTDOOR, 9, 50, packet_id=250, source="esp32"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 2)

    def test_authoritative_source_with_ble_fallback_guard(self):
        self.assertTrue(
            insert_reading(
                self.conn, OUTDOOR, 10, 50, packet_id=1, source="esp32",
                authoritative_source="esp32",
            )
        )
        self.assertFalse(
            insert_reading(
                self.conn, OUTDOOR, 11, 50, packet_id=2, source="ble",
                authoritative_source="esp32",
            )
        )

    def test_mqtt_tls_requires_verified_certificates(self):
        class Client:
            def __init__(self):
                self.tls = None
                self.insecure = None

            def username_pw_set(self, username, password):
                self.credentials = (username, password)

            def tls_set(self, **kwargs):
                self.tls = kwargs

            def tls_insecure_set(self, value):
                self.insecure = value

        client = Client()
        MQTTSettings(
            host="broker", port=8883, transport="tcp", username="reader",
            password="secret", tls=True, ca_cert="/ca.pem",
        ).configure_client(client)
        self.assertEqual(client.credentials, ("reader", "secret"))
        self.assertEqual(client.tls["cert_reqs"], ssl.CERT_REQUIRED)
        self.assertEqual(client.insecure, False)

    def test_energy_values_are_not_scaled_by_magnitude(self):
        self.assertEqual(_extract_energy_kwh({"energy": 1234.5}), 1234.5)
        self.assertEqual(_extract_energy_kwh({"energy_kwh": 2001}), 2001.0)


class ReadOnlySurfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "sensor_data.db")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        database._create_tables(conn)
        identity = "00:12:4b:00:25:e7:a1:c3"
        upsert_sensor(
            conn, identity, "Utility", "SNZB-02D", "Zone config",
            name_source="z2m", zone_override="Zone MQTT",
        )
        insert_reading(conn, identity, 20, 50, zone="Zone old")
        timestamp = conn.execute(
            "SELECT timestamp FROM readings WHERE ieee_address=?", (identity,)
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO readings (
                ieee_address, timestamp, reading_date, reading_time,
                temperature_c, humidity_pct, zone
            ) VALUES (?, ?, substr(?,1,10), substr(?,12,8), 21, 51, 'Zone old')
            """,
            (identity, timestamp, timestamp, timestamp),
        )
        conn.commit()
        conn.close()
        self.original_web_path = web_server.DATABASE_PATH
        self.original_export_path = export.DATABASE_PATH
        web_server.DATABASE_PATH = self.db_path
        export.DATABASE_PATH = self.db_path
        self.client = app.test_client()

    def tearDown(self):
        web_server.DATABASE_PATH = self.original_web_path
        export.DATABASE_PATH = self.original_export_path
        self.temp_dir.cleanup()

    def test_latest_is_unique_and_effective_zone_is_consistent(self):
        sensors = self.client.get("/api/sensors").get_json()
        utility_sensor = [
            row for row in sensors if row["friendly_name"] == "Utility"
        ]
        self.assertEqual(utility_sensor[0]["zone"], "Zone MQTT")

        latest = self.client.get("/api/readings/latest").get_json()
        utility = [row for row in latest if row["friendly_name"] == "Utility"]
        self.assertEqual(len(utility), 1)
        self.assertEqual(utility[0]["temperature_c"], 21.0)
        self.assertEqual(utility[0]["zone"], "Zone MQTT")

        readings = self.client.get("/api/readings?zone=Zone%20MQTT").get_json()
        self.assertEqual(len(readings), 2)
        self.assertEqual({row["zone"] for row in readings}, {"Zone MQTT"})
        self.assertIn(b"Zone MQTT", self.client.get("/api/export/csv").data)

        output = os.path.join(self.temp_dir.name, "export.csv")
        with contextlib.redirect_stdout(io.StringIO()):
            export.export_to_csv(output)
        self.assertIn("Zone MQTT", Path(output).read_text(encoding="utf-8"))

    def test_stale_hive_runtime_is_capped(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        database._create_tables(conn)
        identity = "hive:test"
        upsert_sensor(conn, identity, "Hive Test", "Hive Thermostat")
        now = web_server.datetime.now()
        stale = now.replace(microsecond=0) - web_server.timedelta(hours=2)
        conn.execute(
            """
            INSERT INTO readings (
                ieee_address, timestamp, reading_date, reading_time, heating_on
            ) VALUES (?, ?, ?, ?, 1)
            """,
            (
                identity, stale.isoformat(), stale.date().isoformat(),
                stale.time().isoformat(),
            ),
        )
        conn.commit()
        runtime = _calculate_hive_runtime_seconds(conn, stale.date().isoformat())
        self.assertLessEqual(runtime[identity], 90)
        conn.close()

    def test_hive_cli_handles_heating_and_hotwater_shape(self):
        fake = {
            "heating": [{
                "name": "Hall", "temperature_c": 20.0,
                "target_temp_c": 21.0, "mode": "SCHEDULE",
            }],
            "hotwater": [{"name": "Water", "hw_on": True, "mode": "ON"}],
        }
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["zigbee_sensor_reader", "--hive"]),
            mock.patch.object(hive_reader, "HIVE_USERNAME", "configured"),
            mock.patch.object(hive_reader, "run_hive_poll", return_value=fake),
            contextlib.redirect_stdout(output),
        ):
            main()
        self.assertIn("Hall: 20.0°C", output.getvalue())
        self.assertIn("Water: ON", output.getvalue())


class Z2MZonePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "sensor_data.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        database._create_tables(self.conn)
        self.original_web_path = web_server.DATABASE_PATH
        self.original_export_path = export.DATABASE_PATH
        web_server.DATABASE_PATH = self.db_path
        export.DATABASE_PATH = self.db_path
        self.client = app.test_client()
        self.reader = Z2MReader(
            on_reading=lambda reading: handle_reading(reading, self.conn),
            get_conn_fn=lambda: self.conn,
        )

    def tearDown(self):
        self.conn.close()
        web_server.DATABASE_PATH = self.original_web_path
        export.DATABASE_PATH = self.original_export_path
        self.temp_dir.cleanup()

    @staticmethod
    def _devices(sensor_zone="Cinema Room", plug_zone="Kitchen"):
        return [
            {
                "ieee_address": "0x00124b0025e7a1c3",
                "friendly_name": "Cinema Sensor",
                "description": sensor_zone,
                "definition": {"model": "SNZB-02D"},
            },
            {
                "ieee_address": "0x00124b0025e7a1c4",
                "friendly_name": "Hive Plug",
                "description": plug_zone,
                "definition": {"model": "TS011F"},
            },
        ]

    def _publish_states_without_descriptions(self):
        self.reader.handle_message(
            "zigbee2mqtt/Cinema Sensor",
            '{"temperature":20.5,"humidity":51,"battery":89}',
        )
        self.reader.handle_message(
            "zigbee2mqtt/Hive Plug",
            '{"state":"ON","power":42.5,"energy":1234.5}',
        )

    def test_bridge_zones_survive_state_messages_and_all_surfaces(self):
        self.reader.handle_message(
            "zigbee2mqtt/bridge/devices",
            json.dumps(self._devices()),
        )
        self._publish_states_without_descriptions()

        zones = {
            row["friendly_name"]: row["zone_override"]
            for row in self.conn.execute(
                "SELECT friendly_name, zone_override FROM sensors"
            )
            if row["friendly_name"] in {"Cinema Sensor", "Hive Plug"}
        }
        self.assertEqual(
            zones,
            {"Cinema Sensor": "Cinema Room", "Hive Plug": "Kitchen"},
        )

        dashboard = self.client.get("/api/dashboard").get_json()
        self.assertEqual(dashboard["sonoff"][0]["zone"], "Cinema Room")
        self.assertEqual(dashboard["plugs"][0]["zone"], "Kitchen")

        sensors = {
            row["friendly_name"]: row["zone"]
            for row in self.client.get("/api/sensors").get_json()
        }
        self.assertEqual(sensors["Cinema Sensor"], "Cinema Room")
        self.assertEqual(sensors["Hive Plug"], "Kitchen")

        readings = {
            row["friendly_name"]: row["zone"]
            for row in self.client.get("/api/readings/latest").get_json()
        }
        self.assertEqual(readings["Cinema Sensor"], "Cinema Room")
        self.assertEqual(readings["Hive Plug"], "Kitchen")
        history_zones = {
            row["friendly_name"]: row["zone"]
            for row in self.client.get("/api/readings").get_json()
        }
        self.assertEqual(history_zones["Cinema Sensor"], "Cinema Room")
        self.assertEqual(history_zones["Hive Plug"], "Kitchen")
        health = {
            row["friendly_name"]: row["zone"]
            for row in self.client.get("/api/system").get_json()["sensor_status"]
        }
        self.assertEqual(health["Cinema Sensor"], "Cinema Room")
        self.assertEqual(health["Hive Plug"], "Kitchen")
        csv_payload = self.client.get("/api/export/csv").get_data(as_text=True)
        self.assertIn("Cinema Room", csv_payload)
        self.assertIn("Kitchen", csv_payload)

    def test_explicit_bridge_description_clear_removes_cached_zone(self):
        self.reader.handle_message(
            "zigbee2mqtt/bridge/devices",
            json.dumps(self._devices()),
        )
        self._publish_states_without_descriptions()
        self.reader.handle_message(
            "zigbee2mqtt/bridge/devices",
            json.dumps(self._devices(sensor_zone="", plug_zone="")),
        )
        self._publish_states_without_descriptions()

        zones = self.conn.execute(
            """
            SELECT friendly_name, zone_override FROM sensors
            WHERE friendly_name IN ('Cinema Sensor', 'Hive Plug')
            """
        ).fetchall()
        self.assertEqual({row["zone_override"] for row in zones}, {None})

        dashboard = self.client.get("/api/dashboard").get_json()
        self.assertIsNone(dashboard["sonoff"][0]["zone"])
        self.assertIsNone(dashboard["plugs"][0]["zone"])
        latest = {
            row["friendly_name"]: row["zone"]
            for row in self.client.get("/api/readings/latest").get_json()
        }
        self.assertIsNone(latest["Cinema Sensor"])
        self.assertIsNone(latest["Hive Plug"])
        sensors = {
            row["friendly_name"]: row["zone"]
            for row in self.client.get("/api/sensors").get_json()
        }
        self.assertIsNone(sensors["Cinema Sensor"])
        self.assertIsNone(sensors["Hive Plug"])


if __name__ == "__main__":
    unittest.main()
