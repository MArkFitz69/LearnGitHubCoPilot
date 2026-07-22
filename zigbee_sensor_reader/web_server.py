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

from flask import Flask, Response, jsonify, request

from .config import DATABASE_PATH

logger = logging.getLogger(__name__)

app = Flask(__name__)


def get_db() -> sqlite3.Connection:
    """Get a read-only database connection."""
    conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


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
        "server_time": datetime.utcnow().isoformat(),
    })


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
        filename = f"sensor_data_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
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
    print(f"  GET /api/status          - System overview")
    print(f"  GET /api/sensors         - All sensors")
    print(f"  GET /api/readings        - Readings (filterable)")
    print(f"  GET /api/readings/latest - Latest per sensor")
    print(f"  GET /api/export/csv      - Download CSV")
    print(f"\nPower BI connection:")
    print(f"  Use 'Web' data source with URL:")
    print(f"  http://<pi-ip>:{port}/api/readings?format=csv")
    print(f"\nPress Ctrl+C to stop.\n")

    app.run(host=host, port=port, debug=False)
