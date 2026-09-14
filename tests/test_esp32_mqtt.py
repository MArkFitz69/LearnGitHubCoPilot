import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from zigbee_sensor_reader import web_server
from zigbee_sensor_reader.config import ESP32_SENSOR_NAMES, SHELLY_SENSORS, ZONES
from zigbee_sensor_reader.database import (
    _create_tables,
    _migrate_outdoor_shelly,
    insert_reading,
    upsert_sensor,
)
from zigbee_sensor_reader.esp32_mqtt_reader import (
    ESP32MQTTReader,
    normalise_mac,
    parse_numeric_temperature,
    sensor_identity,
)
from zigbee_sensor_reader.shelly_ble_reader import (
    BTHomePayloadError,
    decode_bthome_v2_hex,
)
from zigbee_sensor_reader.web_server import (
    _build_dashboard_snapshot,
    _build_probe_chart,
    _get_reading_type_counts,
    app,
)


class BTHomeDecoderTests(unittest.TestCase):
    def test_decodes_captured_payloads(self):
        first = decode_bthome_v2_hex("4400AD01602E39450601")
        self.assertEqual(first["packet_id"], 173)
        self.assertEqual(first["battery"], 96)
        self.assertEqual(first["humidity"], 57)
        self.assertAlmostEqual(first["temperature"], 26.2)

        second = decode_bthome_v2_hex("44 00 2A 01 64 2E 43 45 D4 00")
        self.assertEqual(second["packet_id"], 42)
        self.assertEqual(second["battery"], 100)
        self.assertEqual(second["humidity"], 67)
        self.assertAlmostEqual(second["temperature"], 21.2)

    def test_rejects_malformed_unsupported_and_encrypted_payloads(self):
        for payload in ("", "not-hex", "44 45 01", "45 00 01", "20 00 01"):
            with self.subTest(payload=payload):
                with self.assertRaises(BTHomePayloadError):
                    decode_bthome_v2_hex(payload)


