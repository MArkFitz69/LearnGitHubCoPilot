"""
Web API server for remote access to sensor data.

Exposes the SQLite database via HTTP endpoints so Power BI, Excel,
or any other tool can pull data over the network.

Usage:
    python -m zigbee_sensor_reader --serve
    python -m zigbee_sensor_reader --serve --port 8080

Endpoints:
    GET /api/sensors         - List all sensors with metadata
    GET /api/readings        - Get readings (supports filters)
    GET /api/readings/latest - Get the most recent reading per sensor
    GET /api/export/csv      - Download all data as CSV
    GET /api/status          - System status and sensor counts

Query parameters for /api/readings:
    start    - Start datetime (ISO format, e.g. 2026-01-01)
    end      - End datetime (ISO format)
    sensor   - Filter by sensor ieee_address
    zone     - Filter by zone (e.g. "Zone 1")
    limit    - Max rows to return (default 10000)
    format   - "json" (default) or "csv"
"""

import csv
import io
import hmac
import logging
import math
import os
import socket
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request

from .config import DATABASE_PATH, SHELLY_SENSORS, ZIGBEE_HOST, ZIGBEE_PORT
from .database import get_connection
from .onboarding import (
    create_temp_passcode,
    get_onboarding_state,
    is_valid_temp_passcode,
    now_iso,
    queue_start_pairing,
    save_sensor_metadata,
    set_tcp_check_state,
)

logger = logging.getLogger(__name__)

app = Flask(__name__)

POWER_BI_PATH = "/api/readings?format=csv"
DOWNLOAD_PATH = "/api/export/csv"


def get_db() -> sqlite3.Connection:
    """Get a read-only database connection."""
    conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_db_write() -> sqlite3.Connection:
    """Get a read-write connection and ensure latest schema exists."""
    return get_connection()


def _check_dongle_tcp() -> tuple[bool, str]:
    try:
        with socket.create_connection((ZIGBEE_HOST, ZIGBEE_PORT), timeout=3):
            return True, "connected"
    except OSError as exc:
        return False, str(exc)


def _is_admin_passcode(passcode: str) -> bool:
    configured = os.environ.get("ONBOARDING_PASSCODE")
    if not configured:
        return False
    return hmac.compare_digest(passcode, configured)


def _is_valid_onboarding_passcode(conn: sqlite3.Connection, passcode: str) -> bool:
    return _is_admin_passcode(passcode) or is_valid_temp_passcode(conn, passcode)


def _onboarding_auth_result(conn: sqlite3.Connection, passcode: str) -> dict:
    if not passcode:
        return {"ok": False, "error": "Passcode is required."}
    if _is_valid_onboarding_passcode(conn, passcode):
        return {"ok": True, "is_admin": _is_admin_passcode(passcode)}
    return {"ok": False, "error": "Invalid or expired passcode."}


def _parse_iso_timestamp(value: str) -> datetime:
    """Parse timestamps stored in SQLite."""
    return datetime.fromisoformat(value)


def _format_duration_hhmm(seconds: float) -> str:
    """Format duration as HH:MM."""
    total_seconds = max(int(seconds), 0)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{hours:02d}:{minutes:02d}"


def _zone_sort_key(zone: str | None) -> tuple[int, str]:
    """Sort zones naturally (Zone 1, Zone 2, ...)."""
    if not zone:
        return (999, "")
    if zone.lower().startswith("zone "):
        suffix = zone[5:].strip()
        if suffix.isdigit():
            return (int(suffix), zone)
    return (999, zone)


def _app_base_url() -> str:
    host = request.host or "localhost:8080"
    return f"{request.scheme}://{host}"


def _page_links() -> dict[str, str]:
    base_url = _app_base_url()
    return {
        "base_url": base_url,
        "power_bi_url": f"{base_url}{POWER_BI_PATH}",
        "download_url": f"{base_url}{DOWNLOAD_PATH}",
    }


def _is_plug_row(ieee_address: str, model: str | None, friendly_name: str | None) -> bool:
    """Heuristic to classify smart plugs into their own dashboard section."""
    model_l = (model or "").lower()
    name_l = (friendly_name or "").lower()
    if model_l.startswith("ts011f"):
        return True
    if "plug" in model_l or "socket" in model_l:
        return True
    if "plug" in name_l or "socket" in name_l:
        return True
    return False


