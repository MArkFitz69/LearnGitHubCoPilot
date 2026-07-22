"""
Export sensor data from SQLite to CSV or Excel for analysis in Excel / Power BI.
"""

import csv
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import DATABASE_PATH, EXPORT_DIR


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def export_to_csv(
    output_path: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sensor_ieee: str | None = None,
) -> str:
    """
    Export readings to a CSV file.

    Args:
        output_path: File path for the CSV. Defaults to exports/readings_<timestamp>.csv
        start_date:  ISO date string to filter from (inclusive)
        end_date:    ISO date string to filter to (inclusive)
        sensor_ieee: Filter to a specific sensor's IEEE address

    Returns:
        The path to the created CSV file.
    """
    export_dir = Path(EXPORT_DIR)
    export_dir.mkdir(parents=True, exist_ok=True)

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(export_dir / f"readings_{ts}.csv")

    conn = _get_conn()
    query = """
        SELECT
            r.timestamp,
            r.ieee_address,
            COALESCE(s.friendly_name, r.ieee_address) AS sensor_name,
            s.model,
            r.temperature_c,
            r.humidity_pct,
            r.battery_pct,
            r.link_quality
        FROM readings r
        LEFT JOIN sensors s ON r.ieee_address = s.ieee_address
        WHERE 1=1
    """
    params: list = []

    if start_date:
        query += " AND r.timestamp >= ?"
        params.append(start_date)
    if end_date:
        query += " AND r.timestamp <= ?"
        params.append(end_date)
    if sensor_ieee:
        query += " AND r.ieee_address = ?"
        params.append(sensor_ieee)

    query += " ORDER BY r.timestamp ASC"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Timestamp", "IEEE Address", "Sensor Name", "Model",
            "Temperature (°C)", "Humidity (%)", "Battery (%)", "Link Quality",
        ])
        for row in rows:
            writer.writerow([
                row["timestamp"],
                row["ieee_address"],
                row["sensor_name"],
                row["model"],
                row["temperature_c"],
                row["humidity_pct"],
                row["battery_pct"],
                row["link_quality"],
            ])

    print(f"Exported {len(rows)} readings to {output_path}")
    return output_path


def export_to_excel(
    output_path: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    sensor_ieee: str | None = None,
) -> str:
    """
    Export readings to an Excel (.xlsx) file with separate sheets per sensor.

    Requires openpyxl: pip install openpyxl
    """
    try:
        import openpyxl
    except ImportError:
        raise ImportError(
            "openpyxl is required for Excel export. Install it with: pip install openpyxl"
        )

    export_dir = Path(EXPORT_DIR)
    export_dir.mkdir(parents=True, exist_ok=True)

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(export_dir / f"readings_{ts}.xlsx")

    conn = _get_conn()

    # Get all sensors
    sensors = conn.execute(
        "SELECT ieee_address, friendly_name FROM sensors ORDER BY friendly_name"
    ).fetchall()

    wb = openpyxl.Workbook()

    # Create an "All Sensors" summary sheet
    ws_all = wb.active
    ws_all.title = "All Sensors"
    headers = [
        "Timestamp", "Sensor Name", "Temperature (°C)",
        "Humidity (%)", "Battery (%)", "Link Quality",
    ]
    ws_all.append(headers)

    query = """
        SELECT
            r.timestamp,
            COALESCE(s.friendly_name, r.ieee_address) AS sensor_name,
            r.temperature_c,
            r.humidity_pct,
            r.battery_pct,
            r.link_quality
        FROM readings r
        LEFT JOIN sensors s ON r.ieee_address = s.ieee_address
        WHERE 1=1
    """
    params: list = []
    if start_date:
        query += " AND r.timestamp >= ?"
        params.append(start_date)
    if end_date:
        query += " AND r.timestamp <= ?"
        params.append(end_date)
    if sensor_ieee:
        query += " AND r.ieee_address = ?"
        params.append(sensor_ieee)
    query += " ORDER BY r.timestamp ASC"

    rows = conn.execute(query, params).fetchall()
    for row in rows:
        ws_all.append([
            row["timestamp"], row["sensor_name"], row["temperature_c"],
            row["humidity_pct"], row["battery_pct"], row["link_quality"],
        ])

    # Create per-sensor sheets
    for sensor in sensors:
        ieee = sensor["ieee_address"]
        name = sensor["friendly_name"] or ieee
        sheet_name = name[:31]  # Sheet names max 31 chars
        ws = wb.create_sheet(title=sheet_name)
        ws.append(["Timestamp", "Temperature (°C)", "Humidity (%)", "Battery (%)", "Link Quality"])

        sensor_query = """
            SELECT timestamp, temperature_c, humidity_pct, battery_pct, link_quality
            FROM readings
            WHERE ieee_address = ?
        """
        sensor_params = [ieee]
        if start_date:
            sensor_query += " AND timestamp >= ?"
            sensor_params.append(start_date)
        if end_date:
            sensor_query += " AND timestamp <= ?"
            sensor_params.append(end_date)
        sensor_query += " ORDER BY timestamp ASC"

        for row in conn.execute(sensor_query, sensor_params):
            ws.append([
                row["timestamp"], row["temperature_c"],
                row["humidity_pct"], row["battery_pct"], row["link_quality"],
            ])

    conn.close()
    wb.save(output_path)
    print(f"Exported {len(rows)} readings to {output_path}")
    return output_path


def get_sensor_summary() -> None:
    """Print a summary of all sensors and their latest readings."""
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT
            s.ieee_address,
            COALESCE(s.friendly_name, s.ieee_address) AS name,
            s.model,
            s.first_seen,
            s.last_seen,
            COUNT(r.id) AS total_readings,
            ROUND(AVG(r.temperature_c), 1) AS avg_temp,
            ROUND(MIN(r.temperature_c), 1) AS min_temp,
            ROUND(MAX(r.temperature_c), 1) AS max_temp,
            ROUND(AVG(r.humidity_pct), 1) AS avg_humidity
        FROM sensors s
        LEFT JOIN readings r ON s.ieee_address = r.ieee_address
        GROUP BY s.ieee_address
        ORDER BY s.friendly_name
        """
    ).fetchall()
    conn.close()

    if not rows:
        print("No sensors found in database.")
        return

    print(f"\n{'='*70}")
    print(f"{'Sensor Summary':^70}")
    print(f"{'='*70}")
    for row in rows:
        print(f"\n  {row['name']} ({row['ieee_address']})")
        print(f"    Model:    {row['model'] or 'Unknown'}")
        print(f"    Readings: {row['total_readings']}")
        if row["total_readings"] > 0:
            print(f"    Temp:     {row['avg_temp']}°C avg "
                  f"({row['min_temp']}°C – {row['max_temp']}°C)")
            print(f"    Humidity: {row['avg_humidity']}% avg")
        print(f"    First:    {row['first_seen']}")
        print(f"    Last:     {row['last_seen']}")
    print(f"\n{'='*70}\n")
