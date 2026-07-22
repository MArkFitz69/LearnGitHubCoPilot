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

        # Register our listener for device attribute reports
        self.app.add_listener(self)

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

    # ── zigpy listener callbacks ──────────────────────────────────────

    def attribute_updated(self, cluster, attrid, value) -> None:
        """Called by zigpy when a device reports an attribute change."""
        device = cluster.endpoint.device
        ieee = str(device.ieee)
        friendly = SENSOR_NAMES.get(ieee, ieee)
        model = getattr(device, "model", None)

        reading = SensorReading(
            ieee_address=ieee,
            friendly_name=friendly,
            model=model,
        )

        if isinstance(cluster, TemperatureMeasurement):
            # ZCL temperature is in units of 0.01°C
            reading.temperature_c = value / 100.0
            logger.info("%s temperature: %.1f°C", friendly, reading.temperature_c)

        elif isinstance(cluster, RelativeHumidity):
            # ZCL humidity is in units of 0.01%
            reading.humidity_pct = value / 100.0
            logger.info("%s humidity: %.1f%%", friendly, reading.humidity_pct)

        elif isinstance(cluster, PowerConfiguration):
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
