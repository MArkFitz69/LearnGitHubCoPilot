# Home Sensor Reader

Python service for collecting home heating and environmental telemetry into
SQLite and exposing read-only dashboards, CSV, Excel, and Power BI feeds.

## Data sources

| Source | Integration | Measurements |
|---|---|---|
| Sonoff and Hive Zigbee devices | Zigbee2MQTT over MQTT | Temperature, humidity, battery, state, power, energy |
| ESP32 heating node | MQTT | Four Dallas boiler flow/return temperatures |
| Outdoor Shelly Blu H&T | ESP32-forwarded BTHome v2, with direct BLE fallback | Temperature, humidity, battery |
| Attic Shelly Blu H&T | Direct BLE and Zigbee2MQTT | Temperature, humidity, battery |
| Hive thermostats/hot water | Hive cloud API | Current/target temperature, mode, heating and boost state |

Zigbee2MQTT is the only Zigbee integration. Pair, rename, and describe Zigbee
devices in Zigbee2MQTT; the collector consumes its MQTT metadata and readings.

Outdoor is stored as `shelly:94:B2:16:08:82:98` (`Outdoor`, Zone 4). Attic is
stored as `shelly:FC:4D:6A:1D:1D:FB` (`Attic`, Zone 5). The Attic Zigbee2MQTT
EUI-64 `fc:4d:6a:ff:fe:1d:1d:fb` is migrated and canonicalized automatically.
Historical readings and packet state are preserved.

## Architecture and behavior

The collector runs independent Zigbee2MQTT and ESP32 MQTT clients while the
async main loop polls Hive and direct Shelly BLE. MQTT callbacks use their own
SQLite connections; writes use WAL, a busy timeout, and atomic transactions.
Schema/data migrations are versioned with `PRAGMA user_version`.

The ESP32 reader subscribes only to:

```text
heating-esp/status
heating-esp/sensor/boiler1_out/state
heating-esp/sensor/bolier_1_return/state
heating-esp/sensor/boiler1_return/state
heating-esp/sensor/boiler2_out/state
heating-esp/sensor/boiler2_return/state
heating-esp/sensor/outdoorh_t_json/state
heating-esp/sensor/shelly_raw_payload/state
```

The misspelled `bolier_1_return` topic remains compatible. The preferred
Outdoor payload is:

```json
{"mac":"94:B2:16:08:82:98","payload":"44007E01642E4345D900"}
```

Compact or whitespace-separated plain BTHome v2 hex is accepted; malformed,
unsupported, or encrypted payloads are logged and ignored. Packet IDs are
deduplicated persistently with 8-bit sequence handling. Outdoor prefers ESP32
and falls back to BLE only after `SHELLY_SOURCE_FALLBACK_SECONDS` (default 1800)
without an ESP32 packet. Attic prefers BLE. When an authoritative source keeps
republishing the same valid BTHome advertisement, rapid repeats are rejected,
but one unchanged heartbeat is stored after
`SHELLY_HEARTBEAT_REPEAT_SECONDS` (default 900, i.e. one row per 15 minutes to
match typical Sonoff/Z2M/heating-probe reporting cadence). This keeps sensor
timestamps current without recording every MQTT retransmission. Accepted and
rejected packet decisions are logged at INFO with identity, source, packet
ID, delta, and reason. This setting only throttles unchanged-value rows
written to the `readings` table; ESP32/Shelly device-health "last seen"
tracking (used by `/system` and `/api/system`) is updated independently on
every MQTT message regardless of this interval, so raising it does not delay
detecting an offline sensor. Lower it (e.g. `SHELLY_HEARTBEAT_REPEAT_SECONDS=300`)
if you want unchanged heartbeats stored more frequently than every 15 minutes.
If the physical sensor or forwarding path resets its 8-bit packet counter, an
otherwise stale/out-of-range counter is accepted only from that sensor's
configured authoritative source after
`SHELLY_COUNTER_RESET_SECONDS` (default 1800). Rapid stale packets and stale
fallback-source packets remain rejected.

## Raspberry Pi setup

