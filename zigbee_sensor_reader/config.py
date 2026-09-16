"""
Configuration for the home sensor collector.

Edit these settings to match your setup:
- DATABASE_PATH: Where to store the SQLite database
- POLLING_INTERVAL: How often to read sensors (seconds)
- SENSOR_NAMES: Friendly names for your sensors (keyed by IEEE address)
"""

import os

# Database configuration
DATABASE_PATH = os.environ.get(
    "ZIGBEE_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "sensor_data.db"),
)

# How often to poll sensors for new data (in seconds)
POLLING_INTERVAL = int(os.environ.get("ZIGBEE_POLL_INTERVAL", "60"))
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")

# CSV / Excel export directory
EXPORT_DIR = os.environ.get(
    "ZIGBEE_EXPORT_DIR",
    os.path.join(os.path.dirname(__file__), "..", "exports"),
)

# Friendly names for sensors. Map the Zigbee IEEE address to a room/label.
# Zigbee2MQTT supplies names automatically; entries here are local fallbacks.
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
    # Zone 4 - Outdoor
    "94:B2:16:08:82:98": "Zone 4",          # Outdoor (Shelly Blu H&T)
    # Zone 5 - Attic
    "FC:4D:6A:1D:1D:FB": "Zone 5",          # Attic (Shelly Blu H&T)
}

# Shelly Blu H&T sensors (keyed by BLE MAC address, uppercase with colons)
# Run `python -m zigbee_sensor_reader --discover-shelly` to find MAC addresses
SHELLY_SENSORS: dict[str, str] = {
    "94:B2:16:08:82:98": "Outdoor",
    "FC:4D:6A:1D:1D:FB": "Attic",
}

# Known alternate identities published by other gateways. Values are the
# canonical database identities shared with direct BLE collection.
SHELLY_IDENTITY_ALIASES: dict[str, str] = {
    "fc:4d:6a:ff:fe:1d:1d:fb": "shelly:FC:4D:6A:1D:1D:FB",
}

# Preferred BTHome source per physical Shelly. A different source is accepted
# only when the preferred source has been silent for the fallback interval.
SHELLY_AUTHORITATIVE_SOURCES: dict[str, str] = {
    "94:B2:16:08:82:98": "esp32",
    "FC:4D:6A:1D:1D:FB": "ble",
}
SHELLY_SOURCE_FALLBACK_SECONDS = int(
    os.environ.get("SHELLY_SOURCE_FALLBACK_SECONDS", "1800")
)
SHELLY_HEARTBEAT_REPEAT_SECONDS = max(
    1,
    int(os.environ.get("SHELLY_HEARTBEAT_REPEAT_SECONDS", "900")),
)

# ESP32 MQTT sensor feed. The reader uses the Zigbee2MQTT broker connection
# settings by default; only the ESPHome node/topic prefix normally needs to be
# changed.
ESP32_TOPIC_PREFIX = (
    os.environ.get("ESP32_TOPIC_PREFIX", "heating-esp").strip("/") or "heating-esp"
)
ESP32_MQTT_STALE_MINUTES = max(
    1,
    int(os.environ.get("ESP32_MQTT_STALE_MINUTES", "30")),
)

# Canonical sensor keys and visible names. The firmware topic typo
# "bolier_1_return" is mapped to the canonical "boiler1_return" identity by the
# MQTT reader.
ESP32_SENSOR_NAMES: dict[str, str] = {
    "boiler1_out": "Boiler 1 Out",
    "boiler1_return": "Boiler 1 Return",
    "boiler2_out": "Boiler 2 Out",
    "boiler2_return": "Boiler 2 Return",
    "shelly_raw_payload": "Outdoor",
}

# Optional zone defaults for the ESP32-fed sensors. Set only the values that
# are useful for your installation, for example ESP32_ZONE_BOILER1_OUT=Zone 1.
ESP32_SENSOR_ZONES: dict[str, str] = {
    key: value
    for key, value in {
        "boiler1_out": os.environ.get("ESP32_ZONE_BOILER1_OUT", ""),
        "boiler1_return": os.environ.get("ESP32_ZONE_BOILER1_RETURN", ""),
        "boiler2_out": os.environ.get("ESP32_ZONE_BOILER2_OUT", ""),
        "boiler2_return": os.environ.get("ESP32_ZONE_BOILER2_RETURN", ""),
        "shelly_raw_payload": os.environ.get("ESP32_ZONE_SHELLY", "Zone 4"),
    }.items()
    if value.strip()
}

# BLE scan duration for Shelly sensors (seconds)
# The Blu H&T advertises every ~3-10 minutes, so scan long enough to catch one
SHELLY_SCAN_DURATION = int(os.environ.get("SHELLY_SCAN_DURATION", "120"))
