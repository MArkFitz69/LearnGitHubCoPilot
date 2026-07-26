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
import time
from datetime import datetime

from .config import (
    POLLING_INTERVAL,
    SENSOR_NAMES,
    ZIGBEE_BACKEND,
    ZIGBEE_HEARTBEAT_STALE_SECONDS,
    ZIGBEE_PERIODIC_LOG_INTERVAL_SECONDS,
    ZIGBEE_ACTIVE_POLL_INTERVAL,
    ZIGBEE_ACTIVE_POLL_ON_STALE_CACHE,
    ZONES,
)
from .database import (
    get_connection,
    insert_reading,
    insert_zigbee_frame_event,
    upsert_sensor,
)
from .export import export_to_csv, export_to_excel, get_sensor_summary
from .onboarding import (
    fetch_pending_commands,
    mark_command_done,
    mark_command_failed,
    maybe_close_expired_pairing,
    record_device_joined,
    record_first_reading,
    set_pairing_state,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _reading_signature(reading) -> tuple:
    """Build a comparable value signature for de-duplicating cached readings."""
    return (
        reading.temperature_c,
        reading.humidity_pct,
        reading.battery_pct,
    )


def _load_last_signatures(conn) -> dict[str, tuple]:
    """Load the latest stored value signature per IEEE from the database."""
    rows = conn.execute(
        """
        SELECT r.ieee_address,
               r.temperature_c,
               r.humidity_pct,
               r.battery_pct
        FROM readings r
        JOIN (
            SELECT ieee_address, MAX(id) AS max_id
            FROM readings
            GROUP BY ieee_address
        ) latest ON latest.max_id = r.id
        """
    ).fetchall()
    return {
        row["ieee_address"]: (
            row["temperature_c"],
            row["humidity_pct"],
            row["battery_pct"],
        )
        for row in rows
    }


def _load_last_insert_timestamps(conn) -> dict[str, str]:
    """Load latest reading timestamp per IEEE."""
    rows = conn.execute(
        """
        SELECT ieee_address, MAX(timestamp) AS last_ts
        FROM readings
        GROUP BY ieee_address
        """
    ).fetchall()
    return {row["ieee_address"]: row["last_ts"] for row in rows if row["last_ts"]}


def _load_last_heartbeat_timestamps(conn) -> dict[str, str]:
    """Load latest Zigbee frame timestamp per IEEE."""
    rows = conn.execute(
        """
        SELECT ieee_address, MAX(recorded_at) AS last_frame_at
        FROM zigbee_frame_events
        GROUP BY ieee_address
        """
    ).fetchall()
    return {row["ieee_address"]: row["last_frame_at"] for row in rows if row["last_frame_at"]}


def _seconds_since(timestamp_iso: str | None) -> int | None:
    if not timestamp_iso:
        return None
    try:
        then = datetime.fromisoformat(timestamp_iso)
    except ValueError:
        return None
    return max(0, int((datetime.now() - then).total_seconds()))


def _get_sensor_metadata(conn, ieee_address: str):
    return conn.execute(
        "SELECT friendly_name, zone FROM sensors WHERE ieee_address = ?",
        (ieee_address,),
    ).fetchone()


def handle_reading(
    reading,
    conn,
    reading_source: str | None = None,
    source_event_age_seconds: int | None = None,
    is_stale: bool | None = None,
) -> None:
    """Process an incoming sensor reading: store in DB and print to console."""
    existing = _get_sensor_metadata(conn, reading.ieee_address)
    zone = ZONES.get(reading.ieee_address) or (existing["zone"] if existing else None)
    friendly_name = reading.friendly_name
    if (not friendly_name or friendly_name == reading.ieee_address) and existing:
        friendly_name = existing["friendly_name"] or reading.friendly_name
    upsert_sensor(
        conn,
        ieee_address=reading.ieee_address,
        friendly_name=friendly_name,
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
        reading_source=reading_source,
        source_event_age_seconds=source_event_age_seconds,
        is_stale=is_stale,
    )
    record_first_reading(conn, reading.ieee_address)

    parts = [f"[{friendly_name}]"]
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


def _handle_device_joined(conn, ieee_address: str, model: str | None) -> None:
    upsert_sensor(
        conn,
        ieee_address=ieee_address,
        model=model,
    )
    record_device_joined(conn, ieee_address, model)


async def _recover_listener_for_pairing(listener) -> None:
    """Reinitialize the Zigbee listener to recover from stale EZSP transport state."""
    try:
        await listener.stop()
    except Exception as exc:
        logger.warning("Listener stop during pairing recovery failed: %s", exc)
    await asyncio.sleep(1)
    await listener.start()


async def _process_onboarding_commands(listener, conn) -> None:
    maybe_close_expired_pairing(conn)
    for row in fetch_pending_commands(conn):
        command_id = row["id"]
        command = row["command"]
        if command != "start_pairing":
            mark_command_failed(conn, command_id, f"Unknown command: {command}")
            continue

        last_error = None
        for attempt in (1, 2):
            try:
                await listener.permit_join(duration=120)
                set_pairing_state(conn, active=True, duration_seconds=120)
                mark_command_done(conn, command_id)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                logger.warning("permit_join attempt %d failed: %s", attempt, exc)
                if attempt == 1:
                    await _recover_listener_for_pairing(listener)

        if last_error is not None:
            mark_command_failed(conn, command_id, f"permit_join failed: {last_error}")


async def run_collector(pair: bool = False) -> None:
    """Run the main data collection loop."""
    conn = get_connection()
    logger.info("Database ready at %s", conn.execute("PRAGMA database_list").fetchone()[2])
    logger.info("Zigbee backend mode: %s", ZIGBEE_BACKEND)

    # Register any pre-configured sensor names
    for ieee, name in SENSOR_NAMES.items():
        upsert_sensor(conn, ieee_address=ieee, friendly_name=name, zone=ZONES.get(ieee))

    last_signatures = _load_last_signatures(conn)
    last_insert_timestamps = _load_last_insert_timestamps(conn)
    last_heartbeat_timestamps = _load_last_heartbeat_timestamps(conn)
    heartbeat_bootstrap_mode = len(last_heartbeat_timestamps) == 0

    def _on_frame_event(event: dict) -> None:
        nonlocal heartbeat_bootstrap_mode
        try:
            ieee_address = event.get("ieee_address")
            if not ieee_address:
                return
            insert_zigbee_frame_event(
                conn,
                ieee_address=ieee_address,
                friendly_name=event.get("friendly_name"),
                endpoint_id=event.get("endpoint_id"),
                cluster_id=event.get("cluster_id"),
                attribute_id=event.get("attribute_id"),
                value_text=event.get("value_text"),
                aps_timestamp=event.get("aps_timestamp"),
                zigbee_sequence=event.get("zigbee_sequence"),
                lqi=event.get("lqi"),
                rssi=event.get("rssi"),
                source=event.get("source"),
            )
            last_heartbeat_timestamps[ieee_address] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            heartbeat_bootstrap_mode = False
        except Exception as exc:
            logger.debug("Failed to store Zigbee frame event: %s", exc)

    def _on_reading(reading) -> None:
        heartbeat_age = _seconds_since(last_heartbeat_timestamps.get(reading.ieee_address))
        is_stale = heartbeat_age is None or heartbeat_age > ZIGBEE_HEARTBEAT_STALE_SECONDS
        handle_reading(
            reading,
            conn,
            reading_source="value_change",
            source_event_age_seconds=heartbeat_age,
            is_stale=is_stale,
        )
        last_signatures[reading.ieee_address] = _reading_signature(reading)
        last_insert_timestamps[reading.ieee_address] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    if ZIGBEE_BACKEND == "z2m":
        from .zigbee2mqtt_reader import Zigbee2MqttListener

        listener = Zigbee2MqttListener()
        using_z2m = True
    else:
        from .zigbee_reader import ZigbeeSensorListener

        listener = ZigbeeSensorListener(
            on_reading=_on_reading,
            on_device_joined=lambda ieee, model: _handle_device_joined(conn, ieee, model),
            on_frame_event=_on_frame_event,
        )
        using_z2m = False

    def _drain_z2m_events() -> None:
        if not using_z2m:
            return
        for event_type, payload in listener.drain_events():
            if event_type == "device":
                ieee = payload.get("ieee_address")
                if not ieee:
                    continue
                upsert_sensor(
                    conn,
                    ieee_address=ieee,
                    friendly_name=payload.get("friendly_name"),
                    model=payload.get("model"),
                    zone=ZONES.get(ieee),
                )
                continue
            if event_type == "frame":
                _on_frame_event(payload)
                continue
            if event_type == "reading":
                reading = payload.get("reading")
                if reading:
                    _on_reading(reading)

    try:
        await listener.start()
        if using_z2m:
            _drain_z2m_events()
        else:
            # Ensure all currently known Zigbee devices are present in the registry,
            # including non-sensor routers (e.g. smart plugs) that may not emit
            # temperature/humidity readings.
            for ieee, device in listener.app.devices.items():
                ieee_str = str(ieee)
                upsert_sensor(
                    conn,
                    ieee_address=ieee_str,
                    friendly_name=SENSOR_NAMES.get(ieee_str),
                    model=getattr(device, "model", None),
                    zone=ZONES.get(ieee_str),
                )

        if pair:
            logger.info(
                "Pairing mode: network is open for 120 seconds. "
                "Put your sensor in pairing mode now (hold button 5+ seconds)."
            )
            await listener.permit_join(duration=120)
            set_pairing_state(conn, active=True, duration_seconds=120)

        logger.info(
            "Collecting sensor data (Ctrl+C to stop). "
            "Polling interval: %ds",
            POLLING_INTERVAL,
        )

        next_sensor_poll = 0.0
        next_forced_poll = 0.0
        while True:
            _drain_z2m_events()
            await _process_onboarding_commands(listener, conn)

            now = time.monotonic()
            if now < next_sensor_poll:
                await asyncio.sleep(1)
                continue

            next_sensor_poll = now + POLLING_INTERVAL

            # Read cached Zigbee sensor values and store them
            try:
                if using_z2m:
                    cached = listener.read_cached_sensors()
                else:
                    from .zigbee_reader import read_cached_sensors

                    cached = read_cached_sensors(listener.app)
                stored = 0
                skipped = 0
                periodic = 0
                stale_skipped = 0
                for reading in cached:
                    signature = _reading_signature(reading)
                    previous = last_signatures.get(reading.ieee_address)
                    heartbeat_age = _seconds_since(last_heartbeat_timestamps.get(reading.ieee_address))
                    bootstrap_age = _seconds_since(last_insert_timestamps.get(reading.ieee_address))
                    heartbeat_ok = (
                        (heartbeat_age is not None and heartbeat_age <= ZIGBEE_HEARTBEAT_STALE_SECONDS)
                        or (
                            heartbeat_bootstrap_mode
                            and bootstrap_age is not None
                            and bootstrap_age <= ZIGBEE_HEARTBEAT_STALE_SECONDS
                        )
                    )
                    is_stale = not heartbeat_ok

                    if previous != signature:
                        handle_reading(
                            reading,
                            conn,
                            reading_source="value_change",
                            source_event_age_seconds=heartbeat_age,
                            is_stale=is_stale,
                        )
                        last_signatures[reading.ieee_address] = signature
                        last_insert_timestamps[reading.ieee_address] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                        stored += 1
                        continue

                    skipped += 1
                    elapsed = _seconds_since(last_insert_timestamps.get(reading.ieee_address))
                    if elapsed is None or elapsed < ZIGBEE_PERIODIC_LOG_INTERVAL_SECONDS:
                        continue

                    if heartbeat_ok:
                        handle_reading(
                            reading,
                            conn,
                            reading_source="heartbeat_confirmed",
                            source_event_age_seconds=heartbeat_age,
                            is_stale=False,
                        )
                        last_insert_timestamps[reading.ieee_address] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                        periodic += 1
                    else:
                        stale_skipped += 1
                if cached:
                    logger.info(
                        "Stored %d Zigbee sensor readings (changed), stored %d heartbeat-confirmed, skipped %d unchanged cached, stale-suppressed %d",
                        stored,
                        periodic,
                        skipped,
                        stale_skipped,
                    )

                # If every cached sensor is unchanged for this cycle, attempt an
                # active network read (throttled) to refresh potentially stale cache.
                if (
                    not using_z2m
                    and
                    ZIGBEE_ACTIVE_POLL_ON_STALE_CACHE
                    and
                    cached
                    and skipped == len(cached)
                    and now >= next_forced_poll
                ):
                    from .zigbee_reader import poll_sensors

                    forced_checked = 0
                    forced_stored = 0
                    active_readings = await poll_sensors(listener.app)
                    for reading in active_readings:
                        forced_checked += 1
                        signature = _reading_signature(reading)
                        previous = last_signatures.get(reading.ieee_address)
                        if previous == signature:
                            continue
                        heartbeat_age = _seconds_since(last_heartbeat_timestamps.get(reading.ieee_address))
                        is_stale = heartbeat_age is None or heartbeat_age > ZIGBEE_HEARTBEAT_STALE_SECONDS
                        handle_reading(
                            reading,
                            conn,
                            reading_source="active_poll_change",
                            source_event_age_seconds=heartbeat_age,
                            is_stale=is_stale,
                        )
                        last_signatures[reading.ieee_address] = signature
                        last_insert_timestamps[reading.ieee_address] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                        forced_stored += 1

                    next_forced_poll = time.monotonic() + ZIGBEE_ACTIVE_POLL_INTERVAL
                    logger.info(
                        "Forced Zigbee poll checked %d sensors, stored %d changed readings",
                        forced_checked,
                        forced_stored,
                    )
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
