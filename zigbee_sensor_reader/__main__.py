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

from .config import POLLING_INTERVAL, SENSOR_NAMES
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
    upsert_sensor(
        conn,
        ieee_address=reading.ieee_address,
        friendly_name=reading.friendly_name,
        model=reading.model,
    )

    insert_reading(
        conn,
        ieee_address=reading.ieee_address,
        temperature_c=reading.temperature_c,
        humidity_pct=reading.humidity_pct,
        battery_pct=reading.battery_pct,
        link_quality=reading.link_quality,
    )

    parts = [f"[{reading.friendly_name}]"]
    if reading.temperature_c is not None:
        parts.append(f"Temp: {reading.temperature_c:.1f}°C")
    if reading.humidity_pct is not None:
        parts.append(f"Humidity: {reading.humidity_pct:.1f}%")
    if reading.battery_pct is not None:
        parts.append(f"Battery: {reading.battery_pct:.0f}%")
    print("  ".join(parts))


async def run_collector(pair: bool = False) -> None:
    """Run the main data collection loop."""
    # Import Zigbee dependencies only when actually collecting data
    from .zigbee_reader import ZigbeeSensorListener

    conn = get_connection()
    logger.info("Database ready at %s", conn.execute("PRAGMA database_list").fetchone()[2])

    # Register any pre-configured sensor names
    for ieee, name in SENSOR_NAMES.items():
        upsert_sensor(conn, ieee_address=ieee, friendly_name=name)

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
        while True:
            await asyncio.sleep(POLLING_INTERVAL)
            logger.debug("Heartbeat – still listening...")

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

    args = parser.parse_args()

    if args.summary:
        get_sensor_summary()
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
