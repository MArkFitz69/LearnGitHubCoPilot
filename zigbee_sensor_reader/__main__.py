"""Command-line entry point and long-running sensor collector."""

import argparse
import asyncio
import logging
import time

from .config import (
    ESP32_SENSOR_NAMES,
    ESP32_SENSOR_ZONES,
    POLLING_INTERVAL,
    SENSOR_NAMES,
    SHELLY_AUTHORITATIVE_SOURCES,
    SHELLY_SENSORS,
    ZONES,
)
from .database import (
    get_connection,
    insert_reading,
    record_mqtt_device_activity,
    upsert_sensor,
)
from .export import export_to_csv, export_to_excel, get_sensor_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _get_sensor_metadata(conn, ieee_address: str):
    return conn.execute(
        """
        SELECT friendly_name, zone, zone_override
        FROM sensors WHERE ieee_address=?
        """,
        (ieee_address,),
    ).fetchone()


def handle_reading(reading, conn=None) -> bool:
    """Store one callback reading using a connection owned by this thread."""
    owns_connection = conn is None
    if owns_connection:
        conn = get_connection()
    try:
        existing = _get_sensor_metadata(conn, reading.ieee_address)
        zone = ZONES.get(reading.ieee_address)
        if not zone and existing:
            zone = existing["zone_override"] or existing["zone"]
        if getattr(reading, "zone", None):
            zone = reading.zone
        friendly_name = reading.friendly_name
        if (
            (not friendly_name or friendly_name == reading.ieee_address)
            and existing
        ):
            friendly_name = existing["friendly_name"] or reading.friendly_name
        source = getattr(reading, "source", "z2m")
        upsert_sensor(
            conn,
            ieee_address=reading.ieee_address,
            friendly_name=friendly_name,
            model=reading.model,
            zone=(
                ZONES.get(reading.ieee_address)
                if source == "z2m"
                else zone
            ),
            zone_override=(
                getattr(reading, "zone", None)
                or (existing["zone_override"] if existing else None)
                if source == "z2m"
                else None
            ),
            name_source="z2m" if source == "z2m" else "config",
        )

        mac = (
            reading.ieee_address.removeprefix("shelly:")
            if reading.ieee_address.startswith("shelly:")
            else None
        )
        stored = insert_reading(
            conn,
            ieee_address=reading.ieee_address,
            temperature_c=reading.temperature_c,
            humidity_pct=reading.humidity_pct,
            battery_pct=reading.battery_pct,
            link_quality=reading.link_quality,
            zone=zone,
            state=getattr(reading, "state", None),
            power_w=getattr(reading, "power_w", None),
            energy_kwh=getattr(reading, "energy_kwh", None),
            device_min_temp_c=getattr(reading, "device_min_temp_c", None),
            device_max_temp_c=getattr(reading, "device_max_temp_c", None),
            device_min_humidity_pct=getattr(
                reading, "device_min_humidity_pct", None
            ),
            device_max_humidity_pct=getattr(
                reading, "device_max_humidity_pct", None
            ),
            battery_voltage_mv=getattr(reading, "battery_voltage_mv", None),
            packet_id=getattr(reading, "packet_id", None),
            source=source,
            authoritative_source=(
                SHELLY_AUTHORITATIVE_SOURCES.get(mac) if mac else None
            ),
        )
        if not stored:
            logger.debug(
                "Skipped duplicate, stale, or non-authoritative packet %s for %s",
                getattr(reading, "packet_id", None),
                reading.ieee_address,
            )
            return False

        parts = [f"[{friendly_name}]"]
        if zone:
            parts.append(f"({zone})")
        if reading.temperature_c is not None:
            parts.append(f"Temp: {reading.temperature_c:.1f}°C")
        if reading.humidity_pct is not None:
            parts.append(f"Humidity: {reading.humidity_pct:.1f}%")
        if reading.battery_pct is not None:
            parts.append(f"Battery: {reading.battery_pct:.0f}%")
        if getattr(reading, "state", None):
            parts.append(f"State: {reading.state}")
        if getattr(reading, "power_w", None) is not None:
            parts.append(f"Power: {reading.power_w:.2f}W")
        if getattr(reading, "energy_kwh", None) is not None:
            parts.append(f"Energy: {reading.energy_kwh:.3f}kWh")
        print("  ".join(parts))
        return True
    finally:
        if owns_connection:
            conn.close()


def handle_esp32_activity(activity) -> None:
    """Persist ESP32 MQTT health using a callback-thread-owned connection."""
    conn = get_connection()
    try:
        record_mqtt_device_activity(
            conn,
            device_key=activity.device_key,
            topic_prefix=activity.topic_prefix,
            topic=activity.topic,
            retained=activity.retained,
            reported_status=activity.reported_status,
            is_sensor_publication=activity.is_sensor_publication,
        )
    finally:
        conn.close()