```bash
git clone https://github.com/MArkFitz69/LearnGitHubCoPilot.git
cd LearnGitHubCoPilot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Core environment:

```bash
export HIVE_USERNAME=your-email@example.com
export HIVE_PASSWORD=your-hive-secret
export MYENERGI_HUB_SERIAL=your-hub-serial   # optional: zappi/eddi/harvi
export MYENERGI_API_KEY=your-hub-password    # set in the myenergi app
export SENSOR_STORE_INTERVAL_SECONDS=900     # max one stored row per sensor per interval
export Z2M_MQTT_HOST=home-logger
export Z2M_MQTT_PORT=8081
export Z2M_MQTT_TRANSPORT=websockets
export ESP32_TOPIC_PREFIX=heating-esp
export SHELLY_HEARTBEAT_REPEAT_SECONDS=900
export SHELLY_COUNTER_RESET_SECONDS=1800
# Lower to 300 for more frequent unchanged-value heartbeats if desired.
```

The ESP32 client inherits every Z2M broker setting unless the corresponding
`ESP32_MQTT_*` variable is set.

The existing ESP32 subscription also records device-level MQTT health. A
non-retained recognized sensor publication or non-retained `online` status
confirms live communication even when the sensor payload is malformed; malformed
payloads still never create readings. Retained `online` replays do not refresh
liveness, while explicit `offline` status is immediate. Health becomes stale
after 30 minutes by default:

```bash
export ESP32_MQTT_STALE_MINUTES=30
```

### MQTT production hardening

Use a dedicated Mosquitto account restricted by ACL to read
`zigbee2mqtt/#` and the exact `heating-esp` topics. Do not grant publish access
except where the Zigbee2MQTT device-list request requires it. Configure:

```bash
export Z2M_MQTT_USER=sensor-reader
export Z2M_MQTT_PASS=your-service-secret
export Z2M_MQTT_TLS=true
export Z2M_MQTT_CA_CERT=/etc/ssl/certs/home-mqtt-ca.pem
```

Optional mutual TLS uses `Z2M_MQTT_CLIENT_CERT` and
`Z2M_MQTT_CLIENT_KEY`. TLS always validates the broker certificate and
hostname; there is no insecure verification mode. These settings work with
TCP/TLS or WebSockets/WSS according to `Z2M_MQTT_TRANSPORT`.

Local unencrypted MQTT remains supported for an existing trusted single-host
setup, but credentials, ACLs, and TLS are recommended whenever traffic crosses
the LAN.

### Services

```bash
sudo cp zigbee_sensor_reader/zigbee-sensor-reader.service /etc/systemd/system/
sudo cp zigbee_sensor_reader/sensor-data-api.service /etc/systemd/system/
sudo systemctl edit zigbee-sensor-reader
sudo systemctl edit sensor-data-api
sudo systemctl daemon-reload
sudo systemctl enable --now zigbee-sensor-reader sensor-data-api
```