def _get_system_info() -> dict:
    """
    Gather Pi hardware stats, service status, and database metrics.
    All values are best-effort â€” failures return None rather than raising.
    """
    info = {}

    # â”€â”€ Uptime â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    try:
        uptime_secs = float(Path("/proc/uptime").read_text().split()[0])
        days, remainder = divmod(int(uptime_secs), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        info["uptime"] = f"{days}d {hours:02d}:{minutes:02d}"
        info["uptime_seconds"] = uptime_secs
    except Exception:
        info["uptime"] = None

    # â”€â”€ CPU temperature (Raspberry Pi specific) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        info["cpu_temp_c"] = round(int(raw) / 1000, 1)
    except Exception:
        info["cpu_temp_c"] = None

    # â”€â”€ CPU usage % (psutil preferred, /proc/stat fallback) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    try:
        import psutil
        info["cpu_pct"] = psutil.cpu_percent(interval=0.5)
    except ImportError:
        try:
            import time

            def _read_cpu_times():
                line = Path("/proc/stat").read_text().splitlines()[0]
                vals = list(map(int, line.split()[1:]))
                idle = vals[3]
                total = sum(vals)
                return idle, total

            idle1, total1 = _read_cpu_times()
            import time as _time
            _time.sleep(0.25)
            idle2, total2 = _read_cpu_times()
            delta_total = total2 - total1
            delta_idle = idle2 - idle1
            info["cpu_pct"] = round((1 - delta_idle / delta_total) * 100, 1) if delta_total else None
        except Exception:
            info["cpu_pct"] = None
    except Exception:
        info["cpu_pct"] = None

    # â”€â”€ RAM â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    try:
        import psutil
        vm = psutil.virtual_memory()
        info["ram_total_mb"] = round(vm.total / 1024 / 1024, 1)
        info["ram_used_mb"] = round(vm.used / 1024 / 1024, 1)
        info["ram_available_mb"] = round(vm.available / 1024 / 1024, 1)
        info["ram_pct"] = vm.percent
    except ImportError:
        try:
            mem = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, val = line.split(":", 1)
                mem[key.strip()] = int(val.strip().split()[0])  # kB
            total_mb = round(mem["MemTotal"] / 1024, 1)
            avail_mb = round(mem.get("MemAvailable", mem.get("MemFree", 0)) / 1024, 1)
            used_mb = round(total_mb - avail_mb, 1)
            info["ram_total_mb"] = total_mb
            info["ram_used_mb"] = used_mb
            info["ram_available_mb"] = avail_mb
            info["ram_pct"] = round(used_mb / total_mb * 100, 1) if total_mb else None
        except Exception:
            info["ram_total_mb"] = info["ram_used_mb"] = info["ram_available_mb"] = info["ram_pct"] = None
    except Exception:
        info["ram_total_mb"] = info["ram_used_mb"] = info["ram_available_mb"] = info["ram_pct"] = None

    # â”€â”€ Disk (partition where DB lives) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    try:
        import shutil
        db_dir = str(Path(DATABASE_PATH).resolve().parent)
        usage = shutil.disk_usage(db_dir)
        info["disk_total_gb"] = round(usage.total / 1024 ** 3, 2)
        info["disk_used_gb"] = round(usage.used / 1024 ** 3, 2)
        info["disk_free_gb"] = round(usage.free / 1024 ** 3, 2)
        info["disk_pct"] = round(usage.used / usage.total * 100, 1)
    except Exception:
        info["disk_total_gb"] = info["disk_used_gb"] = info["disk_free_gb"] = info["disk_pct"] = None

    # â”€â”€ Database file â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    try:
        db_path = Path(DATABASE_PATH).resolve()
        db_size = db_path.stat().st_size if db_path.exists() else 0
        info["db_size_mb"] = round(db_size / 1024 / 1024, 2)
    except Exception:
        info["db_size_mb"] = None

    # â”€â”€ Database metrics â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    try:
        conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        today = datetime.now().strftime("%Y-%m-%d")

        info["db_total_readings"] = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        info["db_today_readings"] = conn.execute(
            "SELECT COUNT(*) FROM readings WHERE substr(timestamp,1,10)=?", (today,)
        ).fetchone()[0]

        ts_range = conn.execute(
            "SELECT MIN(timestamp) as first, MAX(timestamp) as last FROM readings"
        ).fetchone()
        info["db_first_reading"] = ts_range["first"]
        info["db_last_reading"] = ts_range["last"]

        type_counts = conn.execute("""
            SELECT
                SUM(CASE WHEN ieee_address LIKE 'hive-hw:%' THEN 1 ELSE 0 END) as hotwater,
                SUM(CASE WHEN ieee_address LIKE 'hive:%' AND ieee_address NOT LIKE 'hive-hw:%' THEN 1 ELSE 0 END) as hive,
                SUM(CASE WHEN ieee_address LIKE 'shelly:%' THEN 1 ELSE 0 END) as shelly,
                SUM(CASE WHEN ieee_address NOT LIKE 'hive%' AND ieee_address NOT LIKE 'shelly:%' THEN 1 ELSE 0 END) as zigbee
            FROM readings
        """).fetchone()
        info["db_counts"] = dict(type_counts)

        # Per-sensor last seen and stale flag
        sensor_status = conn.execute("""
            SELECT s.friendly_name, s.model, COALESCE(s.zone_override, s.zone) AS zone, r.last_ts,
                   CAST((julianday('now','localtime') - julianday(r.last_ts)) * 1440 AS INTEGER) AS mins_ago
            FROM sensors s
            LEFT JOIN (
                SELECT ieee_address, MAX(timestamp) as last_ts
                FROM readings GROUP BY ieee_address
            ) r ON s.ieee_address = r.ieee_address
            ORDER BY COALESCE(s.zone_override, s.zone), s.friendly_name
        """).fetchall()
        info["sensor_status"] = [dict(row) for row in sensor_status]

        conn.close()
    except Exception as e:
        logger.warning("DB metrics error: %s", e)

    # â”€â”€ Systemd service status â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    services = ["zigbee-sensor-reader", "sensor-data-api"]
    info["services"] = {}
    for svc in services:
        try:
            result = subprocess.run(
                ["systemctl", "show", svc,
                 "--property=ActiveState,SubState,ExecMainPID,ActiveEnterTimestamp,MemoryCurrent"],
                capture_output=True, text=True, timeout=5
            )
            props = {}
            for line in result.stdout.strip().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k] = v
            active = props.get("ActiveState", "unknown")
            sub = props.get("SubState", "")
            mem_bytes = None
            try:
                mem_val = int(props.get("MemoryCurrent", "18446744073709551615"))
                if mem_val < 2 ** 63:
                    mem_bytes = mem_val
            except ValueError:
                pass
            since_raw = props.get("ActiveEnterTimestamp", "")
            info["services"][svc] = {
                "active_state": active,
                "sub_state": sub,
                "pid": props.get("ExecMainPID"),
                "memory_mb": round(mem_bytes / 1024 / 1024, 1) if mem_bytes else None,
                "since": since_raw.replace("n/a", "").strip() or None,
                "status_class": "ok" if active == "active" else ("warn" if active == "activating" else "err"),
            }
        except FileNotFoundError:
            info["services"][svc] = {"active_state": "systemctl not available", "status_class": "warn"}
        except Exception as e:
            info["services"][svc] = {"active_state": f"error: {e}", "status_class": "err"}

    info["generated_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    return info



def _calculate_hive_runtime_seconds(conn: sqlite3.Connection, day: str) -> dict:
    """
    Calculate today's runtime in seconds for each Hive thermostat.

    Runtime is computed from sampled heating_on states by summing intervals where
    heating_on=1 from each sample time to the next sample time.
    """
    rows = conn.execute(
        """
        SELECT ieee_address, timestamp, heating_on
        FROM readings
        WHERE (ieee_address LIKE 'hive:%' OR ieee_address LIKE 'hive-hw:%')
          AND substr(timestamp, 1, 10) = ?
        ORDER BY ieee_address, timestamp
        """,
        (day,),
    ).fetchall()

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["ieee_address"], []).append(row)

    now = datetime.now()
    runtimes: dict[str, float] = {}
    for ieee_address, samples in grouped.items():
        total_seconds = 0.0
        for index, sample in enumerate(samples):
            if sample["heating_on"] != 1:
                continue

            current_ts = _parse_iso_timestamp(sample["timestamp"])
            if index + 1 < len(samples):
                next_ts = _parse_iso_timestamp(samples[index + 1]["timestamp"])
            else:
                next_ts = now

            if next_ts > current_ts:
                total_seconds += (next_ts - current_ts).total_seconds()

        runtimes[ieee_address] = total_seconds

    return runtimes


