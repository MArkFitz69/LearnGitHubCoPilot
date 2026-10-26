"""One-shot, read-only G3 candidate register probe; never stores measurements."""

import argparse
from datetime import datetime
import math
import sys
import time

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

# Candidate layout, NOT a verified Duracell G3 protocol. See the README for
# source revision and comparison requirements. Only telemetry blocks are read.
BLOCKS = (
    (0x2000, 11),  # Battery: SOC, voltage, signed 32-bit power.
    (0x1001, 24),  # Inverter output and both MPPT power pairs.
    (0x1300, 12),  # Grid exchange and grid-tied load power pairs.
)
ENERGY_BLOCKS = ((0x2000, 19), (0x1001, 40), (0x1300, 54))


class DiagnosticError(Exception):
    """A response cannot be used as a register snapshot."""


def _power(registers: dict[int, int], address: int, *, signed: bool) -> float:
    """Candidate high-word-first 32-bit deciwatts, expressed as watts."""
    value = (registers[address] << 16) | registers[address + 1]
    if signed and value >= 0x80000000:
        value -= 0x100000000
    return value / 10


def candidate_values(registers: dict[int, int]) -> dict[str, float]:
    """Decode the Skyline hypothesis, not authoritative inverter measurements."""
    return {
        "battery_soc_pct": float(registers[0x2000]),
        "battery_voltage_v": registers[0x2006] / 10,
        "battery_power_w_sign_unverified": _power(registers, 0x2009, signed=True),
        "mppt1_power_w": _power(registers, 0x1012, signed=False),
        "mppt2_power_w": _power(registers, 0x1016, signed=False),
        "grid_power_w_sign_unverified": _power(registers, 0x1300, signed=True),
        "grid_tied_load_w": _power(registers, 0x130A, signed=True),
    }


def energy_values(registers: dict[int, int]) -> tuple[dict[str, float], dict[str, float]]:
    """Extended Skyline candidates; daily counter units still need live checks."""
    basic = candidate_values(registers)
    live = {
        "battery_soc_pct": basic["battery_soc_pct"],
        "battery_voltage_v": basic["battery_voltage_v"],
        "battery_power_w": basic["battery_power_w_sign_unverified"],
        "grid_power_w": basic["grid_power_w_sign_unverified"],
        "load_power_w": basic["grid_tied_load_w"],
        "pv_power_w": basic["mppt1_power_w"] + basic["mppt2_power_w"],
        "mppt1_voltage_v": registers[0x1010] / 10,
        "mppt2_voltage_v": registers[0x1014] / 10,
    }
    daily = {}
    for name, address, scale in (
        ("solar_kwh", 0x1027, 1000),
        ("grid_import_kwh", 0x1332, 100),
        ("battery_charge_kwh", 0x200B, 100),
        ("battery_discharge_kwh", 0x200F, 100),
    ):
        daily[name] = ((registers[address] << 16) | registers[address + 1]) / scale
    return live, daily


def read_block(client: ModbusTcpClient, address: int, count: int, unit: int) -> list[int]:
    """Use only function 03. Reject exceptions, missing and malformed data."""
    response = client.read_holding_registers(
        address=address, count=count, device_id=unit,
    )
    if response is None:
        raise DiagnosticError(f"No response at 0x{address:04X}")
    if response.isError():
        raise DiagnosticError(f"Modbus error at 0x{address:04X}: {response}")
    values = getattr(response, "registers", None)
    if (
        not isinstance(values, list)
        or len(values) != count
        or any(type(value) is not int or not 0 <= value <= 65535 for value in values)
    ):
        raise DiagnosticError(f"Malformed/truncated register block at 0x{address:04X}")
    return values


def diagnose(host: str, port: int = 502, unit: int = 1, timeout: float = 3,
             *, energy: bool = False) -> int:
    """Make three sequential reads, with no retries or device/unit scans."""
    client = ModbusTcpClient(host, port=port, timeout=timeout, retries=0)
    registers: dict[int, int] = {}
    print(
        f"READ ONLY: {host}:{port}, unit {unit}, function 03; "
        "no database access or settings writes."
    )
    print("All interpretations are UNVERIFIED; compare with Dura-i Bluetooth.")
    try:
        if not client.connect():
            raise DiagnosticError("Connection failed; no registers were read")
        for index, (address, count) in enumerate(ENERGY_BLOCKS if energy else BLOCKS):
            if index:
                time.sleep(0.25)
            values = read_block(client, address, count, unit)
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            print(f"{timestamp} block 0x{address:04X} ({address}), count={count}")
            for offset, value in enumerate(values):
                reg = address + offset
                registers[reg] = value
                print(f"  {reg:5d} / 0x{reg:04X}: {value:5d} / 0x{value:04X}")
        print("CANDIDATE decoding: big-endian bytes, high word first; NOT VERIFIED")
        for name, value in candidate_values(registers).items():
            print(f"  {name}: {value:g}")
        if energy:
            live, daily = energy_values(registers)
            print("EXTENDED CANDIDATES: compare voltages and today's kWh with Dura-i")
            for name, value in {**live, **daily}.items():
                print(f"  {name}: {value:g}")
            print("Daily counter reset timezone is UNVERIFIED; do not assume UK midnight.")
        if registers[0x2000] > 100:
            print("WARNING: SOC outside 0..100%; candidate register map does not match.")
        print("No data saved. Grid-tied load excludes any separate EPS/backup load.")
        return 0
    except (OSError, ModbusException, DiagnosticError) as exc:
        print(f"Diagnostic failed: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


def _unit(value: str) -> int:
    unit = int(value)
    if not 1 <= unit <= 247:
        raise argparse.ArgumentTypeError("unit must be 1..247 (no broadcast)")
    return unit


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be 1..65535")
    return port


def _timeout(value: str) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or not 0 < timeout <= 10:
        raise argparse.ArgumentTypeError("timeout must be greater than 0 and at most 10 seconds")
    return timeout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Explicit inverter IP/hostname")
    parser.add_argument("--port", type=_port, default=502)
    parser.add_argument("--unit", type=_unit, default=1, help="Single unit ID; never scanned")
    parser.add_argument("--timeout", type=_timeout, default=3)
    parser.add_argument("--energy", action="store_true",
                        help="Also read candidate MPPT voltages and daily kWh counters")
    args = parser.parse_args()
    if not args.host.strip():
        parser.error("--host must not be empty")
    raise SystemExit(diagnose(
        args.host, args.port, args.unit, args.timeout, energy=args.energy,
    ))


if __name__ == "__main__":
    main()
