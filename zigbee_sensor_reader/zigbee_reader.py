"""
Zigbee coordinator interface for the Sonoff Dongle-M.

Uses the zigpy + bellows (Silicon Labs EZSP) stack to communicate with
Sonoff SNZB-02 / SNZB-02D / SNZB-02P temperature & humidity sensors.

The Sonoff Dongle-M uses an EFR32MG21 chip which speaks the EZSP protocol.
"""

import asyncio
import logging
from typing import Any, Callable

import zigpy.config as zigpy_conf
import zigpy.types as zigpy_types
from zigpy.application import ControllerApplication
from zigpy.zcl.clusters.measurement import (
    RelativeHumidity,
    TemperatureMeasurement,
)
from zigpy.zcl.clusters.general import PowerConfiguration

from .config import DEVICE_PATH, SERIAL_BAUDRATE, FLOW_CONTROL, SENSOR_NAMES

logger = logging.getLogger(__name__)


class SensorReading:
    """Container for a single sensor report."""

    def __init__(
        self,
        ieee_address: str,
        friendly_name: str,
        model: str | None = None,
        temperature_c: float | None = None,
        humidity_pct: float | None = None,
        battery_pct: float | None = None,
        link_quality: int | None = None,
    ):
        self.ieee_address = ieee_address
        self.friendly_name = friendly_name
        self.model = model
        self.temperature_c = temperature_c
        self.humidity_pct = humidity_pct
        self.battery_pct = battery_pct
        self.link_quality = link_quality

    def __repr__(self) -> str:
        return (
            f"SensorReading({self.friendly_name}: "
            f"temp={self.temperature_c}°C, "
            f"humidity={self.humidity_pct}%, "
            f"battery={self.battery_pct}%)"
        )


# Callback type: called whenever a new reading arrives
ReadingCallback = Callable[[SensorReading], None]


class ZigbeeSensorListener:
    """
    Listens for attribute reports from Zigbee temperature/humidity sensors.

    Uses zigpy with the bellows radio library (EZSP protocol) which is what
    the Sonoff Dongle-M requires.
    """

    def __init__(self, on_reading: ReadingCallback | None = None):
        self.on_reading = on_reading
        self.app: ControllerApplication | None = None

    def _get_zigpy_config(self) -> dict:
        """Build the zigpy configuration dict for bellows/EZSP (ember adapter)."""
        return {
            zigpy_conf.CONF_DATABASE: "zigbee_network.db",
            zigpy_conf.CONF_DEVICE: {
                zigpy_conf.CONF_DEVICE_PATH: DEVICE_PATH,
            },
            zigpy_conf.CONF_OTA: {
                zigpy_conf.CONF_OTA_ENABLED: False,
            },
        }

    async def start(self) -> None:
        """Start the Zigbee coordinator and begin listening for reports."""
        from bellows.zigbee.application import ControllerApplication as BellowsApp

        config = self._get_zigpy_config()

        # Pass raw config — BellowsApp.new() validates internally
        # auto_form=True creates a new Zigbee network on first run
        self.app = await BellowsApp.new(config, auto_form=True)

        # Register application-level listener for join/leave events
        self.app.add_listener(self)

        # Register cluster-level listeners on all known devices
        self._register_cluster_listeners()

        logger.info(
            "Zigbee coordinator started on %s. Network channel: %s",
            DEVICE_PATH,
            self.app.state.network_info.channel,
        )
        logger.info(
            "Coordinator IEEE: %s", self.app.state.node_info.ieee
        )

        # Log all known devices
        for ieee, dev in self.app.devices.items():
            friendly = SENSOR_NAMES.get(str(ieee), str(ieee))
            model = getattr(dev, "model", None)
            logger.info("Known device: %s (%s) model=%s", friendly, ieee, model)

    def _register_cluster_listeners(self) -> None:
        """Add listeners on every relevant cluster of every device."""
        target_clusters = (
            TemperatureMeasurement.cluster_id,
            RelativeHumidity.cluster_id,
            PowerConfiguration.cluster_id,
        )
        for ieee, device in self.app.devices.items():
            for ep_id, endpoint in device.endpoints.items():
                if ep_id == 0:
                    continue  # skip ZDO
                for cluster_id in target_clusters:
                    if cluster_id in endpoint.in_clusters:
                        cluster = endpoint.in_clusters[cluster_id]
                        cluster.add_listener(_ClusterListener(self, cluster))
                        logger.debug(
                            "Registered listener on %s cluster 0x%04X",
                            SENSOR_NAMES.get(str(ieee), str(ieee)),
                            cluster_id,
                        )

    async def stop(self) -> None:
        """Shut down the Zigbee coordinator cleanly."""
        if self.app:
            await self.app.shutdown()
            logger.info("Zigbee coordinator stopped.")

    async def permit_join(self, duration: int = 60) -> None:
        """
        Open the network for new devices to join.

        Put your sensor in pairing mode (hold the button for 5+ seconds)
        while the network is open.
        """
        if self.app:
            await self.app.permit(duration)
            logger.info("Network open for joining (%d seconds).", duration)


