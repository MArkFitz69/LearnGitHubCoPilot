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
ZIGBEE_PORT = int(os.environ.get("ZIGBEE_PORT", "8888"))

# Connection string for zigpy/bellows (socket:// for network, COMx for USB)
# Override with ZIGBEE_DEVICE_PATH env var for custom setups
DEVICE_PATH = os.environ.get(
    "ZIGBEE_DEVICE_PATH",
    f"socket://{ZIGBEE_HOST}:{ZIGBEE_PORT}",
)
SERIAL_BAUDRATE = 115200

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
    # "00:12:4b:00:xx:xx:xx:xx": "Kitchen",
    # "00:12:4b:00:yy:yy:yy:yy": "Bedroom",
}