def _build_dashboard_snapshot(conn: sqlite3.Connection) -> dict:
    """Build a dashboard snapshot for API and HTML rendering."""
    latest_rows = conn.execute(
        """
        SELECT r.ieee_address, s.friendly_name, s.model, r.timestamp, r.reading_date, r.reading_time,
               r.temperature_c, r.humidity_pct, r.battery_pct,
               COALESCE(s.zone_override, s.zone, r.zone) AS zone,
               r.state, r.power_w, r.energy_kwh, r.link_quality,
               r.heating_on, r.boost_on, r.target_temp_c, r.heating_mode,
               r.device_min_temp_c, r.device_max_temp_c,
               r.device_min_humidity_pct, r.device_max_humidity_pct,
               r.battery_voltage_mv, r.rssi,
               s.zone_override, s.name_source
        FROM readings r
        INNER JOIN (
            SELECT ieee_address, MAX(id) AS max_id
            FROM readings
            GROUP BY ieee_address
        ) latest ON r.id = latest.max_id
        LEFT JOIN sensors s ON r.ieee_address = s.ieee_address
        ORDER BY s.friendly_name
        """
    ).fetchall()

    today_local = datetime.now().strftime("%Y-%m-%d")
    daily_rows = conn.execute(
        """
        SELECT ieee_address,
               MIN(temperature_c) AS min_temp_c,
               MAX(temperature_c) AS max_temp_c,
               MIN(humidity_pct) AS min_humidity_pct,
               MAX(humidity_pct) AS max_humidity_pct
        FROM readings
        WHERE substr(timestamp, 1, 10) = ?
        GROUP BY ieee_address
        """,
        (today_local,),
    ).fetchall()
    daily_by_sensor = {row["ieee_address"]: dict(row) for row in daily_rows}
    hive_runtime_seconds = _calculate_hive_runtime_seconds(conn, today_local)

    sonoff = []
    shelly = []
    hive = []
    hotwater = []
    plugs = []

    for row in latest_rows:
        ieee_address = row["ieee_address"]
        model = row["model"] or ""
        latest = dict(row)
        if ieee_address.startswith("shelly:"):
            mac = ieee_address.split("shelly:", 1)[1].upper()
            configured_name = SHELLY_SENSORS.get(mac)
            if configured_name:
                latest["friendly_name"] = configured_name
        daily = daily_by_sensor.get(ieee_address, {})

        if ieee_address.startswith("hive-hw:"):
            # Hive hot water â€” state/mode only, no temperature
            hw_on = row["heating_on"] == 1
            boost_on = row["boost_on"] == 1
            if boost_on:
                status = "boost"
            elif hw_on:
                status = "on"
            else:
                status = "off"
            hotwater.append({
                **latest,
                "status": status,
                "runtime_today_seconds": hive_runtime_seconds.get(ieee_address, 0.0),
                "runtime_today_hhmm": _format_duration_hhmm(
                    hive_runtime_seconds.get(ieee_address, 0.0)
                ),
            })
            continue

        if ieee_address.startswith("hive:"):
            heating_on = row["heating_on"] == 1
            boost_on = row["boost_on"] == 1
            if boost_on:
                status = "boost"
            elif heating_on:
                status = "on"
            else:
                status = "off"

            hive.append(
                {
                    **latest,
                    "status": status,
                    "runtime_today_seconds": hive_runtime_seconds.get(ieee_address, 0.0),
                    "runtime_today_hhmm": _format_duration_hhmm(
                        hive_runtime_seconds.get(ieee_address, 0.0)
                    ),
                }
            )
            continue

        sensor_row = {
            **latest,
            # DB-computed daily min/max (from all readings today)
            "min_temp_c": daily.get("min_temp_c"),
            "max_temp_c": daily.get("max_temp_c"),
            "min_humidity_pct": daily.get("min_humidity_pct"),
            "max_humidity_pct": daily.get("max_humidity_pct"),
            # Device-reported daily min/max (from ZCL attributes 0x0001/0x0002)
            "device_min_temp_c": latest.get("device_min_temp_c"),
            "device_max_temp_c": latest.get("device_max_temp_c"),
        }

        if _is_plug_row(ieee_address, model, latest.get("friendly_name")):
            plugs.append(sensor_row)
        elif model.startswith("SNZB-02"):
            sonoff.append(sensor_row)
        elif ieee_address.startswith("shelly:") or model == "Shelly Blu H&T":
            shelly.append(sensor_row)
        else:
            # Any other Zigbee sensor (unknown model, other Sonoff models, etc.)
            sonoff.append(sensor_row)

    sonoff.sort(key=lambda row: (_zone_sort_key(row.get("zone")), (row.get("friendly_name") or row.get("ieee_address") or "").lower()))
    hive.sort(key=lambda row: (_zone_sort_key(row.get("zone")), (row.get("friendly_name") or row.get("ieee_address") or "").lower()))
    hotwater.sort(key=lambda row: (row.get("friendly_name") or row.get("ieee_address")))
    shelly.sort(key=lambda row: (_zone_sort_key(row.get("zone")), (row.get("friendly_name") or row.get("ieee_address") or "").lower()))
    plugs.sort(key=lambda row: (_zone_sort_key(row.get("zone")), (row.get("friendly_name") or row.get("ieee_address") or "").lower()))

    return {
        "date_local": today_local,
        "generated_at_local": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        # Backward-compatible aliases used by earlier dashboard/API responses.
        "date_utc": today_local,
        "generated_at_utc": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "sonoff": sonoff,
        "shelly": shelly,
        "hive": hive,
        "hotwater": hotwater,
        "plugs": plugs,
        **_page_links(),
    }


@app.route("/api/status")
def api_status():
    """System status overview (lightweight)."""
    conn = get_db()
    sensor_count = conn.execute("SELECT COUNT(*) FROM sensors").fetchone()[0]
    reading_count = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    latest = conn.execute(
        "SELECT MAX(timestamp) as ts FROM readings"
    ).fetchone()["ts"]
    conn.close()

    return jsonify({
        "status": "running",
        "sensors": sensor_count,
        "total_readings": reading_count,
        "latest_reading": latest,
        "server_time": datetime.now().isoformat(),
    })