class ESP32ReaderTests(unittest.TestCase):
    def setUp(self):
        self.readings = []
        self.reader = ESP32MQTTReader(
            on_reading=self.readings.append,
            topic_prefix="heating-esp",
        )

    def test_numeric_parsing(self):
        self.assertEqual(parse_numeric_temperature(" 21.6\n"), 21.6)
        for payload in ("", "unknown", "nan", "inf"):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    parse_numeric_temperature(payload)

    def test_subscribes_only_to_known_topics_with_json_preferred(self):
        self.assertIn(
            "heating-esp/sensor/outdoorh_t_json/state",
            self.reader.subscription_topics,
        )
        self.assertIn(
            "heating-esp/sensor/shelly_raw_payload/state",
            self.reader.subscription_topics,
        )
        self.assertNotIn("heating-esp/#", self.reader.subscription_topics)

    def test_topic_dispatch_and_name_identity_mapping(self):
        topics = {
            "boiler1_out": ("boiler1_out", "Boiler 1 Out", 21.6),
            "bolier_1_return": ("boiler1_return", "Boiler 1 Return", 22.2),
            "boiler2_out": ("boiler2_out", "Boiler 2 Out", 24.2),
            "boiler2_return": ("boiler2_return", "Boiler 2 Return", 21.0),
        }
        for topic_key, (canonical_key, name, value) in topics.items():
            self.reader.handle_message(
                f"heating-esp/sensor/{topic_key}/state",
                str(value),
            )
            reading = self.readings[-1]
            self.assertEqual(reading.ieee_address, sensor_identity(canonical_key))
            self.assertEqual(reading.friendly_name, name)
            self.assertEqual(reading.temperature_c, value)

        self.assertEqual(
            ESP32_SENSOR_NAMES["shelly_raw_payload"],
            "Outdoor",
        )
        self.assertEqual(SHELLY_SENSORS["94:B2:16:08:82:98"], "Outdoor")
        self.assertEqual(ZONES["94:B2:16:08:82:98"], "Zone 4")

    def test_ignores_status_discovery_debug_and_malformed_values(self):
        ignored = [
            ("heating-esp/status", "online"),
            ("heating-esp/debug", "21.0"),
            ("heating-esp/sensor/boiler1_out/config", "{}"),
            ("homeassistant/sensor/heating-esp/config", "{}"),
            ("heating-esp/sensor/boiler1_out/state", "bad"),
            ("heating-esp/sensor/shelly_raw_payload/state", "44 45 01"),
            ("heating-esp/sensor/outdoorh_t_json/state", "{bad json"),
            ("heating-esp/sensor/outdoorh_t_json/state", '{"mac":"bad","payload":"4400AD01602E39450601"}'),
            ("heating-esp/sensor/outdoorh_t_json/state", '{"mac":"94:B2:16:08:82:98"}'),
            ("heating-esp/sensor/outdoorh_t_json/state", '{"mac":"94:B2:16:08:82:98","payload":"not-hex"}'),
        ]
        for topic, payload in ignored:
            self.reader.handle_message(topic, payload)
        self.assertEqual(self.readings, [])

    def test_json_shelly_dispatch_mac_identity_and_cross_topic_deduplication(self):
        json_topic = "heating-esp/sensor/outdoorh_t_json/state"
        plain_topic = "heating-esp/sensor/shelly_raw_payload/state"
        json_payload = '{"mac":"94b216088298","payload":"44007E01642E4345D900"}'
        self.reader.handle_message(json_topic, json_payload)
        self.reader.handle_message(plain_topic, "44007E01642E4345D900")

        self.assertEqual(len(self.readings), 1)
        reading = self.readings[0]
        self.assertEqual(reading.ieee_address, "shelly:94:B2:16:08:82:98")
        self.assertEqual(reading.friendly_name, "Outdoor")
        self.assertEqual(reading.packet_id, 126)
        self.assertAlmostEqual(reading.temperature_c, 21.7)
        self.assertEqual(reading.humidity_pct, 67)
        self.assertEqual(reading.battery_pct, 100)
        self.assertEqual(reading.zone, "Zone 4")

    def test_plain_hex_topic_remains_supported(self):
        self.reader.handle_message(
            "heating-esp/sensor/shelly_raw_payload/state",
            "4400AD01602E39450601",
        )
        self.assertEqual(self.readings[0].ieee_address, "shelly:94:B2:16:08:82:98")

    def test_distinct_valid_packets_are_each_emitted(self):
        topic = "heating-esp/sensor/outdoorh_t_json/state"
        self.reader.handle_message(
            topic,
            '{"mac":"94:B2:16:08:82:98","payload":"44007E01642E4345D900"}',
        )
        self.reader.handle_message(
            topic,
            '{"mac":"94:B2:16:08:82:98","payload":"44007F01642E4345D900"}',
        )
        self.assertEqual([reading.packet_id for reading in self.readings], [126, 127])

    def test_mac_normalization_and_validation(self):
        self.assertEqual(normalise_mac("94b216088298"), "94:B2:16:08:82:98")
        self.assertEqual(normalise_mac("94:b2:16:08:82:98"), "94:B2:16:08:82:98")
        for value in ("", "94:B2:16:08:82", "GG:B2:16:08:82:98"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalise_mac(value)


class PersistenceAndDashboardTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _create_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def _add_reading(self, identity, name, model, temperature, humidity=None, battery=None):
        upsert_sensor(self.conn, identity, name, model)
        insert_reading(
            self.conn,
            identity,
            temperature,
            humidity,
            battery_pct=battery,
        )

    def test_packet_id_deduplication_persists_in_database(self):
        identity = "shelly:94:B2:16:08:82:98"
        upsert_sensor(self.conn, identity, "Outdoor", "Shelly Blu H&T", "Zone 4")
        self.assertTrue(
            insert_reading(
                self.conn,
                identity,
                26.2,
                57,
                battery_pct=96,
                packet_id=173,
            )
        )
        self.assertFalse(
            insert_reading(
                self.conn,
                identity,
                26.2,
                57,
                battery_pct=96,
                packet_id=173,
            )
        )
        count = self.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        self.assertEqual(count, 1)

    def test_legacy_outdoor_identity_and_name_are_migrated(self):
        self.conn.execute("DELETE FROM sensors WHERE ieee_address = ?", ("shelly:94:B2:16:08:82:98",))
        upsert_sensor(
            self.conn,
            "shelly:94:B2:16:08:82:98",
            "Shelly Attic",
            "SBHT-203C",
            "Zone 5",
        )
        upsert_sensor(
            self.conn,
            "shelly:esp32:outdoor_ht",
            "Outdoor H&T",
            "Shelly Blu H&T",
            "Zone 5",
        )
        insert_reading(self.conn, "shelly:esp32:outdoor_ht", 21.2, 67, packet_id=42)

        _migrate_outdoor_shelly(self.conn)

        rows = self.conn.execute(
            "SELECT ieee_address, friendly_name, model, zone FROM sensors WHERE ieee_address LIKE 'shelly:%'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ieee_address"], "shelly:94:B2:16:08:82:98")
        self.assertEqual(rows[0]["friendly_name"], "Outdoor")
        self.assertEqual(rows[0]["model"], "SBHT-203C")
        self.assertEqual(rows[0]["zone"], "Zone 4")
        reading = self.conn.execute("SELECT ieee_address FROM readings").fetchone()
        self.assertEqual(reading["ieee_address"], "shelly:94:B2:16:08:82:98")

    def test_probe_chart_has_96_latest_reading_buckets_and_gaps(self):
        now = datetime(2026, 9, 14, 21, 20)
        window_start = datetime(2026, 9, 13, 21, 30)
        identity = "esp32:boiler1_out"
        upsert_sensor(self.conn, identity, "Boiler 1 Out", "ESP32 Dallas Temperature")
        samples = [
            (window_start + timedelta(minutes=1), 20.0),
            (window_start + timedelta(minutes=14), 21.0),
            (window_start + timedelta(minutes=31), 22.0),
        ]
        for timestamp, temperature in samples:
            self.conn.execute(
                """
                INSERT INTO readings (
                    ieee_address, timestamp, reading_date, reading_time, temperature_c
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    identity,
                    timestamp.isoformat(),
                    timestamp.date().isoformat(),
                    timestamp.time().isoformat(timespec="seconds"),
                    temperature,
                ),
            )
        self.conn.commit()

        chart = _build_probe_chart(self.conn, now=now)

        self.assertEqual(chart["bucket_count"], 96)
        self.assertEqual(chart["bucket_minutes"], 15)
        self.assertEqual(len(chart["labels"]), 96)
        self.assertEqual(len(chart["series"]), 4)
        series = next(item for item in chart["series"] if item["ieee_address"] == identity)
        self.assertEqual(len(series["values"]), 96)
        self.assertEqual(series["values"][0], 21.0)
        self.assertIsNone(series["values"][1])
        self.assertEqual(series["values"][2], 22.0)
        self.assertEqual(sum(value is not None for value in series["values"]), 2)

    def test_dashboard_classification_and_system_counts(self):
        for key in (
            "boiler1_out",
            "boiler1_return",
            "boiler2_out",
            "boiler2_return",
        ):
            self._add_reading(
                sensor_identity(key),
                ESP32_SENSOR_NAMES[key],
                "ESP32 Dallas Temperature",
                21.0,
            )
        self._add_reading(
            "shelly:94:B2:16:08:82:98",
            "Shelly Attic",
            "SBHT-203C",
            21.2,
            humidity=67,
            battery=100,
        )
        self._add_reading(
            "aa:bb:cc:dd:ee:ff:00:11",
            "Living Room",
            "SNZB-02D",
            20.0,
        )

        with app.test_request_context("/api/dashboard"):
            snapshot = _build_dashboard_snapshot(self.conn)

        self.assertEqual(len(snapshot["esp32"]), 4)
        self.assertEqual(
            {row["friendly_name"] for row in snapshot["esp32"]},
            {
                "Boiler 1 Out",
                "Boiler 1 Return",
                "Boiler 2 Out",
                "Boiler 2 Return",
            },
        )
        self.assertEqual(len(snapshot["outdoor"]), 1)
        self.assertEqual(snapshot["outdoor"][0]["friendly_name"], "Outdoor")
        self.assertEqual(snapshot["outdoor"][0]["zone"], "Zone 4")
        self.assertEqual(len(snapshot["shelly"]), 0)
        self.assertEqual(len(snapshot["sonoff"]), 1)
        self.assertEqual(snapshot["probe_chart"]["bucket_count"], 96)

        counts = _get_reading_type_counts(self.conn)
        self.assertEqual(counts["esp32"], 4)
        self.assertEqual(counts["shelly"], 1)
        self.assertEqual(counts["zigbee"], 1)


class APISurfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "sensor_data.db")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        _create_tables(conn)
        upsert_sensor(
            conn,
            "esp32:boiler1_out",
            "Boiler 1 Out",
            "ESP32 Dallas Temperature",
        )
        insert_reading(conn, "esp32:boiler1_out", 21.6, None)
        conn.close()
        self.original_db_path = web_server.DATABASE_PATH
        web_server.DATABASE_PATH = self.db_path
        self.client = app.test_client()

    def tearDown(self):
        web_server.DATABASE_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_existing_api_export_and_dashboard_surfaces_include_esp32(self):
        sensors = self.client.get("/api/sensors").get_json()
        self.assertEqual(sensors[0]["ieee_address"], "esp32:boiler1_out")

        readings = self.client.get("/api/readings").get_json()
        self.assertEqual(readings[0]["friendly_name"], "Boiler 1 Out")
        self.assertEqual(readings[0]["temperature_c"], 21.6)
        self.assertIn(
            b"esp32:boiler1_out",
            self.client.get("/api/readings?format=csv").data,
        )

        latest = self.client.get("/api/readings/latest").get_json()
        self.assertEqual(latest[0]["ieee_address"], "esp32:boiler1_out")

        csv_response = self.client.get("/api/export/csv")
        self.assertIn(b"esp32:boiler1_out", csv_response.data)

        dashboard_json = self.client.get("/api/dashboard").get_json()
        self.assertEqual(dashboard_json["esp32"][0]["friendly_name"], "Boiler 1 Out")
        self.assertEqual(dashboard_json["probe_chart"]["bucket_count"], 96)
        self.assertEqual(len(dashboard_json["probe_chart"]["series"]), 4)

        dashboard_html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("ESP32 / Heating Probes", dashboard_html)
        self.assertIn("Boiler 1 Out", dashboard_html)
        self.assertIn("probeChart", dashboard_html)
        self.assertIn("Preceding 24 Hours", dashboard_html)
        self.assertIn("<h2>Outdoor</h2>", dashboard_html)


if __name__ == "__main__":
    unittest.main()