class _ClusterListener:
    """Lightweight listener attached to a single cluster, forwarding to the main handler."""

    def __init__(self, parent: "ZigbeeSensorListener", cluster):
        self.parent = parent
        self.cluster = cluster

    def attribute_updated(self, attrid, value, *args):
        """Called by zigpy when this cluster receives an attribute report."""
        self.parent._handle_attribute(self.cluster, attrid, value)

    def cluster_command(self, *args, **kwargs):
        pass

    # ── zigpy listener callbacks ──────────────────────────────────────

    def _handle_attribute(self, cluster, attrid, value) -> None:
        """Process an attribute report from a cluster listener."""
        device = cluster.endpoint.device
        ieee = str(device.ieee)
        friendly = SENSOR_NAMES.get(ieee, ieee)
        model = getattr(device, "model", None)

        reading = SensorReading(
            ieee_address=ieee,
            friendly_name=friendly,
            model=model,
        )

        if cluster.cluster_id == TemperatureMeasurement.cluster_id:
            # ZCL temperature is in units of 0.01°C
            reading.temperature_c = value / 100.0
            logger.info("%s temperature: %.1f°C", friendly, reading.temperature_c)

        elif cluster.cluster_id == RelativeHumidity.cluster_id:
            # ZCL humidity is in units of 0.01%
            reading.humidity_pct = value / 100.0
            logger.info("%s humidity: %.1f%%", friendly, reading.humidity_pct)

        elif cluster.cluster_id == PowerConfiguration.cluster_id:
            # Battery percentage remaining
            # ZCL reports battery percentage as 0-200 (0.5% steps)
            reading.battery_pct = value / 2.0
            logger.info("%s battery: %.0f%%", friendly, reading.battery_pct)

        else:
            return  # Ignore clusters we don't care about

        if self.on_reading:
            self.on_reading(reading)

    def device_joined(self, device) -> None:
        """Called when a new device joins the network."""
        ieee = str(device.ieee)
        model = getattr(device, "model", None)
        logger.info("New device joined: %s (model=%s)", ieee, model)
        logger.info(
            "Add this to SENSOR_NAMES in config.py:\n"
            '    "%s": "Room Name",',
            ieee,
        )

    def device_left(self, device) -> None:
        """Called when a device leaves the network."""
        logger.info("Device left: %s", device.ieee)


async def poll_sensors(app: ControllerApplication) -> list[SensorReading]:
    """
    Actively request current values from all known sensors.

    Most Sonoff SNZB sensors report periodically on their own, but this
    can be used to force an immediate read.
    """
    readings = []

    for ieee, device in app.devices.items():
        friendly = SENSOR_NAMES.get(str(ieee), str(ieee))
        model = getattr(device, "model", None)

        for ep_id, endpoint in device.endpoints.items():
            if ep_id == 0:
                continue  # skip ZDO endpoint

            reading = SensorReading(
                ieee_address=str(ieee),
                friendly_name=friendly,
                model=model,
            )

            # Read temperature
            if TemperatureMeasurement.cluster_id in endpoint.in_clusters:
                cluster = endpoint.in_clusters[TemperatureMeasurement.cluster_id]
                try:
                    result = await cluster.read_attributes(["measured_value"])
                    val = result[0].get("measured_value")
                    if val is not None:
                        reading.temperature_c = val / 100.0
                except Exception as e:
                    logger.warning("Failed to read temperature from %s: %s", friendly, e)

            # Read humidity
            if RelativeHumidity.cluster_id in endpoint.in_clusters:
                cluster = endpoint.in_clusters[RelativeHumidity.cluster_id]
                try:
                    result = await cluster.read_attributes(["measured_value"])
                    val = result[0].get("measured_value")
                    if val is not None:
                        reading.humidity_pct = val / 100.0
                except Exception as e:
                    logger.warning("Failed to read humidity from %s: %s", friendly, e)

            # Read battery
            if PowerConfiguration.cluster_id in endpoint.in_clusters:
                cluster = endpoint.in_clusters[PowerConfiguration.cluster_id]
                try:
                    result = await cluster.read_attributes(["battery_percentage_remaining"])
                    val = result[0].get("battery_percentage_remaining")
                    if val is not None:
                        reading.battery_pct = val / 2.0
                except Exception as e:
                    logger.warning("Failed to read battery from %s: %s", friendly, e)

            if reading.temperature_c is not None or reading.humidity_pct is not None:
                readings.append(reading)

    return readings


def read_cached_sensors(app: ControllerApplication) -> list[SensorReading]:
    """
    Read last-known values from the zigpy attribute cache.

    This is instant (no network I/O) — reads whatever the sensors last
    reported. Sensors update the cache every 10-20 minutes on their own.
    """
    readings = []

    for ieee, device in app.devices.items():
        ieee_str = str(ieee)
        if ieee_str not in SENSOR_NAMES:
            continue

        friendly = SENSOR_NAMES[ieee_str]
        model = getattr(device, "model", None)

        for ep_id, endpoint in device.endpoints.items():
            if ep_id == 0:
                continue

            temp = None
            humidity = None
            battery = None

            if TemperatureMeasurement.cluster_id in endpoint.in_clusters:
                cluster = endpoint.in_clusters[TemperatureMeasurement.cluster_id]
                val = cluster.get(0x0000)  # measured_value
                if val is not None:
                    temp = val / 100.0

            if RelativeHumidity.cluster_id in endpoint.in_clusters:
                cluster = endpoint.in_clusters[RelativeHumidity.cluster_id]
                val = cluster.get(0x0000)
                if val is not None:
                    humidity = val / 100.0

            if PowerConfiguration.cluster_id in endpoint.in_clusters:
                cluster = endpoint.in_clusters[PowerConfiguration.cluster_id]
                val = cluster.get(0x0021)  # battery_percentage_remaining
                if val is not None:
                    battery = val / 2.0

            if temp is not None or humidity is not None:
                readings.append(SensorReading(
                    ieee_address=ieee_str,
                    friendly_name=friendly,
                    model=model,
                    temperature_c=temp,
                    humidity_pct=humidity,
                    battery_pct=battery,
                ))

    return readings
