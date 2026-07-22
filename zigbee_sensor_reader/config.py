"""
Configuration for Zigbee sensor reader.

Edit these settings to match your setup:
- SERIAL_PORT: The COM port or /dev/tty path for your Sonoff Dongle-M
- DATABASE_PATH: Where to store the SQLite database
- POLLING_INTERVAL: How often to read sensors (seconds)
- SENSOR_NAMES: Friendly names for your sensors (keyed by IEEE address)
"""

import os

# Serial port for the Sonoff Zigbee Dongle-M
# Windows: "COM3", "COM4", etc. (check Device Manager)
# Linux:   "/dev/ttyUSB0" or "/dev/ttyACM0"
# macOS:   "/dev/tty.usbserial-xxxx"
SERIAL_PORT = os.environ.get("ZIGBEE_SERIAL_PORT", "COM3")
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
