"""
Configuration for Zigbee sensor reader.

Edit these settings to match your setup:
- ZIGBEE_HOST: IP address of the Sonoff Dongle-M on your network
- ZIGBEE_PORT: TCP port (typically 8888 for EZSP-based dongles)
- DATABASE_PATH: Where to store the SQLite database
- POLLING_INTERVAL: How often to read sensors (seconds)
- SENSOR_NAMES: Friendly names for your sensors (keyed by IEEE address)
"""

import os

# Network connection for the Sonoff Zigbee Dongle-M (Ethernet)
# The dongle exposes a TCP serial socket on the network
ZIGBEE_HOST = os.environ.get("ZIGBEE_HOST", "192.168.1.59")
ZIGBEE_PORT = int(os.environ.get("ZIGBEE_PORT", "6638"))

# Connection string for zigpy/bellows (socket:// for network, COMx for USB)
# Override with ZIGBEE_DEVICE_PATH env var for custom setups
DEVICE_PATH = os.environ.get(
    "ZIGBEE_DEVICE_PATH",
    f"socket://{ZIGBEE_HOST}:{ZIGBEE_PORT}",
)
SERIAL_BAUDRATE = 115200

# Radio adapter type: "ember" (EZSP) for the Dongle-M's EFR32 chip
RADIO_TYPE = "ezsp"

# Hardware flow control (RTS/CTS) — disabled for this dongle
FLOW_CONTROL = False

# Database configuration
DATABASE_PATH = os.environ.get(
    "ZIGBEE_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "sensor_data.db"),
)

# How often to poll sensors for new data (in seconds)
POLLING_INTERVAL = int(os.environ.get("ZIGBEE_POLL_INTERVAL", "60"))

# CSV / Excel export directory
EXPORT_DIR = os.environ.get(
    "ZIGBEE_EXPORT_DIR",
    os.path.join(os.path.dirname(__file__), "..", "exports"),
)

# Friendly names for sensors. Map the Zigbee IEEE address to a room/label.
# These are discovered automatically; add friendly names here once you know
# the addresses. Run the program once and it will print discovered devices.
# Example:
#   "00:12:4b:00:25:e7:a1:c3": "Living Room",
SENSOR_NAMES: dict[str, str] = {
    "f4:b3:b1:ff:fe:60:ae:82": "Living Room",
    "a4:c1:38:0a:d3:e2:ff:ff": "Dining Room",
    "a4:c1:38:0a:ca:6f:ff:ff": "Porch",
    "a4:c1:38:0a:b8:01:ff:ff": "Guest Bedroom",
    "f4:b3:b1:ff:fe:61:0f:ea": "Ensuite",
    "a4:c1:38:0a:d9:4a:ff:ff": "Blanca Room",
    "f4:b3:b1:ff:fe:61:1b:f3": "Stellas Room",
    "f4:b3:b1:ff:fe:5e:09:d8": "Games Room",
}

# Friendly names for Hive thermostats (keyed by Hive device name)
HIVE_NAMES: dict[str, str] = {
    "Thermostat 4": "Hall",
    "Thermostat 5": "Master Bedroom",
    "Thermostat 6": "Top Floor Landing",
}

# Heating zones — groups sensors and thermostats for analysis
# Zone 1 (Thermostat 4 / Hall): Ground floor
# Zone 2 (Thermostat 5 / Master Bedroom): First floor
# Zone 3 (Thermostat 6 / Top Floor Landing): Top floor
ZONES: dict[str, str] = {
    # Zone 1 - Ground floor (controlled by Thermostat 4 / Hall)
    "f4:b3:b1:ff:fe:60:ae:82": "Zone 1",  # Living Room
    "a4:c1:38:0a:d3:e2:ff:ff": "Zone 1",  # Dining Room
    "a4:c1:38:0a:ca:6f:ff:ff": "Zone 1",  # Porch
    "Thermostat 4": "Zone 1",              # Hall thermostat
    # Zone 2 - First floor (controlled by Thermostat 5 / Master Bedroom)
    "a4:c1:38:0a:b8:01:ff:ff": "Zone 2",  # Guest Bedroom
    "f4:b3:b1:ff:fe:61:0f:ea": "Zone 2",  # Ensuite
    "Thermostat 5": "Zone 2",              # Master Bedroom thermostat
    # Zone 3 - Top floor (controlled by Thermostat 6 / Top Floor Landing)
    "a4:c1:38:0a:d9:4a:ff:ff": "Zone 3",  # Blanca Room
    "f4:b3:b1:ff:fe:61:1b:f3": "Zone 3",  # Stellas Room
    "f4:b3:b1:ff:fe:5e:09:d8": "Zone 3",  # Games Room
    "Thermostat 6": "Zone 3",              # Top Floor Landing thermostat
    # Zone 5 - Outside
    "94:B2:16:08:82:98": "Zone 5",         # Outside (Shelly Blu H&T)
}

# Shelly Blu H&T sensors (keyed by BLE MAC address, uppercase with colons)
# Run `python -m zigbee_sensor_reader --discover-shelly` to find MAC addresses
SHELLY_SENSORS: dict[str, str] = {
    "94:B2:16:08:82:98": "Outside",
}

# BLE scan duration for Shelly sensors (seconds)
# The Blu H&T advertises every ~3-10 minutes, so scan long enough to catch one
SHELLY_SCAN_DURATION = int(os.environ.get("SHELLY_SCAN_DURATION", "120"))