Set `WEB_HOST` to the Pi's private LAN address where practical. The web service
is intentionally passwordless and read-only for a trusted home LAN. Restrict
TCP port 8080 to the home subnet with the host/router firewall and **never
port-forward or expose it to the internet**. Example with UFW:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp
sudo ufw deny 8080/tcp
```

## Duracell Dura-i G3: read-only local diagnostic

This is an **experimental diagnostic**, not a production inverter reader.
It does not run with the collector, open the sensor database, change inverter
settings, or save measurements. No Home Assistant installation is needed.

The target is the PD-DH1P-3.6K-G3. A successful connection to TCP port 502
does not prove Modbus compatibility or validate the register map. The
[Solar Assistant model page](https://solar-assistant.io/help/inverters/duracell/DH1P-G3)
lists this exact model as connecting through `senergy`, but documents an RS485
connection, not the inverter's native TCP interface.
The [G3 community discussion](https://community.home-assistant.io/t/duracell-dura-i-g3-dura-5-battery/988204)
reports both an RS485 adapter installation and a separate native TCP attempt.
The related [mapping discussion](https://community.home-assistant.io/t/help-mapping-modbus-registers-for-dura-inverter-with-home-assistant/989077)
contains conflicting addresses/scales, so these are **candidate interpretations**.
Its Hoymiles/DTU example is for different US hardware, not proof for this G3.

The diagnostic uses three small holding-register reads (function 03), sequentially,
with unit ID 1, a three-second timeout per request and no retries or scans.
It stops on the first failed/invalid response. Raw output includes local
timestamps and zero-based protocol addresses in decimal and hex (not `4xxxx`
reference-number notation).

Candidate decoding follows
[HA-Skyline controller.py at revision 43a3bd9](https://github.com/iPeel/HA-Skyline/blob/43a3bd9a098d5b902ee966d2e573f6e7dcdaff62/custom_components/cyg_skyline/controller.py):
the battery/PV/grid blocks, high-word-first 32-bit power values, and conversion
from deciwatts to watts. The integration expresses those power values in kW
by dividing by 10000; this diagnostic divides by 10 for W.
Battery voltage is also a candidate from the forum's 8198 mapping.
No upstream control code or settings writes are used.

| Candidate | Address(es), decimal | Interpretation to verify |
|---|---|---|
| Battery SOC | 8192 | unsigned 16-bit, % |
| Battery voltage | 8198 | unsigned 16-bit, x 0.1 V |
| Battery power | 8201-8202 | signed 32-bit, x 0.1 W; direction unverified |
| MPPT1 power | 4114-4115 | unsigned 32-bit, x 0.1 W |
| MPPT2 power | 4118-4119 | unsigned 32-bit, x 0.1 W |
| Grid exchange | 4864-4865 | signed 32-bit, x 0.1 W; direction unverified |
| Grid-tied load | 4874-4875 | signed 32-bit, x 0.1 W; excludes separate EPS load |

All pairs use big-endian bytes and the most significant register first. These
interpretations have **not been validated on a physical Duracell inverter**.
Missing/error responses never become zero-valued measurements. Implausible SOC
is flagged, and candidate values must not be used for energy accounting yet.

Once this code is available on the Pi, install dependencies in its virtual
environment and run a single snapshot manually:

```bash
cd /home/admin/LearnGitHubCoPilot
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m zigbee_sensor_reader.duracell_diagnostic --host 192.168.1.108
```

The default port is 502. `--unit` selects one unit (1-247), not a scan;
change it only when the configured inverter unit is known. `--timeout`
accepts values greater than zero and at most 10 seconds. No service restart,
credentials, new hardware or inverter configuration changes are required.
Avoid simultaneous third-party Modbus clients during the test: some gateways
only support one connection.

Compare each snapshot with **Dura-i's live Bluetooth view**, not the delayed
cloud display. Record the time, SOC, PV power, battery charge/discharge power,
grid import/export and load alongside the raw output. A repeated snapshot
during naturally different conditions helps establish scaling and direction;
do not change charging settings to force a test. Do not assume grid-tied load
is whole-house demand when EPS/backup loads exist. Zero PV at night cannot
validate its power scaling. Validate Dura16 SOC against its displayed value
rather than assuming a different battery model's compatibility.

Share the output and simultaneous app values without account credentials or
device serials. A timeout or Modbus exception means stop and investigate the
protocol/unit ID rather than scanning registers or altering settings. Automatic
15-minute logging, battery SOC storage, energy counters and dashboard changes
are deferred until the map is verified; existing history is untouched.

## Commands

```bash
python -m zigbee_sensor_reader
python -m zigbee_sensor_reader --hive
python -m zigbee_sensor_reader --discover-shelly
python -m zigbee_sensor_reader --serve --port 8080
python -m zigbee_sensor_reader --summary
python -m zigbee_sensor_reader --export csv
python -m zigbee_sensor_reader --export xlsx
```

## Read-only web surfaces

| Endpoint | Description |
|---|---|
| `/dashboard` | Boiler probes, exact 96-bucket local 24-hour chart, Hive, Outdoor, Attic, and plugs |
| `/system` | Pi/service/database status and sensor health |
| `/api/status` | Lightweight status |
| `/api/system` | Full system status and source counts |
| `/api/dashboard` | Dashboard JSON and chart series |
| `/api/sensors` | Sensor registry with effective zones |
| `/api/readings` | Filterable readings; add `format=csv` for Power BI |
| `/api/readings/latest` | Exactly one latest row per sensor |
| `/api/export/csv` | Download all matching readings |

`/api/system` includes structured `esp32_mqtt_health` entries. `/system`
renders the same state, last confirmed communication, age, last MQTT status,
retained-status indicator, and last live topic after the Services table.

Zones are configured in `config.py` or derived from Zigbee2MQTT descriptions;
the website does not mutate configuration or database metadata. All APIs,
dashboard queries, CSV exports, and Power BI feeds expose the same effective
zone precedence: Zigbee2MQTT description, configured sensor zone, then the
historical reading zone. Configured canonical Outdoor/Attic zones remain
authoritative. Zigbee2MQTT descriptions are cached across ordinary state
messages that omit device metadata; clearing a description in Zigbee2MQTT
clears the derived zone on the next bridge device-list update.

The boiler chart uses exactly 96 local 15-minute buckets. Each sensor/bucket
contains only the latest actual reading; gaps remain null. Values are never
averaged, smoothed, carried forward, jittered, or converted into runtime.

## Project structure

```text
zigbee_sensor_reader/
├── __main__.py
├── config.py
├── database.py
├── mqtt_config.py
├── z2m_reader.py
├── esp32_mqtt_reader.py
├── shelly_ble_reader.py
├── hive_reader.py
├── web_server.py
├── export.py
├── zigbee-sensor-reader.service
└── sensor-data-api.service
```
