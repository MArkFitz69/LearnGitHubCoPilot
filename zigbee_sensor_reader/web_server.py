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
import logging
import sqlite3
from datetime import datetime

from flask import Flask, Response, jsonify, render_template_string, request

from .config import DATABASE_PATH, SHELLY_SENSORS

logger = logging.getLogger(__name__)

app = Flask(__name__)


def get_db() -> sqlite3.Connection:
    """Get a read-only database connection."""
    conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


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


def _calculate_hive_runtime_seconds(conn: sqlite3.Connection, day: str) -> dict[str, float]:
    """
    Calculate today's runtime in seconds for each Hive thermostat.

    Runtime is computed from sampled heating_on states by summing intervals where
    heating_on=1 from each sample time to the next sample time.
    """
    rows = conn.execute(
        """
        SELECT ieee_address, timestamp, heating_on
        FROM readings
        WHERE ieee_address LIKE 'hive:%'
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
        SELECT r.ieee_address, s.friendly_name, s.model, r.timestamp,
               r.temperature_c, r.humidity_pct, r.battery_pct, r.zone,
               r.heating_on, r.boost_on, r.target_temp_c, r.heating_mode
        FROM readings r
        INNER JOIN (
            SELECT ieee_address, MAX(timestamp) AS max_ts
            FROM readings
            GROUP BY ieee_address
        ) latest ON r.ieee_address = latest.ieee_address AND r.timestamp = latest.max_ts
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
            "min_temp_c": daily.get("min_temp_c"),
            "max_temp_c": daily.get("max_temp_c"),
            "min_humidity_pct": daily.get("min_humidity_pct"),
            "max_humidity_pct": daily.get("max_humidity_pct"),
        }

        if model.startswith("SNZB-02"):
            sonoff.append(sensor_row)
        elif ieee_address.startswith("shelly:") or model == "Shelly Blu H&T":
            shelly.append(sensor_row)

    sonoff.sort(key=lambda row: (_zone_sort_key(row.get("zone")), row.get("friendly_name") or row.get("ieee_address")))
    hive.sort(key=lambda row: (_zone_sort_key(row.get("zone")), row.get("friendly_name") or row.get("ieee_address")))
    shelly.sort(key=lambda row: (_zone_sort_key(row.get("zone")), row.get("friendly_name") or row.get("ieee_address")))

    return {
        "date_local": today_local,
        "generated_at_local": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        # Backward-compatible aliases used by earlier dashboard/API responses.
        "date_utc": today_local,
        "generated_at_utc": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "sonoff": sonoff,
        "shelly": shelly,
        "hive": hive,
    }


@app.route("/api/status")
def api_status():
    """System status overview."""
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
    body { font-family: Arial, sans-serif; margin: 20px; color: #222; }
    h1, h2 { margin-bottom: 8px; }
    .meta { color: #666; margin-bottom: 16px; }
    table { border-collapse: collapse; width: 100%; margin-bottom: 24px; }
    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
    th { background: #f4f4f4; }
    .status-on { color: #0b7a0b; font-weight: bold; }
    .status-off { color: #a33; font-weight: bold; }
    .status-boost { color: #8a2be2; font-weight: bold; }
  </style>
</head>
<body>
  <h1>Home Sensor Dashboard</h1>
  <div class="meta">Generated (Local): {{ generated_at_local or generated_at_utc }} | Auto-refresh: 60s</div>

  <h2>Sonoff Sensors (SNZB-02D / SNZB-02DR2)</h2>
  <table>
    <thead>
      <tr>
        <th>Sensor</th><th>Zone</th><th>Timestamp</th><th>Temp (C)</th><th>Humidity (%)</th>
        <th>Daily Low/High Temp (C)</th><th>Daily Low/High Humidity (%)</th>
      </tr>
    </thead>
    <tbody>
      {% for s in sonoff %}
      <tr>
        <td>{{ s.friendly_name or s.ieee_address }}</td>
        <td>{{ s.zone or "-" }}</td>
        <td>{{ s.timestamp }}</td>
        <td>{% if s.temperature_c is not none %}{{ "%.1f"|format(s.temperature_c) }}{% else %}-{% endif %}</td>
        <td>{% if s.humidity_pct is not none %}{{ "%.1f"|format(s.humidity_pct) }}{% else %}-{% endif %}</td>
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
        <th>Thermostat</th><th>Zone</th><th>Timestamp</th><th>Current Temp (C)</th>
        <th>Target Temp (C)</th><th>Mode</th><th>Status</th><th>Daily Runtime (HH:MM)</th>
      </tr>
    </thead>
    <tbody>
      {% for h in hive %}
      <tr>
        <td>{{ h.friendly_name or h.ieee_address }}</td>
        <td>{{ h.zone or "-" }}</td>
        <td>{{ h.timestamp }}</td>
        <td>{% if h.temperature_c is not none %}{{ "%.1f"|format(h.temperature_c) }}{% else %}-{% endif %}</td>
        <td>{% if h.target_temp_c is not none %}{{ "%.1f"|format(h.target_temp_c) }}{% else %}-{% endif %}</td>
        <td>{{ h.heating_mode or "-" }}</td>
        <td class="status-{{ h.status }}">{{ h.status }}</td>
        <td>{{ h.runtime_today_hhmm }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <h2>Shelly Blu H&amp;T</h2>
  <table>
    <thead>
      <tr>
        <th>Sensor</th><th>Zone</th><th>Timestamp</th><th>Temp (C)</th><th>Humidity (%)</th><th>Battery (%)</th>
        <th>Daily Low/High Temp (C)</th><th>Daily Low/High Humidity (%)</th>
      </tr>
    </thead>
    <tbody>
      {% for s in shelly %}
      <tr>
        <td>{{ s.friendly_name or s.ieee_address }}</td>
        <td>{{ s.zone or "-" }}</td>
        <td>{{ s.timestamp }}</td>
        <td>{% if s.temperature_c is not none %}{{ "%.1f"|format(s.temperature_c) }}{% else %}-{% endif %}</td>
        <td>{% if s.humidity_pct is not none %}{{ "%.1f"|format(s.humidity_pct) }}{% else %}-{% endif %}</td>
        <td>{% if s.battery_pct is not none %}{{ "%.0f"|format(s.battery_pct) }}{% else %}-{% endif %}</td>
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
</body>
</html>
"""
    return render_template_string(html, **snapshot)


@app.route("/api/sensors")
def api_sensors():
    """List all registered sensors."""
    conn = get_db()
    rows = conn.execute(
        "SELECT ieee_address, friendly_name, model, zone, first_seen, last_seen "
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
        SELECT r.ieee_address, s.friendly_name, r.timestamp,
               r.temperature_c, r.humidity_pct, r.battery_pct,
               r.zone, r.heating_on, r.boost_on,
               r.target_temp_c, r.heating_mode
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
        query += " AND r.zone = ?"
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
        SELECT r.ieee_address, s.friendly_name, r.timestamp,
               r.temperature_c, r.humidity_pct, r.battery_pct,
               r.zone, r.heating_on, r.boost_on,
               r.target_temp_c, r.heating_mode
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
        SELECT r.ieee_address, s.friendly_name, r.timestamp,
               r.temperature_c, r.humidity_pct, r.battery_pct,
               r.zone, r.heating_on, r.boost_on,
               r.target_temp_c, r.heating_mode
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
        query += " AND r.zone = ?"
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
