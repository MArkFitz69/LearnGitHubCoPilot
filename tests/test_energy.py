from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from flask import render_template

from zigbee_sensor_reader import database, duracell_reader, energy_data, myenergi_reader, web_server
from zigbee_sensor_reader.duracell_diagnostic import ENERGY_BLOCKS, energy_values
from zigbee_sensor_reader.energy_dashboard import snapshot

UTC = timezone.utc
NOW = datetime(2026, 7, 7, 12, tzinfo=UTC)


def history_payload(day, missing=(), fields=None):
    start, end = energy_data.day_bounds(day)
    rows = []
    for index in range(int((end - start).total_seconds() / 3600)):
        if index in missing:
            continue
        hour = start + timedelta(hours=index)
        rows.append({
            "yr": hour.year, "mon": hour.month, "dom": hour.day, "hr": hour.hour,
            **(fields if fields is not None else {"h1d": 3600000}),
        })
    return {"U123": rows}


def inverter_registers():
    registers = {
        address + offset: 0 for address, count in ENERGY_BLOCKS for offset in range(count)
    }
    registers.update({
        0x2000: 71, 0x2006: 530, 0x200A: 3190,
        0x1010: 3054, 0x1014: 1949, 0x1013: 240, 0x1017: 230,
        0x1027: 0, 0x1028: 12500,
        0x1332: 0, 0x1333: 345,
        0x200B: 0, 0x200C: 850, 0x200F: 0, 0x2010: 400,
    })
    return registers


class EnergyStoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        database._create_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_schema_upgrade_is_repeatable_and_preserves_readings(self):
        database.upsert_sensor(self.conn, "unchanged", "Unchanged", "test")
        database.insert_reading(self.conn, "unchanged", 21, 50)
        original = tuple(self.conn.execute("SELECT * FROM readings").fetchone())
        for name in ("energy_samples", "energy_daily", "energy_health"):
            self.conn.execute(f"DROP TABLE {name}")
        self.conn.execute("PRAGMA user_version=5")
        self.conn.commit()
        database._create_tables(self.conn)
        database._create_tables(self.conn)
        self.assertEqual(original, tuple(self.conn.execute("SELECT * FROM readings").fetchone()))
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 6)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM energy_samples").fetchone()[0], 0)

    def test_identical_values_are_written_at_900_seconds(self):
        def store(at, value=71):
            return energy_data.store_sample(
                self.conn, "duracell", "inverter", {"battery_soc_pct": value},
                {"battery_soc_pct": "provisional"}, at,
            )
        self.assertTrue(store(NOW))
        self.assertFalse(store(NOW + timedelta(seconds=899), 70))
        self.assertTrue(store(NOW + timedelta(seconds=900)))
        self.assertFalse(store(NOW + timedelta(seconds=900)))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM energy_samples").fetchone()[0], 2)

    def test_naive_and_nonfinite_measurements_rejected(self):
        with self.assertRaises(ValueError):
            energy_data.stamp(datetime(2026, 1, 1))
        for value in (float("nan"), float("inf"), True, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                energy_data.store_sample(
                    self.conn, "x", "y", {"power": value}, {"power": "verified"}, NOW,
                )
        with self.assertRaises(ValueError):
            energy_data.store_sample(self.conn, "x", "y", {"power": 1}, {}, NOW)

    def test_daily_upserts_do_not_accumulate_and_reset_remains_flagged(self):
        def store(value):
            energy_data.store_daily(
                self.conn, "duracell", "inverter", NOW.date(), "solar_kwh",
                value, "verified", "partial", "counter", NOW, monotonic=True,
            )
        store(10)
        store(10)
        store(12)
        row = self.conn.execute("SELECT * FROM energy_daily").fetchone()
        self.assertEqual(row["value"], 12)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM energy_daily").fetchone()[0], 1)
        store(1)
        store(3)
        self.assertEqual(self.conn.execute("SELECT quality FROM energy_daily").fetchone()[0], "reset")

    def test_health_failure_preserves_last_success(self):
        energy_data.record_health(self.conn, "duracell", now=NOW)
        energy_data.record_health(self.conn, "duracell", "timeout", NOW + timedelta(minutes=1))
        row = self.conn.execute("SELECT * FROM energy_health").fetchone()
        self.assertEqual(row["last_success"], energy_data.stamp(NOW))
        self.assertEqual(row["error"], "timeout")

    def test_dashboard_hides_unverified_totals_but_shows_provisional_live(self):
        energy_data.store_sample(
            self.conn, "duracell", "inverter", {"battery_soc_pct": 71},
            {"battery_soc_pct": "provisional"}, NOW,
        )
        energy_data.store_daily(
            self.conn, "duracell", "inverter", NOW.date(), "solar_kwh",
            12.5, "provisional", "partial", "Unverified counter", NOW,
        )
        result = snapshot(self.conn, NOW.date(), NOW)
        self.assertEqual(result["live"][0]["value"], 71)
        self.assertEqual(result["live"][0]["quality"], "provisional")
        solar = next(item for item in result["daily"] if item["metric"] == "solar_kwh")
        self.assertIsNone(solar["value"])
        self.assertEqual(solar["quality"], "provisional")
        self.assertEqual(result["summary"][-1]["cells"][1]["value"], None)

    def test_stale_and_missing_samples_are_explicit(self):
        energy_data.store_sample(
            self.conn, "myenergi", "myenergi:grd", {"grid_power_w": 0},
            {"grid_power_w": "verified"}, NOW - timedelta(hours=1),
        )
        result = snapshot(self.conn, NOW.date(), NOW)
        self.assertTrue(result["live"][0]["stale"])
        self.assertEqual(result["live"][0]["value"], 0)
        self.assertGreater(len(result["missing_live"]), 0)
        self.assertTrue(all(item["value"] is None for item in result["daily"]))

    def test_separate_device_totals_not_double_counted(self):
        for kind, serial, value in (("eddi", "123", 2), ("zappi", "456", 7), ("zappi", "789", 3)):
            energy_data.store_daily(
                self.conn, "myenergi", f"myenergi:{kind}:{serial}", NOW.date(),
                "device_kwh", value, "verified", "complete", "history", NOW,
            )
        result = snapshot(self.conn, NOW.date(), NOW)
        self.assertEqual(result["summary"][-1]["cells"][-2]["value"], 2)
        self.assertEqual(result["summary"][-1]["cells"][-1]["value"], 10)

    def test_trend_gaps_are_not_joined(self):
        for minutes in (0, 15, 90):
            energy_data.store_sample(
                self.conn, "myenergi", "myenergi:grd", {"grid_power_w": 200},
                {"grid_power_w": "verified"}, NOW + timedelta(minutes=minutes),
            )
        result = snapshot(self.conn, NOW.date(), NOW + timedelta(hours=2))
        self.assertEqual(len(result["charts"][0]["segments"]), 2)

    def test_daily_graph_preserves_zero_missing_partial_and_unverified_values(self):
        for offset, value, quality, coverage in (
            (0, 4, "verified", "partial"), (1, 8, "verified", "complete"),
            (2, 0, "verified", "complete"), (3, 20, "provisional", "partial"),
        ):
            energy_data.store_daily(
                self.conn, "duracell", "inverter", NOW.date() - timedelta(days=offset),
                "grid_import_kwh", value, quality, coverage, "counter", NOW,
            )
        result = snapshot(self.conn, NOW.date(), NOW)
        self.assertEqual(len(result["daily_graph"]["series"]), 5)
        bars = result["daily_graph"]["series"][0]["bars"]
        self.assertEqual(len(bars), 7)
        self.assertEqual([bar["day"] for bar in bars], [row["day"] for row in result["summary"]])
        self.assertIsNone(bars[0]["value"])
        self.assertIsNone(bars[3]["value"])
        self.assertEqual(bars[4]["value"], 0)
        self.assertEqual(bars[4]["height"], 0)
        self.assertEqual(bars[5]["height"], 240)
        self.assertEqual(bars[6]["height"], 120)
        self.assertEqual(bars[6]["coverage"], "partial")
        with web_server.app.app_context():
            html = render_template("energy.html", **result)
        self.assertIn('stroke-dasharray="4 3"', html)
        self.assertIn("4.000 kWh, partial", html)
        self.assertIn("0.000 kWh, complete", html)
        self.assertIn("House consumption &mdash; unavailable", html)
        self.assertEqual(html.count('class="daily-energy-graph"'), 1)

    def test_daily_graph_all_missing_or_zero_has_safe_widths(self):
        energy_data.store_daily(
            self.conn, "duracell", "inverter", NOW.date(),
            "solar_kwh", 0, "verified", "complete", "counter", NOW,
        )
        result = snapshot(self.conn, NOW.date(), NOW)
        for series in result["daily_graph"]["series"]:
            self.assertTrue(all(bar["height"] == 0 for bar in series["bars"]))

    def test_daily_overlays_share_scale_without_inventing_house_consumption(self):
        for metric, value in (("grid_import_kwh", 4), ("solar_kwh", 8),
                              ("battery_charge_kwh", 100)):
            energy_data.store_daily(
                self.conn, "duracell", "inverter", NOW.date(),
                metric, value, "verified", "complete", "counter", NOW,
            )
        for kind, value in (("eddi", 2), ("zappi", 6)):
            energy_data.store_daily(
                self.conn, "myenergi", f"myenergi:{kind}:123", NOW.date(),
                "device_kwh", value, "verified", "partial", "history", NOW,
            )
        graph = snapshot(self.conn, NOW.date(), NOW)["daily_graph"]
        self.assertEqual(graph["maximum"], 8)
        self.assertEqual([series["bars"][-1]["height"] for series in graph["series"]],
                         [120, 240, 0, 60, 180])
        self.assertEqual(len({series["bars"][-1]["x"] for series in graph["series"]}), 1)
        self.assertFalse(graph["series"][2]["available"])
        self.assertTrue(all(bar["value"] is None for bar in graph["series"][2]["bars"]))

    def test_original_dashboard_embeds_energy_and_keeps_sensor_sections(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "dashboard.db"
            with sqlite3.connect(path) as destination:
                self.conn.backup(destination)
            destination.close()

            def connect():
                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                return conn

            with mock.patch.object(web_server, "get_db", side_effect=connect):
                client = web_server.app.test_client()
                for route in ("/", "/dashboard?date=2026-07-07"):
                    response = client.get(route)
                    self.assertEqual(response.status_code, 200)
                    html = response.get_data(as_text=True)
                    self.assertEqual(html.lower().count("<!doctype html>"), 1)
                    self.assertIn('id="home-energy"', html)
                    self.assertIn("Daily energy supply and consumption overlays", html)
                    self.assertIn("Sonoff Sensors", html)
                    self.assertIn('action="/dashboard#home-energy"', html)
                    self.assertNotIn("<iframe", html)
                self.assertEqual(client.get("/dashboard?date=invalid").status_code, 400)

    def test_old_database_without_energy_schema_has_empty_state(self):
        for name in ("energy_samples", "energy_daily", "energy_health"):
            self.conn.execute(f"DROP TABLE {name}")
        result = snapshot(self.conn, NOW.date(), NOW)
        self.assertFalse(result["ready"])
        self.assertEqual(result["live"], [])


class EnergyTimeTests(unittest.TestCase):
    def test_uk_days_have_dst_correct_utc_bounds(self):
        for day, hours in ((date(2026, 3, 29), 23), (date(2026, 10, 25), 25),
                           (date(2026, 7, 7), 24)):
            with self.subTest(day=day):
                start, end = energy_data.day_bounds(day)
                self.assertEqual((end - start).total_seconds() / 3600, hours)
        self.assertEqual(energy_data.day_bounds(date(2026, 7, 7))[0].hour, 23)

    def test_tariff_boundaries_fixed_in_utc(self):
        for hour, minute, cheap in ((0, 59, False), (1, 0, True), (7, 59, True), (8, 0, False)):
            now = NOW.replace(hour=hour, minute=minute)
            self.assertEqual(energy_data.tariff_context(now, NOW.date())["cheap_now"], cheap)
        summer = energy_data.tariff_context(NOW, NOW.date())["intervals"][0]["label"]
        winter_day = date(2026, 12, 1)
        winter = energy_data.tariff_context(NOW, winter_day)["intervals"][0]["label"]
        self.assertEqual(summer, "02:00 BST - 09:00 BST")
        self.assertEqual(winter, "01:00 GMT - 08:00 GMT")

    def test_tariff_is_seven_actual_hours_on_both_clock_changes(self):
        for day in (date(2026, 3, 29), date(2026, 10, 25)):
            intervals = energy_data.tariff_context(NOW, day)["intervals"]
            self.assertEqual(len(intervals), 1)
            self.assertEqual(
                (datetime.fromisoformat(intervals[0]["end"]) -
                 datetime.fromisoformat(intervals[0]["start"])).total_seconds(), 7 * 3600,
            )


class MyenergiHistoryTests(unittest.TestCase):
    def test_conversion_phases_boost_and_hour_deduplication(self):
        day = date(2026, 7, 6)
        payload = history_payload(day, fields={"h1d": 3600000, "h2d": 1800000, "h3b": 1800000})
        payload["U123"].append(dict(payload["U123"][0]))
        value, coverage, _ = energy_data.history_totals(payload, "123", "zappi", day, NOW)
        self.assertEqual(value, 48)
        self.assertEqual(coverage, "complete")

    def test_dst_history_totals_use_uk_day_not_utc_day(self):
        for day, hours in ((date(2026, 3, 29), 23), (date(2026, 10, 25), 25)):
            value, coverage, _ = energy_data.history_totals(
                history_payload(day), "123", "eddi", day, datetime(2027, 1, 1, tzinfo=UTC),
            )
            self.assertEqual(value, hours)
            self.assertEqual(coverage, "complete")

    def test_missing_hours_are_partial_and_missing_energy_is_not_zero(self):
        day = date(2026, 7, 6)
        value, coverage, _ = energy_data.history_totals(
            history_payload(day, missing=(3,)), "123", "eddi", day, NOW,
        )
        self.assertEqual(value, 23)
        self.assertEqual(coverage, "partial")
        for payload in ({}, {"U123": []}, history_payload(day, fields={"imp": 100})):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                energy_data.history_totals(payload, "123", "eddi", day, NOW)

    def test_conflicting_duplicates_and_invalid_energy_rejected(self):
        day = date(2026, 7, 6)
        payload = history_payload(day)
        payload["U123"].append({**payload["U123"][0], "h1d": 0})
        with self.assertRaises(ValueError):
            energy_data.history_totals(payload, "123", "eddi", day, NOW)
        for value in (-1, True, float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                energy_data.history_totals(
                    history_payload(day, fields={"h1d": value}), "123", "zappi", day, NOW,
                )

    def test_explicit_utc_request_covers_dst_day(self):
        with mock.patch.object(myenergi_reader, "_request_json", return_value={}) as fetch:
            myenergi_reader.fetch_device_history("zappi", "123", date(2026, 10, 25))
        fetch.assert_called_once_with("/cgi-jdayhour-Z123-2026-10-24-23-25")

    def test_director_hostname_is_validated(self):
        response = mock.Mock()
        response.headers = {"x_myenergi-asn": "attacker.invalid"}
        with mock.patch.object(myenergi_reader.requests, "get", return_value=response):
            with self.assertRaises(ValueError):
                myenergi_reader._request_json("/cgi-jstatus-*")


class EnergyCollectorAndWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "energy.db")
        self.db_patch = mock.patch.object(database, "DATABASE_PATH", self.path)
        self.web_patch = mock.patch.object(web_server, "DATABASE_PATH", self.path)
        self.db_patch.start()
        self.web_patch.start()
        database.get_connection().close()
        self.client = web_server.app.test_client()

    def tearDown(self):
        self.db_patch.stop()
        self.web_patch.stop()
        self.temp.cleanup()

    def test_extended_register_map(self):
        live, daily = energy_values(inverter_registers())
        self.assertEqual(live["mppt1_voltage_v"], 305.4)
        self.assertEqual(live["mppt2_voltage_v"], 194.9)
        self.assertEqual(live["pv_power_w"], 47)
        self.assertEqual(daily, {
            "solar_kwh": 12.5, "grid_import_kwh": 3.45,
            "battery_charge_kwh": 8.5, "battery_discharge_kwh": 4,
        })

    def test_extended_collection_only_uses_bounded_holding_reads(self):
        registers = inverter_registers()
        responses = []
        for address, count in ENERGY_BLOCKS:
            result = mock.Mock()
            result.isError.return_value = False
            result.registers = [registers[address + offset] for offset in range(count)]
            responses.append(result)
        with (
            mock.patch.object(duracell_reader, "DURACELL_HOST", "test-inverter"),
            mock.patch.object(duracell_reader, "ModbusTcpClient") as factory,
            mock.patch.object(duracell_reader.time, "sleep"),
        ):
            client = factory.return_value
            client.read_holding_registers.side_effect = responses
            live, _ = duracell_reader.fetch_snapshot()
            self.assertEqual(live["battery_soc_pct"], 71)
            self.assertEqual(client.read_holding_registers.call_args_list, [
                mock.call(address=address, count=count, device_id=duracell_reader.DURACELL_UNIT)
                for address, count in ENERGY_BLOCKS
            ])
            client.write_register.assert_not_called()
            client.write_registers.assert_not_called()
            client.close.assert_called_once()

    def test_verified_counters_require_uk_reset_timezone_confirmation(self):
        values = energy_values(inverter_registers())
        with (
            mock.patch.object(duracell_reader, "fetch_snapshot", return_value=values),
            mock.patch.object(duracell_reader, "utc_now", return_value=NOW),
            mock.patch.object(duracell_reader, "DURACELL_VERIFIED_METRICS", {"solar_kwh"}),
            mock.patch.object(duracell_reader, "DURACELL_COUNTER_DAY_TIMEZONE", ""),
        ):
            duracell_reader.collect_snapshot()
        conn = database.get_connection()
        self.assertEqual(conn.execute(
            "SELECT quality FROM energy_daily WHERE metric='solar_kwh'"
        ).fetchone()[0], "provisional")
        conn.close()

    def test_duracell_is_provisional_until_explicitly_verified(self):
        values = energy_values(inverter_registers())
        with (
            mock.patch.object(duracell_reader, "fetch_snapshot", return_value=values),
            mock.patch.object(duracell_reader, "utc_now", return_value=NOW),
            mock.patch.object(duracell_reader, "DURACELL_VERIFIED_METRICS", set()),
        ):
            duracell_reader.collect_snapshot()
            duracell_reader.collect_snapshot()
        conn = database.get_connection()
        self.assertEqual(conn.execute("SELECT count(*) FROM energy_samples").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT quality FROM energy_daily").fetchone()[0], "provisional")
        self.assertEqual(conn.execute("SELECT count(*) FROM readings").fetchone()[0], 0)
        conn.close()

    def test_duracell_failure_records_health_and_does_not_invent_sample(self):
        with mock.patch.object(duracell_reader, "fetch_snapshot", side_effect=TimeoutError("offline")):
            with self.assertRaises(TimeoutError):
                duracell_reader.collect_snapshot()
        conn = database.get_connection()
        self.assertEqual(conn.execute("SELECT count(*) FROM energy_samples").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT error FROM energy_health").fetchone()[0], "offline")
        conn.close()

    def test_history_empty_today_does_not_block_yesterday_or_repeat_poll(self):
        readings = [{"ieee_address": "myenergi:eddi:123"}]
        with (
            mock.patch.object(myenergi_reader, "utc_now", return_value=NOW),
            mock.patch.object(myenergi_reader, "fetch_device_history") as fetch,
        ):
            fetch.side_effect = [{}, history_payload(NOW.date() - timedelta(days=1))]
            myenergi_reader.collect_daily_history(readings)
            myenergi_reader.collect_daily_history(readings)
            self.assertEqual(fetch.call_count, 2)
        conn = database.get_connection()
        rows = conn.execute("SELECT * FROM energy_daily").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 24)
        conn.close()

    def test_routes_are_read_only_escaped_and_have_units(self):
        conn = database.get_connection()
        energy_data.store_sample(
            conn, "duracell", "inverter", {"mppt1_voltage_v": 305.4},
            {"mppt1_voltage_v": "provisional"},
        )
        energy_data.record_health(conn, "duracell", "<script>alert(1)</script>")
        conn.close()
        with (
            mock.patch.object(duracell_reader, "fetch_snapshot", side_effect=AssertionError("network")),
            mock.patch.object(myenergi_reader, "_request_json", side_effect=AssertionError("network")),
        ):
            response = self.client.get("/energy")
            api = self.client.get("/api/energy")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("305.4", text)
        self.assertIn("Provisional mapping", text)
        self.assertIn("MPPT 1 voltage", text)
        self.assertIn("kWh", text)
        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertEqual(api.status_code, 200)
        self.assertEqual(api.json["live"][0]["unit"], "V")

    def test_bad_date_returns_400(self):
        for path in ("/energy", "/api/energy"):
            for value in ("garbage", "2026-02-30", "2099-01-01", "20260707"):
                self.assertEqual(self.client.get(path, query_string={"date": value}).status_code, 400)

    def test_midnight_uk_snapshot_belongs_to_selected_local_day(self):
        conn = database.get_connection()
        at = datetime(2026, 7, 6, 23, 15, tzinfo=UTC)
        energy_data.store_sample(
            conn, "myenergi", "myenergi:gen", {"pv_power_w": 0},
            {"pv_power_w": "verified"}, at,
        )
        today = snapshot(conn, date(2026, 7, 7), NOW)
        yesterday = snapshot(conn, date(2026, 7, 6), NOW)
        conn.close()
        self.assertEqual(len(today["charts"]), 1)
        self.assertEqual(len(yesterday["charts"]), 0)


if __name__ == "__main__":
    unittest.main()
