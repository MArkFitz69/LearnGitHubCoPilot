"""
Main entry point for the Zigbee Sensor Reader.

Usage:
    python -m zigbee_sensor_reader              # Start collecting data
    python -m zigbee_sensor_reader --pair        # Open network for new sensors
    python -m zigbee_sensor_reader --export csv  # Export data to CSV
    python -m zigbee_sensor_reader --export xlsx # Export data to Excel
    python -m zigbee_sensor_reader --summary     # Show sensor summary
"""

import argparse
import asyncio
import logging
import sys

from .config import POLLING_INTERVAL, SENSOR_NAMES, ZONES
from .database import get_connection, insert_reading, upsert_sensor
from .export import export_to_csv, export_to_excel, get_sensor_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def handle_reading(reading, conn) -> None:
    """Process an incoming sensor reading: store in DB and print to console."""
    zone = ZONES.get(reading.ieee_address)
    upsert_sensor(
        conn,
        ieee_address=reading.ieee_address,
        friendly_name=reading.friendly_name,
        model=reading.model,
        zone=zone,
    )

    insert_reading(
        conn,
        ieee_address=reading.ieee_address,
        temperature_c=reading.temperature_c,
        humidity_pct=reading.humidity_pct,
        battery_pct=reading.battery_pct,
        link_quality=reading.link_quality,
        zone=zone,
        device_min_temp_c=getattr(reading, "device_min_temp_c", None),
        device_max_temp_c=getattr(reading, "device_max_temp_c", None),
        device_min_humidity_pct=getattr(reading, "device_min_humidity_pct", None),
        device_max_humidity_pct=getattr(reading, "device_max_humidity_pct", None),
        battery_voltage_mv=getattr(reading, "battery_voltage_mv", None),
    )

    parts = [f"[{reading.friendly_name}]"]
    if zone:
        parts.append(f"({zone})")
    if reading.temperature_c is not None:
        parts.append(f"Temp: {reading.temperature_c:.1f}°C")
    if reading.humidity_pct is not None:
        parts.append(f"Humidity: {reading.humidity_pct:.1f}%")
    if reading.battery_pct is not None:
        parts.append(f"Battery: {reading.battery_pct:.0f}%")
    bv = getattr(reading, "battery_voltage_mv", None)
    if bv is not None:
        parts.append(f"V: {bv/1000:.2f}V")
    dmin = getattr(reading, "device_min_temp_c", None)
    dmax = getattr(reading, "device_max_temp_c", None)
    if dmin is not None and dmax is not None:
        parts.append(f"Device min/max: {dmin:.1f}/{dmax:.1f}°C")
    print("  ".join(parts))