@app.route("/api/sensors/<path:ieee_address>/zone", methods=["PATCH"])
def api_set_zone(ieee_address: str):
    """Set or clear a zone override for a sensor.

    Body (JSON): {"zone": "Zone 1"}  — set a zone
                 {"zone": null}       — clear the override (revert to config.py)
    """
    from .database import set_sensor_zone_override
    data = request.get_json(silent=True) or {}
    zone_value = data.get("zone")  # None means clear
    conn = get_db_write()
    try:
        set_sensor_zone_override(conn, ieee_address, zone_value or None)
        return jsonify({"ok": True, "ieee_address": ieee_address, "zone_override": zone_value})
    except Exception as exc:
        logger.warning("Zone update failed for %s: %s", ieee_address, exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/system")
def api_system():
    """Full system and application status as JSON."""
    return jsonify(_get_system_info())


@app.route("/system")
def system_page():
    """Rich HTML system status page: Pi hardware, services, DB, sensors."""
    info = _get_system_info()

    def bar_cls(pct):
        if pct is None:
            return ""
        return "bar-err" if pct >= 90 else ("bar-warn" if pct >= 75 else "bar-ok")

    def txt_cls(pct):
        if pct is None:
            return ""
        return "err" if pct >= 90 else ("warn" if pct >= 75 else "ok")

    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>System Status</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; color: #222; background: #fafafa; }
    h1 { margin-bottom: 4px; }
    h2 { margin: 28px 0 8px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
    .meta { color: #666; margin-bottom: 20px; font-size: .9em; }
    .linkbar { background: #fff; border: 1px solid #ddd; border-radius: 6px; padding: 12px 14px; margin-bottom: 18px; }
    .pill { display: inline-block; margin-right: 12px; margin-bottom: 6px; }
    .endpoint { font-family: Consolas, monospace; background: #f4f4f4; padding: 2px 6px; border-radius: 4px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px,1fr)); gap: 12px; }
    .card { background: #fff; border: 1px solid #ddd; border-radius: 6px; padding: 14px 16px; }
    .card h3 { margin: 0 0 4px; font-size: .8em; color: #777; text-transform: uppercase; letter-spacing: .05em; }
    .card .val { font-size: 1.4em; font-weight: bold; }
    .card .sub { font-size: .82em; color: #666; margin-top: 2px; }
    .bar-bg { background: #eee; border-radius: 4px; height: 8px; margin-top: 8px; }
    .bar-fill { height: 8px; border-radius: 4px; }
    .bar-ok { background: #27ae60; } .bar-warn { background: #e67e22; } .bar-err { background: #c0392b; }
    table { border-collapse: collapse; width: 100%; margin-top: 10px; background: #fff; }
    th, td { border: 1px solid #ddd; padding: 7px 10px; text-align: left; font-size: .88em; }
    th { background: #f4f4f4; }
    .ok { color: #27ae60; font-weight: bold; }
    .warn { color: #e67e22; font-weight: bold; }
    .err { color: #c0392b; font-weight: bold; }
    tr.stale { background: #fff8e1; } tr.down { background: #ffeaea; }
  </style>
</head>
<body>
<h1>&#128202; System Status</h1>
<div class="meta">{{ gen }} &mdash; Auto-refresh 30s &nbsp;|&nbsp; <a href="/dashboard">&#127968; Dashboard</a> &nbsp;|&nbsp; <a href="/onboarding">&#128268; Onboarding</a></div>

<div class="linkbar">
  <div class="pill"><a href="{{ download_url }}">&#128229; Download CSV</a></div>
  <div class="pill"><a href="/api/readings/latest?format=csv">&#128196; Latest readings CSV</a></div>
  <div class="pill"><strong>Power BI URL:</strong> <span class="endpoint">{{ power_bi_url }}</span></div>
</div>

<h2>&#129303; Pi Hardware</h2>
<div class="grid">
  <div class="card"><h3>Uptime</h3><div class="val">{{ uptime or "N/A" }}</div></div>
  <div class="card">
    <h3>CPU Usage</h3>
    <div class="val {{ cpu_txt }}">{{ cpu_str }}</div>
    {% if cpu_temp is not none %}<div class="sub">Temp: {{ cpu_temp }}&deg;C</div>{% endif %}
    {% if cpu_pct is not none %}
    <div class="bar-bg"><div class="bar-fill {{ cpu_bar }}" style="width:{{ [cpu_pct,100]|min }}%"></div></div>
    {% endif %}
  </div>
  <div class="card">
    <h3>RAM</h3>
    {% if ram_pct is not none %}
    <div class="val {{ ram_txt }}">{{ ram_used|int }} / {{ ram_total|int }} MB</div>
    <div class="sub">{{ ram_pct }}% used &mdash; {{ ram_avail|int }} MB free</div>
    <div class="bar-bg"><div class="bar-fill {{ ram_bar }}" style="width:{{ [ram_pct,100]|min }}%"></div></div>
    {% else %}<div class="val">N/A</div>{% endif %}
  </div>
  <div class="card">
    <h3>Storage (DB partition)</h3>
    {% if disk_pct is not none %}
    <div class="val {{ disk_txt }}">{{ disk_used }} / {{ disk_total }} GB</div>
    <div class="sub">{{ disk_pct }}% used &mdash; {{ disk_free }} GB free</div>
    <div class="bar-bg"><div class="bar-fill {{ disk_bar }}" style="width:{{ [disk_pct,100]|min }}%"></div></div>
    {% else %}<div class="val">N/A</div>{% endif %}
  </div>
</div>

<h2>&#9881;&#65039; Services</h2>
<table>
  <thead><tr><th>Service</th><th>State</th><th>Sub-state</th><th>PID</th><th>Memory</th><th>Active since</th></tr></thead>
  <tbody>
  {% for svc_name, svc in services.items() %}
  <tr class="{{ 'down' if svc.status_class == 'err' else '' }}">
    <td><code>{{ svc_name }}</code></td>
    <td class="{{ svc.status_class }}">
      {% if svc.status_class == 'ok' %}&#10003;{% elif svc.status_class == 'warn' %}&#9888;{% else %}&#10007;{% endif %}
      {{ svc.active_state }}
    </td>
    <td>{{ svc.sub_state or "-" }}</td>
    <td>{{ svc.pid or "-" }}</td>
    <td>{{ (svc.memory_mb|string ~ " MB") if svc.memory_mb is not none else "-" }}</td>
    <td style="font-size:.82em;color:#555">{{ svc.since or "-" }}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>

<h2>&#128190; Database</h2>
<div class="grid">
  <div class="card"><h3>File Size</h3><div class="val">{{ db_size }} MB</div></div>
  <div class="card">
    <h3>Total Readings</h3>
    <div class="val">{{ db_total }}</div>
    <div class="sub">{{ db_today }} today</div>
  </div>
  <div class="card">
    <h3>Date Range</h3>
    <div class="val" style="font-size:1em">{{ db_first }}</div>
    <div class="sub">to {{ db_last }}</div>
  </div>
  <div class="card">
    <h3>Breakdown</h3>
    <div class="sub">Zigbee: {{ cnt_zigbee }}</div>
    <div class="sub">Hive heating: {{ cnt_hive }}</div>
    <div class="sub">Hive hot water: {{ cnt_hw }}</div>
    <div class="sub">Shelly BLE: {{ cnt_shelly }}</div>
  </div>
</div>

<h2>&#128268; Sensor Health</h2>
<table>
  <thead><tr><th>Sensor</th><th>Model</th><th>Zone</th><th>Last seen</th><th>Age</th><th>Status</th></tr></thead>
  <tbody>
  {% for s in sensors %}
  {% set m = s.mins_ago %}
  <tr class="{{ 'stale' if m is not none and m > 60 else '' }}">
    <td>{{ s.friendly_name or "-" }}</td>
    <td style="color:#777;font-size:.82em">{{ s.model or "-" }}</td>
    <td>{{ s.zone or "-" }}</td>
    <td style="font-size:.85em">{{ s.last_ts or "never" }}</td>
    <td>{% if m is not none %}{% if m < 60 %}{{ m }}m{% elif m < 1440 %}{{ m // 60 }}h {{ m % 60 }}m{% else %}{{ m // 1440 }}d {{ (m % 1440) // 60 }}h{% endif %}{% else %}-{% endif %}</td>
    <td>{% if m is none %}<span class="warn">no data</span>{% elif m > 120 %}<span class="err">&#9888; stale</span>{% elif m > 60 %}<span class="warn">&#9888; slow</span>{% else %}<span class="ok">&#10003; ok</span>{% endif %}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
</body></html>"""

    c = info.get("db_counts") or {}
    fmt = lambda n: f"{n:,}" if isinstance(n, int) else "0"
    return render_template_string(html,
        gen=info["generated_at"],
        uptime=info.get("uptime"),
        cpu_pct=info.get("cpu_pct"),
        cpu_str=f"{info['cpu_pct']:.1f}%" if info.get("cpu_pct") is not None else "N/A",
        cpu_txt=txt_cls(info.get("cpu_pct")),
        cpu_bar=bar_cls(info.get("cpu_pct")),
        cpu_temp=info.get("cpu_temp_c"),
        ram_pct=info.get("ram_pct"),
        ram_used=info.get("ram_used_mb", 0),
        ram_total=info.get("ram_total_mb", 0),
        ram_avail=info.get("ram_available_mb", 0),
        ram_txt=txt_cls(info.get("ram_pct")),
        ram_bar=bar_cls(info.get("ram_pct")),
        disk_pct=info.get("disk_pct"),
        disk_used=info.get("disk_used_gb"),
        disk_total=info.get("disk_total_gb"),
        disk_free=info.get("disk_free_gb"),
        disk_txt=txt_cls(info.get("disk_pct")),
        disk_bar=bar_cls(info.get("disk_pct")),
        services=info.get("services", {}),
        db_size=info.get("db_size_mb", "?"),
        db_total=fmt(info.get("db_total_readings")),
        db_today=fmt(info.get("db_today_readings")),
        db_first=(info.get("db_first_reading") or "N/A")[:10],
        db_last=(info.get("db_last_reading") or "N/A")[:10],
        cnt_zigbee=fmt(c.get("zigbee")),
        cnt_hive=fmt(c.get("hive")),
        cnt_hw=fmt(c.get("hotwater")),
        cnt_shelly=fmt(c.get("shelly")),
        sensors=info.get("sensor_status", []),
        **_page_links(),
    )


@app.route("/api/dashboard")
def api_dashboard():
    """Dashboard data for UI and integrations."""
    conn = get_db()
    snapshot = _build_dashboard_snapshot(conn)
    conn.close()
    return jsonify(snapshot)


@app.route("/")
@app.route("/dashboard")
def dashboard():
    """Simple web dashboard for current and daily sensor metrics."""
    conn = get_db()
    snapshot = _build_dashboard_snapshot(conn)
    conn.close()

    html = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>Home Sensor Dashboard</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; color: #222; background: #fafafa; }
    h1, h2 { margin-bottom: 8px; }
    h2 { margin-top: 28px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
    .meta { color: #666; margin-bottom: 16px; }
    .titlebar { margin-bottom: 18px; }
    .linkbar { background: #fff; border: 1px solid #ddd; border-radius: 6px; padding: 12px 14px; margin-bottom: 18px; }
    .pill { display: inline-block; margin-right: 12px; margin-bottom: 6px; }
    .endpoint { font-family: Consolas, monospace; background: #f4f4f4; padding: 2px 6px; border-radius: 4px; }
    table { border-collapse: collapse; width: 100%; margin-bottom: 24px; background: #fff; }
    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
    th { background: #f4f4f4; }
    .status-on { color: #0b7a0b; font-weight: bold; }
    .status-off { color: #a33; font-weight: bold; }
    .status-boost { color: #8a2be2; font-weight: bold; }
    .zone-cell { white-space: nowrap; }
    .zone-val { cursor: pointer; border-bottom: 1px dashed #aaa; }
    .zone-val:hover { background: #fffbe6; }
    .zone-edit { display: none; }
    .zone-edit input { width: 90px; padding: 2px 4px; font-size: .9em; }
    .zone-edit button { padding: 2px 6px; font-size: .85em; cursor: pointer; }
    .z2m-badge { font-size: .7em; color: #1a73e8; margin-left: 4px; }
  </style>
</head>
<body>
  <div class="titlebar">
    <h1>&#127968; Home Sensor Dashboard</h1>
    <div class="meta">Generated (Local): {{ generated_at_local or generated_at_utc }} | Auto-refresh: 60s | <a href="/system">&#128202; System Status</a> | <a href="/onboarding">&#128268; Sensor Onboarding</a></div>
  </div>

  <div class="linkbar">
    <div class="pill"><a href="{{ download_url }}">&#128229; Download CSV</a></div>
    <div class="pill"><a href="/api/dashboard">&#128200; Dashboard JSON</a></div>
    <div class="pill"><a href="/api/readings/latest?format=csv">&#128196; Latest readings CSV</a></div>
    <div class="pill"><strong>Power BI URL:</strong> <span class="endpoint">{{ power_bi_url }}</span></div>
  </div>

  <h2>Sonoff Sensors (SNZB-02D / SNZB-02DR2)</h2>
  <table>
    <thead>
      <tr>
        <th>Sensor</th><th>Zone</th><th>Timestamp</th><th>Temp (&deg;C)</th><th>Humidity (%)</th>
        <th>Battery (%)</th><th>Device Min/Max Temp (&deg;C)</th><th>Today Low/High Temp (&deg;C)</th><th>Today Low/High Humidity (%)</th>
      </tr>
    </thead>
    <tbody>
      {% for s in sonoff %}
      <tr>
        <td>{{ s.friendly_name or s.ieee_address }}{% if s.name_source == 'z2m' %}<span class="z2m-badge" title="Name from Zigbee2MQTT">z2m</span>{% endif %}</td>
        <td class="zone-cell" id="zc-{{ s.ieee_address }}">
          <span class="zone-val" onclick="zoneEdit('{{ s.ieee_address }}','{{ s.zone or '' }}')" title="Click to edit zone">{{ s.zone or "-" }}</span>
          <span class="zone-edit">
            <input type="text" placeholder="e.g. Zone 1">
            <button onclick="zoneSave('{{ s.ieee_address }}')">&#10003;</button>
            <button onclick="zoneCancel('{{ s.ieee_address }}')">&#10007;</button>
          </span>
        </td>
        <td>{{ s.timestamp }}</td>
        <td>{% if s.temperature_c is not none %}{{ "%.1f"|format(s.temperature_c) }}{% else %}-{% endif %}</td>
        <td>{% if s.humidity_pct is not none %}{{ "%.1f"|format(s.humidity_pct) }}{% else %}-{% endif %}</td>
        <td>{% if s.battery_pct is not none %}{{ "%.0f"|format(s.battery_pct) }}%{% else %}-{% endif %}</td>
        <td>
          {% if s.device_min_temp_c is not none and s.device_max_temp_c is not none %}
            {{ "%.1f"|format(s.device_min_temp_c) }} / {{ "%.1f"|format(s.device_max_temp_c) }}
          {% else %}-{% endif %}
        </td>
        <td>
          {% if s.min_temp_c is not none and s.max_temp_c is not none %}
            {{ "%.1f"|format(s.min_temp_c) }} / {{ "%.1f"|format(s.max_temp_c) }}
          {% else %}-{% endif %}
        </td>
        <td>
          {% if s.min_humidity_pct is not none and s.max_humidity_pct is not none %}
            {{ "%.1f"|format(s.min_humidity_pct) }} / {{ "%.1f"|format(s.max_humidity_pct) }}
          {% else %}-{% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <h2>Hive Thermostats</h2>
  <table>
    <thead>
      <tr>
        <th>Thermostat</th><th>Zone</th><th>Timestamp</th><th>Current Temp (&deg;C)</th>
        <th>Target Temp (&deg;C)</th><th>Mode</th><th>Status</th><th>Battery (%)</th><th>Daily Runtime (HH:MM)</th>
      </tr>
    </thead>
    <tbody>
      {% for h in hive %}
      <tr>
        <td>{{ h.friendly_name or h.ieee_address }}</td>
        <td class="zone-cell" id="zc-{{ h.ieee_address }}">
          <span class="zone-val" onclick="zoneEdit('{{ h.ieee_address }}','{{ h.zone or '' }}')" title="Click to edit zone">{{ h.zone or "-" }}</span>
          <span class="zone-edit">
            <input type="text" placeholder="e.g. Zone 1">
            <button onclick="zoneSave('{{ h.ieee_address }}')">&#10003;</button>
            <button onclick="zoneCancel('{{ h.ieee_address }}')">&#10007;</button>
          </span>
        </td>
        <td>{{ h.timestamp }}</td>
        <td>{% if h.temperature_c is not none %}{{ "%.1f"|format(h.temperature_c) }}{% else %}-{% endif %}</td>
        <td>{% if h.target_temp_c is not none %}{{ "%.1f"|format(h.target_temp_c) }}{% else %}-{% endif %}</td>
        <td>{{ h.heating_mode or "-" }}</td>
        <td class="status-{{ h.status }}">{{ h.status }}</td>
        <td>{% if h.battery_pct is not none %}{{ "%.0f"|format(h.battery_pct) }}%{% else %}-{% endif %}</td>
        <td>{{ h.runtime_today_hhmm }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <h2>Hive Hot Water</h2>
  <table>
    <thead>
      <tr>
        <th>Device</th><th>Timestamp</th><th>Mode</th><th>Status</th><th>Daily Runtime (HH:MM)</th>
      </tr>
    </thead>
    <tbody>
      {% for h in hotwater %}
      <tr>
        <td>{{ h.friendly_name or h.ieee_address }}</td>
        <td>{{ h.timestamp }}</td>
        <td>{{ h.heating_mode or "-" }}</td>
        <td class="status-{{ h.status }}">{{ h.status }}</td>
        <td>{{ h.runtime_today_hhmm }}</td>
      </tr>
      {% endfor %}
      {% if not hotwater %}
      <tr><td colspan="5" style="color:#999">No hot water data yet</td></tr>
      {% endif %}
    </tbody>
  </table>

  <h2>Shelly Blu H&amp;T</h2>
  <table>
    <thead>
      <tr>
        <th>Sensor</th><th>Zone</th><th>Timestamp</th><th>Temp (&deg;C)</th><th>Humidity (%)</th>
        <th>Battery (%)</th><th>Voltage (V)</th><th>RSSI</th>
        <th>Today Low/High Temp (&deg;C)</th><th>Today Low/High Humidity (%)</th>
      </tr>
    </thead>
    <tbody>
      {% for s in shelly %}
      <tr>
        <td>{{ s.friendly_name or s.ieee_address }}</td>
        <td class="zone-cell" id="zc-{{ s.ieee_address }}">
          <span class="zone-val" onclick="zoneEdit('{{ s.ieee_address }}','{{ s.zone or '' }}')" title="Click to edit zone">{{ s.zone or "-" }}</span>
          <span class="zone-edit">
            <input type="text" placeholder="e.g. Zone 5">
            <button onclick="zoneSave('{{ s.ieee_address }}')">&#10003;</button>
            <button onclick="zoneCancel('{{ s.ieee_address }}')">&#10007;</button>
          </span>
        </td>
        <td>{{ s.timestamp }}</td>
        <td>{% if s.temperature_c is not none %}{{ "%.1f"|format(s.temperature_c) }}{% else %}-{% endif %}</td>
        <td>{% if s.humidity_pct is not none %}{{ "%.1f"|format(s.humidity_pct) }}{% else %}-{% endif %}</td>
        <td>{% if s.battery_pct is not none %}{{ "%.0f"|format(s.battery_pct) }}{% else %}-{% endif %}</td>
        <td>{% if s.battery_voltage_mv is not none %}{{ "%.2f"|format(s.battery_voltage_mv / 1000) }}{% else %}-{% endif %}</td>
        <td>{% if s.rssi is not none %}{{ s.rssi }}{% else %}-{% endif %}</td>
        <td>
          {% if s.min_temp_c is not none and s.max_temp_c is not none %}
            {{ "%.1f"|format(s.min_temp_c) }} / {{ "%.1f"|format(s.max_temp_c) }}
          {% else %}-{% endif %}
        </td>
        <td>
          {% if s.min_humidity_pct is not none and s.max_humidity_pct is not none %}
            {{ "%.1f"|format(s.min_humidity_pct) }} / {{ "%.1f"|format(s.max_humidity_pct) }}
          {% else %}-{% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <h2>Hive Plugs</h2>
  <table>
    <thead>
      <tr>
        <th>Plug</th><th>Zone</th><th>Timestamp</th><th>State</th><th>Power (W)</th><th>Energy (kWh)</th><th>Link quality</th>
      </tr>
    </thead>
    <tbody>
      {% for p in plugs %}
      <tr>
        <td>{{ p.friendly_name or p.ieee_address }}{% if p.name_source == 'z2m' %}<span class="z2m-badge" title="Name from Zigbee2MQTT">z2m</span>{% endif %}</td>
        <td class="zone-cell" id="zc-{{ p.ieee_address }}">
          <span class="zone-val" onclick="zoneEdit('{{ p.ieee_address }}','{{ p.zone or '' }}')" title="Click to edit zone">{{ p.zone or "-" }}</span>
          <span class="zone-edit">
            <input type="text" placeholder="e.g. Zone 1">
            <button onclick="zoneSave('{{ p.ieee_address }}')">&#10003;</button>
            <button onclick="zoneCancel('{{ p.ieee_address }}')">&#10007;</button>
          </span>
        </td>
        <td>{{ p.timestamp }}</td>
        <td class="status-{{ p.state or 'off' }}">{{ p.state or "-" }}</td>
        <td>{% if p.power_w is not none %}{{ "%.2f"|format(p.power_w) }}{% else %}-{% endif %}</td>
        <td>{% if p.energy_kwh is not none %}{{ "%.3f"|format(p.energy_kwh) }}{% else %}-{% endif %}</td>
        <td>{% if p.link_quality is not none %}{{ p.link_quality }}{% else %}-{% endif %}</td>
      </tr>
      {% endfor %}
      {% if not plugs %}
      <tr><td colspan="7" style="color:#999">No plug data</td></tr>
      {% endif %}
    </tbody>
  </table>
<script>
function zoneEdit(ieee, currentZone) {
  var cell = document.getElementById('zc-' + ieee);
  var val = cell.querySelector('.zone-val');
  var editDiv = cell.querySelector('.zone-edit');
  var inp = editDiv.querySelector('input');
  inp.value = currentZone === '-' ? '' : currentZone;
  val.style.display = 'none';
  editDiv.style.display = 'inline';
  inp.focus();
}
function zoneSave(ieee) {
  var cell = document.getElementById('zc-' + ieee);
  var inp = cell.querySelector('.zone-edit input');
  var newZone = inp.value.trim() || null;
  fetch('/api/sensors/' + encodeURIComponent(ieee) + '/zone', {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({zone: newZone})
  }).then(function(r){ return r.json(); }).then(function(d){
    if (d.ok) { location.reload(); }
    else { alert('Failed: ' + d.error); }
  });
}
function zoneCancel(ieee) {
  var cell = document.getElementById('zc-' + ieee);
  cell.querySelector('.zone-val').style.display = '';
  cell.querySelector('.zone-edit').style.display = 'none';
}
</script>
</body>
</html>
"""
    return render_template_string(html, **snapshot)


@app.route("/onboarding")
def onboarding_page():
    """One-sensor-at-a-time onboarding workflow."""
    html = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sensor Onboarding</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; color: #222; background: #fafafa; }
    h1, h2 { margin-bottom: 8px; }
    .meta { color: #666; margin-bottom: 16px; }
    .card { background: #fff; border: 1px solid #ddd; border-radius: 6px; padding: 14px; margin-bottom: 14px; }
    input, select, button { padding: 8px; margin: 4px 0; }
    button { cursor: pointer; }
    .ok { color: #0b7a0b; font-weight: bold; }
    .err { color: #b00020; font-weight: bold; }
    .muted { color: #666; }
    code { background: #f2f2f2; padding: 2px 4px; border-radius: 4px; }
  </style>
</head>
<body>
  <h1>&#128268; Sensor Onboarding</h1>
  <div class="meta"><a href="/dashboard">&#127968; Dashboard</a> | <a href="/system">&#128202; System Status</a></div>

  <div class="card">
    <h2>1) Unlock</h2>
    <label>Passcode:</label><br>
    <input id="passcode" type="password" placeholder="Enter onboarding passcode">
    <button onclick="unlock()">Unlock</button>
    <div id="authMsg" class="muted"></div>
    <div id="tempCodePanel" style="display:none; margin-top:10px;">
      <button onclick="createTempCode()">Generate temporary sharing passcode (15 min)</button>
      <div id="tempCodeMsg" class="muted"></div>
    </div>
  </div>

  <div class="card">
    <h2>2) Start Zigbee2MQTT pairing</h2>
    <div class="muted">This page asks Zigbee2MQTT to open a 120-second permit-join window. Devices are added in Zigbee2MQTT first, then synced into this logger.</div>
    <button onclick="startPairing()">Start 120s Zigbee2MQTT pairing window</button>
    <div id="pairMsg" class="muted"></div>
  </div>

  <div class="card">
    <h2>3) Detected Zigbee2MQTT device</h2>
    <div>Candidate IEEE: <code id="candidateIeee">-</code></div>
    <div>Model: <code id="candidateModel">-</code></div>
    <div>Joined: <code id="candidateJoined">-</code></div>
    <div>First reading: <code id="firstReading">-</code></div>
  </div>

  <div class="card">
    <h2>4) Sync name and zone into this logger</h2>
    <label>IEEE address</label><br>
    <input id="ieeeAddress" placeholder="a4:c1:38:.."><br>
    <label>Friendly name</label><br>
    <input id="friendlyName" placeholder="Attic Probe"><br>
    <label>Zone</label><br>
    <input id="zone" placeholder="Zone 5"><br>
    <button onclick="saveSensor()">Save sensor metadata</button>
    <div id="saveMsg" class="muted"></div>
  </div>

  <div class="card">
    <h2>Onboarding status</h2>
    <div id="statusSummary" class="muted">Loading...</div>
    <button onclick="refreshStatus()">Refresh now</button>
  </div>

<script>
let currentPasscode = "";
let unlocked = false;

async function postJson(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  return await resp.json();
}

async function unlock() {
  const pass = document.getElementById("passcode").value;
  const data = await postJson("/api/onboarding/auth", {passcode: pass});
  if (!data.ok) {
    document.getElementById("authMsg").innerHTML = `<span class="err">${data.error}</span>`;
    return;
  }
  currentPasscode = pass;
  unlocked = true;
  document.getElementById("authMsg").innerHTML = `<span class="ok">Unlocked</span>`;
  document.getElementById("tempCodePanel").style.display = data.is_admin ? "block" : "none";
}

async function createTempCode() {
  if (!unlocked) return;
  const data = await postJson("/api/onboarding/temp-passcode", {passcode: currentPasscode});
  if (!data.ok) {
    document.getElementById("tempCodeMsg").innerHTML = `<span class="err">${data.error}</span>`;
    return;
  }
  document.getElementById("tempCodeMsg").innerHTML =
    `<span class="ok">Temp passcode: <code>${data.passcode}</code> (expires ${data.expires_at})</span>`;
}

async function startPairing() {
  if (!unlocked) {
    document.getElementById("pairMsg").innerHTML = `<span class="err">Unlock first.</span>`;
    return;
  }
  const data = await postJson("/api/onboarding/start-pairing", {passcode: currentPasscode});
  if (!data.ok) {
    document.getElementById("pairMsg").innerHTML = `<span class="err">${data.error}</span>`;
    return;
  }
  document.getElementById("pairMsg").innerHTML =
    `<span class="ok">Zigbee2MQTT pairing started.</span> TCP before pairing: ${data.tcp_precheck_ok ? "OK" : "FAILED"} (${data.tcp_precheck_detail})`;
  refreshStatus();
}

async function saveSensor() {
  if (!unlocked) {
    document.getElementById("saveMsg").innerHTML = `<span class="err">Unlock first.</span>`;
    return;
  }
  const payload = {
    passcode: currentPasscode,
    ieee_address: document.getElementById("ieeeAddress").value.trim(),
    friendly_name: document.getElementById("friendlyName").value.trim(),
    zone: document.getElementById("zone").value.trim()
  };
  const data = await postJson("/api/onboarding/save-sensor", payload);
  if (!data.ok) {
    document.getElementById("saveMsg").innerHTML = `<span class="err">${data.error}</span>`;
    return;
  }
  document.getElementById("saveMsg").innerHTML =
    `<span class="ok">Saved. Updated DB and config.py for ${payload.ieee_address}.</span>`;
  refreshStatus();
}

function _setText(id, value) {
  document.getElementById(id).textContent = value ?? "-";
}

async function refreshStatus() {
  const resp = await fetch("/api/onboarding/status");
  const data = await resp.json();
  _setText("candidateIeee", data.state.candidate_ieee || "-");
  _setText("candidateModel", data.state.candidate_model || "-");
  _setText("candidateJoined", data.state.candidate_joined_at || "-");
  if (data.state.candidate_ieee) {
    document.getElementById("ieeeAddress").value = data.state.candidate_ieee;
  }

  if (data.first_reading && data.first_reading.timestamp) {
    _setText("firstReading", `${data.first_reading.timestamp} temp=${data.first_reading.temperature_c ?? "-"} humidity=${data.first_reading.humidity_pct ?? "-"} battery=${data.first_reading.battery_pct ?? "-"}`);
  } else if (data.first_reading_waiting) {
    _setText("firstReading", `Waiting (deadline ${data.first_reading_deadline || "-"})`);
  } else {
    _setText("firstReading", "-");
  }

  const tcpBefore = data.state.tcp_precheck_ok;
  const tcpAfter = data.state.tcp_postcheck_ok;
  const statusText = [
    `Pairing active: ${data.state.pairing_active ? "yes" : "no"}`,
    `Pairing window: ${data.state.pairing_started_at || "-"} -> ${data.state.pairing_ends_at || "-"}`,
    `TCP pre-check: ${tcpBefore === null ? "-" : (tcpBefore ? "OK" : "FAILED")}`,
    `TCP post-check: ${tcpAfter === null ? "-" : (tcpAfter ? "OK" : "FAILED")}`,
    `TCP now: ${data.tcp_now_ok ? "OK" : "FAILED"} (${data.tcp_now_detail})`,
    data.state.last_error ? `Last error: ${data.state.last_error}` : ""
  ].filter(Boolean).join(" | ");
  document.getElementById("statusSummary").textContent = statusText;
}

setInterval(refreshStatus, 5000);
refreshStatus();
</script>
</body>
</html>
"""
    return render_template_string(html)


@app.route("/api/onboarding/auth", methods=["POST"])
def api_onboarding_auth():
    conn = get_db_write()
    try:
        payload = request.get_json(silent=True) or {}
        passcode = str(payload.get("passcode", ""))
        if not os.environ.get("ONBOARDING_PASSCODE"):
            return jsonify({"ok": False, "error": "ONBOARDING_PASSCODE is not configured on the server."}), 503
        result = _onboarding_auth_result(conn, passcode)
        return jsonify(result), (200 if result.get("ok") else 401)
    finally:
        conn.close()


@app.route("/api/onboarding/temp-passcode", methods=["POST"])
def api_onboarding_temp_passcode():
    conn = get_db_write()
    try:
        payload = request.get_json(silent=True) or {}
        passcode = str(payload.get("passcode", ""))
        if not _is_admin_passcode(passcode):
            return jsonify({"ok": False, "error": "Admin passcode required."}), 403
        temp_code, expires_at = create_temp_passcode(conn, ttl_minutes=15)
        return jsonify({
            "ok": True,
            "passcode": temp_code,
            "expires_at": expires_at,
        })
    finally:
        conn.close()


@app.route("/api/onboarding/start-pairing", methods=["POST"])
def api_onboarding_start_pairing():
    conn = get_db_write()
    try:
        payload = request.get_json(silent=True) or {}
        passcode = str(payload.get("passcode", ""))
        auth = _onboarding_auth_result(conn, passcode)
        if not auth.get("ok"):
            return jsonify(auth), 401

        tcp_ok, tcp_detail = _check_dongle_tcp()
        set_tcp_check_state(conn, precheck_ok=tcp_ok)
        if not tcp_ok:
            return jsonify({
                "ok": False,
                "error": f"Dongle-M TCP pre-check failed: {tcp_detail}",
                "tcp_precheck_ok": False,
                "tcp_precheck_detail": tcp_detail,
            }), 503

        command_id = queue_start_pairing(conn)
        return jsonify({
            "ok": True,
            "command_id": command_id,
            "tcp_precheck_ok": True,
            "tcp_precheck_detail": tcp_detail,
        })
    finally:
        conn.close()


@app.route("/api/onboarding/save-sensor", methods=["POST"])
def api_onboarding_save_sensor():
    conn = get_db_write()
    try:
        payload = request.get_json(silent=True) or {}
        passcode = str(payload.get("passcode", ""))
        auth = _onboarding_auth_result(conn, passcode)
        if not auth.get("ok"):
            return jsonify(auth), 401

        ieee_address = str(payload.get("ieee_address", "")).strip().lower()
        friendly_name = str(payload.get("friendly_name", "")).strip()
        zone = str(payload.get("zone", "")).strip()
        if not ieee_address or not friendly_name or not zone:
            return jsonify({"ok": False, "error": "ieee_address, friendly_name and zone are required."}), 400

        save_sensor_metadata(
            conn,
            ieee_address=ieee_address,
            friendly_name=friendly_name,
            zone=zone,
        )
        return jsonify({"ok": True})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/onboarding/status")
def api_onboarding_status():
    conn = get_db_write()
    try:
        state = get_onboarding_state(conn)
        candidate_ieee = state.get("candidate_ieee")
        candidate_joined_at = state.get("candidate_joined_at")
        first_reading = None
        if candidate_ieee:
            if candidate_joined_at:
                row = conn.execute(
                    """
                    SELECT timestamp, temperature_c, humidity_pct, battery_pct
                    FROM readings
                    WHERE ieee_address = ?
                      AND timestamp >= ?
                    ORDER BY timestamp ASC
                    LIMIT 1
                    """,
                    (candidate_ieee, candidate_joined_at),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT timestamp, temperature_c, humidity_pct, battery_pct
                    FROM readings
                    WHERE ieee_address = ?
                    ORDER BY timestamp DESC
                    LIMIT 1
                    """,
                    (candidate_ieee,),
                ).fetchone()
            if row:
                first_reading = dict(row)

        now_dt = datetime.now()
        deadline = None
        first_reading_waiting = False
        if candidate_joined_at:
            joined_ts = _parse_iso_timestamp(candidate_joined_at)
            deadline_dt = joined_ts + timedelta(minutes=5)
            deadline = deadline_dt.strftime("%Y-%m-%dT%H:%M:%S")
            first_reading_waiting = first_reading is None and now_dt < deadline_dt

        tcp_now_ok, tcp_now_detail = _check_dongle_tcp()
        if not state.get("pairing_active") and state.get("pairing_ends_at"):
            set_tcp_check_state(conn, postcheck_ok=tcp_now_ok)
            state = get_onboarding_state(conn)

        return jsonify({
            "ok": True,
            "state": state,
            "first_reading": first_reading,
            "first_reading_waiting": first_reading_waiting,
            "first_reading_deadline": deadline,
            "tcp_now_ok": tcp_now_ok,
            "tcp_now_detail": tcp_now_detail,
        })
    finally:
        conn.close()


@app.route("/api/sensors")
def api_sensors():
    """List all registered sensors."""
    conn = get_db()
    rows = conn.execute(
        "SELECT ieee_address, friendly_name, model, COALESCE(zone_override, zone) AS zone, first_seen, last_seen "
        "FROM sensors ORDER BY friendly_name"
    ).fetchall()
    conn.close()

    sensors = [dict(row) for row in rows]
    return jsonify(sensors)


@app.route("/api/readings")
def api_readings():
    """
    Get sensor readings with optional filters.

    Query params: start, end, sensor, zone, limit, format
    """
    start = request.args.get("start")
    end = request.args.get("end")
    sensor = request.args.get("sensor")
    zone = request.args.get("zone")
    limit = request.args.get("limit", "10000", type=str)
    output_format = request.args.get("format", "json")

    query = """
        SELECT r.ieee_address, s.friendly_name, r.timestamp, r.reading_date, r.reading_time,
               r.temperature_c, r.humidity_pct, r.battery_pct, r.battery_voltage_mv,
               r.link_quality, r.rssi, r.zone, r.state, r.power_w, r.energy_kwh,
               r.heating_on, r.boost_on, r.target_temp_c, r.heating_mode,
               r.device_min_temp_c, r.device_max_temp_c,
               r.device_min_humidity_pct, r.device_max_humidity_pct
        FROM readings r
        LEFT JOIN sensors s ON r.ieee_address = s.ieee_address
        WHERE 1=1
    """
    params = []

    if start:
        query += " AND r.timestamp >= ?"
        params.append(start)
    if end:
        query += " AND r.timestamp <= ?"
        params.append(end)
    if sensor:
        query += " AND r.ieee_address = ?"
        params.append(sensor)
    if zone:
        query += " AND COALESCE(s.zone_override, r.zone) = ?"
        params.append(zone)

    query += " ORDER BY r.timestamp DESC LIMIT ?"
    params.append(int(limit))

    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()

    if output_format == "csv":
        return _rows_to_csv(rows)

    return jsonify([dict(row) for row in rows])


@app.route("/api/readings/latest")
def api_readings_latest():
    """Get the most recent reading for each sensor."""
    conn = get_db()
    rows = conn.execute("""
        SELECT r.ieee_address, s.friendly_name, r.timestamp, r.reading_date, r.reading_time,
               r.temperature_c, r.humidity_pct, r.battery_pct, r.battery_voltage_mv,
               r.link_quality, r.rssi, r.zone, r.state, r.power_w, r.energy_kwh,
               r.heating_on, r.boost_on, r.target_temp_c, r.heating_mode,
               r.device_min_temp_c, r.device_max_temp_c,
               r.device_min_humidity_pct, r.device_max_humidity_pct
        FROM readings r
        INNER JOIN (
            SELECT ieee_address, MAX(timestamp) as max_ts
            FROM readings GROUP BY ieee_address
        ) latest ON r.ieee_address = latest.ieee_address
                AND r.timestamp = latest.max_ts
        LEFT JOIN sensors s ON r.ieee_address = s.ieee_address
        ORDER BY s.friendly_name
    """).fetchall()
    conn.close()

    output_format = request.args.get("format", "json")
    if output_format == "csv":
        return _rows_to_csv(rows)

    return jsonify([dict(row) for row in rows])


@app.route("/api/export/csv")
def api_export_csv():
    """Export all readings as a downloadable CSV file."""
    start = request.args.get("start")
    end = request.args.get("end")
    zone = request.args.get("zone")

    query = """
        SELECT r.ieee_address, s.friendly_name, r.timestamp, r.reading_date, r.reading_time,
               r.temperature_c, r.humidity_pct, r.battery_pct, r.battery_voltage_mv,
               r.link_quality, r.rssi, r.zone, r.state, r.power_w, r.energy_kwh,
               r.heating_on, r.boost_on, r.target_temp_c, r.heating_mode,
               r.device_min_temp_c, r.device_max_temp_c,
               r.device_min_humidity_pct, r.device_max_humidity_pct
        FROM readings r
        LEFT JOIN sensors s ON r.ieee_address = s.ieee_address
        WHERE 1=1
    """
    params = []

    if start:
        query += " AND r.timestamp >= ?"
        params.append(start)
    if end:
        query += " AND r.timestamp <= ?"
        params.append(end)
    if zone:
        query += " AND COALESCE(s.zone_override, r.zone) = ?"
        params.append(zone)

    query += " ORDER BY r.timestamp ASC"

    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()

    return _rows_to_csv(rows, download=True)


def _rows_to_csv(rows, download: bool = False) -> Response:
    """Convert SQLite rows to CSV response."""
    if not rows:
        return Response("No data\n", mimetype="text/csv")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    for row in rows:
        writer.writerow(dict(row))

    headers = {}
    if download:
        filename = f"sensor_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        headers["Content-Disposition"] = f"attachment; filename={filename}"

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers=headers,
    )


def run_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Start the web API server."""
    logger.info("Starting data API server on http://%s:%d", host, port)
    print(f"\n{'='*50}")
    print(f"  Sensor Data API running on http://{host}:{port}")
    print(f"{'='*50}")
    print(f"\nEndpoints:")
    print(f"  GET /dashboard           - Live sensor dashboard")
    print(f"  GET /onboarding          - Sensor onboarding workflow")
    print(f"  GET /system              - Pi + app system status")
    print(f"  GET /api/status          - System overview")
    print(f"  GET /api/dashboard       - Dashboard JSON data")
    print(f"  GET /api/sensors         - All sensors")
    print(f"  GET /api/readings        - Readings (filterable)")
    print(f"  GET /api/readings/latest - Latest per sensor")
    print(f"  GET /api/export/csv      - Download CSV")
    print(f"\nPower BI connection:")
    print(f"  Use 'Web' data source with URL:")
    print(f"  http://<pi-ip>:{port}/api/readings?format=csv")
    print(f"\nPress Ctrl+C to stop.\n")

    app.run(host=host, port=port, debug=False)