async def run_collector() -> None:
    """Run independent MQTT callbacks alongside periodic Hive/BLE polling."""
    conn = get_connection()
    logger.info(
        "Database ready at %s",
        conn.execute("PRAGMA database_list").fetchone()[2],
    )
    for ieee, name in SENSOR_NAMES.items():
        upsert_sensor(
            conn,
            ieee_address=ieee,
            friendly_name=name,
            zone=ZONES.get(ieee),
        )
    for mac, name in SHELLY_SENSORS.items():
        upsert_sensor(
            conn,
            ieee_address=f"shelly:{mac}",
            friendly_name=name,
            model="Shelly Blu H&T",
            zone=ZONES.get(mac),
        )
    from .esp32_mqtt_reader import sensor_identity
    for sensor_key, name in ESP32_SENSOR_NAMES.items():
        upsert_sensor(
            conn,
            ieee_address=sensor_identity(sensor_key),
            friendly_name=name,
            model=(
                "Shelly Blu H&T"
                if sensor_key == "shelly_raw_payload"
                else "ESP32 Dallas Temperature"
            ),
            zone=ESP32_SENSOR_ZONES.get(sensor_key),
        )
    conn.close()

    from .esp32_mqtt_reader import run_esp32_mqtt_reader
    from .z2m_reader import run_z2m_reader

    mqtt_tasks = [
        asyncio.create_task(
            run_z2m_reader(
                on_reading=handle_reading,
                get_conn_fn=get_connection,
            )
        ),
        asyncio.create_task(
            run_esp32_mqtt_reader(
                on_reading=handle_reading,
                on_activity=handle_esp32_activity,
            )
        ),
    ]
    logger.info("Zigbee2MQTT and ESP32 MQTT readers started")

    try:
        next_poll = 0.0
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            if now < next_poll:
                continue
            next_poll = now + POLLING_INTERVAL
            try:
                from .hive_reader import HIVE_USERNAME, poll_hive
                if HIVE_USERNAME:
                    await poll_hive()
            except Exception as exc:
                logger.warning("Hive poll failed: %s", exc)
            try:
                from .config import SHELLY_SCAN_DURATION
                from .shelly_ble_reader import poll_shelly_ble
                await poll_shelly_ble(scan_duration=SHELLY_SCAN_DURATION)
            except ImportError:
                logger.info("bleak is not installed; direct Shelly BLE is disabled")
            except Exception as exc:
                logger.warning("Shelly BLE poll failed: %s", exc)
    finally:
        for task in mqtt_tasks:
            task.cancel()
        await asyncio.gather(*mqtt_tasks, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Home sensor reader using Zigbee2MQTT, ESP32 MQTT, Hive, and BLE",
    )
    parser.add_argument("--export", choices=["csv", "xlsx"])
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--sensor")
    parser.add_argument("--hive", action="store_true")
    parser.add_argument("--discover-shelly", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.discover_shelly:
        from .shelly_ble_reader import discover_shelly_sensors
        results = asyncio.run(discover_shelly_sensors(scan_duration=30.0))
        for sensor in results:
            print(
                f"{sensor['mac']}: {sensor.get('temperature', '?')}°C "
                f"{sensor.get('humidity', '?')}% "
                f"battery {sensor.get('battery', '?')}%"
            )
        return
    if args.hive:
        from .hive_reader import HIVE_USERNAME, run_hive_poll
        if not HIVE_USERNAME:
            print("Set HIVE_USERNAME and HIVE_PASSWORD environment variables first.")
            return
        data = run_hive_poll()
        for reading in data.get("heating", []):
            print(
                f"{reading['name']}: {reading['temperature_c']}°C "
                f"(target: {reading['target_temp_c']}°C, mode: {reading['mode']})"
            )
        for reading in data.get("hotwater", []):
            print(
                f"{reading['name']}: {'ON' if reading['hw_on'] else 'OFF'} "
                f"(mode: {reading['mode']})"
            )
        return
    if args.summary:
        get_sensor_summary()
        return
    if args.serve:
        from .config import WEB_HOST
        from .web_server import run_server
        run_server(host=WEB_HOST, port=args.port)
        return
    if args.export:
        exporter = export_to_csv if args.export == "csv" else export_to_excel
        exporter(
            start_date=args.start,
            end_date=args.end,
            sensor_ieee=args.sensor,
        )
        return
    asyncio.run(run_collector())


if __name__ == "__main__":
    main()