async def run_collector(pair: bool = False) -> None:
    """Run the main data collection loop."""
    # Import Zigbee dependencies only when actually collecting data
    from .zigbee_reader import ZigbeeSensorListener

    conn = get_connection()
    logger.info("Database ready at %s", conn.execute("PRAGMA database_list").fetchone()[2])

    # Register any pre-configured sensor names
    for ieee, name in SENSOR_NAMES.items():
        upsert_sensor(conn, ieee_address=ieee, friendly_name=name, zone=ZONES.get(ieee))

    listener = ZigbeeSensorListener(
        on_reading=lambda reading: handle_reading(reading, conn)
    )

    try:
        await listener.start()

        if pair:
            logger.info(
                "Pairing mode: network is open for 120 seconds. "
                "Put your sensor in pairing mode now (hold button 5+ seconds)."
            )
            await listener.permit_join(duration=120)

        logger.info(
            "Collecting sensor data (Ctrl+C to stop). "
            "Polling interval: %ds",
            POLLING_INTERVAL,
        )

        # Sensors report asynchronously via attribute_updated callback
        # Also poll Hive thermostats on each interval
        while True:
            await asyncio.sleep(POLLING_INTERVAL)
            logger.debug("Heartbeat – still listening...")

            # Read cached Zigbee sensor values and store them
            try:
                from .zigbee_reader import read_cached_sensors
                cached = read_cached_sensors(listener.app)
                for reading in cached:
                    handle_reading(reading, conn)
                if cached:
                    logger.info("Stored %d Zigbee sensor readings (cached)", len(cached))
            except Exception as e:
                logger.debug("Zigbee cache read failed: %s", e)

            # Poll Hive if credentials are configured
            try:
                from .hive_reader import poll_hive, HIVE_USERNAME
                if HIVE_USERNAME:
                    await poll_hive()
            except Exception as e:
                logger.debug("Hive poll skipped: %s", e)

            # Poll Shelly Blu sensors via BLE (if bleak is available)
            try:
                from .shelly_ble_reader import poll_shelly_ble
                from .config import SHELLY_SCAN_DURATION
                await poll_shelly_ble(scan_duration=SHELLY_SCAN_DURATION)
            except ImportError:
                pass  # bleak not installed (e.g. running on Windows dev machine)
            except Exception as e:
                logger.debug("Shelly BLE poll skipped: %s", e)

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await listener.stop()
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sonoff Zigbee Temperature & Humidity Sensor Reader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m zigbee_sensor_reader                  Start collecting sensor data
  python -m zigbee_sensor_reader --pair            Pair new sensors (120s window)
  python -m zigbee_sensor_reader --export csv      Export all data to CSV
  python -m zigbee_sensor_reader --export xlsx     Export all data to Excel
  python -m zigbee_sensor_reader --summary         Show sensor summary stats
  python -m zigbee_sensor_reader --export csv --start 2026-01-01 --end 2026-07-01
        """,
    )
    parser.add_argument(
        "--pair",
        action="store_true",
        help="Open network for new sensors to join (120 second window)",
    )
    parser.add_argument(
        "--export",
        choices=["csv", "xlsx"],
        help="Export data to CSV or Excel file",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Show summary statistics for all sensors",
    )
    parser.add_argument(
        "--start",
        help="Start date filter for exports (ISO format, e.g. 2026-01-01)",
    )
    parser.add_argument(
        "--end",
        help="End date filter for exports (ISO format, e.g. 2026-07-31)",
    )
    parser.add_argument(
        "--sensor",
        help="Filter to a specific sensor IEEE address",
    )
    parser.add_argument(
        "--hive",
        action="store_true",
        help="Test Hive API connection and fetch current thermostat data",
    )
    parser.add_argument(
        "--discover-shelly",
        action="store_true",
        help="Scan for Shelly Blu H&T sensors via Bluetooth and show their MAC addresses",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start the web API server for remote data access (Power BI, Excel)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port for the web API server (default: 8080)",
    )

    args = parser.parse_args()

    if args.discover_shelly:
        from .shelly_ble_reader import discover_shelly_sensors
        print("Scanning for Shelly Blu sensors (30 seconds)...")
        print("Make sure the sensor is nearby and has battery.")
        results = asyncio.run(discover_shelly_sensors(scan_duration=30.0))
        if results:
            print(f"\nFound {len(results)} sensor(s):")
            for s in results:
                print(f"  MAC: {s['mac']}  Name: {s.get('name', '?')}")
                print(f"       Temp: {s.get('temperature', '?')}°C  "
                      f"Humidity: {s.get('humidity', '?')}%  "
                      f"Battery: {s.get('battery', '?')}%")
            print("\nAdd the MAC address to SHELLY_SENSORS in config.py:")
            for s in results:
                print(f'    "{s["mac"]}": "Outside",')
        else:
            print("\nNo sensors found. Try:")
            print("  - Moving the sensor closer to the Pi")
            print("  - Pressing the button on the sensor to wake it")
            print("  - Running the scan for longer (set SHELLY_SCAN_DURATION=60)")
        return

    if args.hive:
        from .hive_reader import poll_hive, HIVE_USERNAME
        if not HIVE_USERNAME:
            print("Set HIVE_USERNAME and HIVE_PASSWORD environment variables first.")
            return
        print("Connecting to Hive API...")
        readings = asyncio.run(poll_hive())
        if readings:
            print(f"\nFound {len(readings)} thermostat(s):")
            for r in readings:
                print(f"  {r['name']}: {r['temperature_c']}°C "
                      f"(target: {r['target_temp_c']}°C, mode: {r['mode']})")
        else:
            print("No thermostat data returned. Check credentials.")
        return

    if args.summary:
        get_sensor_summary()
        return

    if args.serve:
        from .web_server import run_server
        run_server(port=args.port)
        return

    if args.export:
        if args.export == "csv":
            export_to_csv(
                start_date=args.start,
                end_date=args.end,
                sensor_ieee=args.sensor,
            )
        elif args.export == "xlsx":
            export_to_excel(
                start_date=args.start,
                end_date=args.end,
                sensor_ieee=args.sensor,
            )
        return

    # Default: run the data collector
    asyncio.run(run_collector(pair=args.pair))


if __name__ == "__main__":
    main()
