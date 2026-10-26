import contextlib
import io
import socket
import struct
import threading
import unittest
from unittest import mock

from pymodbus.exceptions import ModbusIOException

from zigbee_sensor_reader import duracell_diagnostic as diagnostic


def register_snapshot():
    registers = {
        address + offset: 0
        for address, count in diagnostic.BLOCKS
        for offset in range(count)
    }
    registers.update({
        0x2000: 85,
        0x2006: 536,
        0x2009: 65535,
        0x200A: 53536,  # signed 32-bit -12000 -> -1200 W
        0x1012: 1,
        0x1013: 4464,  # unsigned 32-bit 70000 -> 7000 W
        0x1016: 0,
        0x1017: 10000,
        0x1300: 0,
        0x1301: 2500,
        0x130A: 0,
        0x130B: 12000,
    })
    return registers


def response(values):
    result = mock.Mock()
    result.isError.return_value = False
    result.registers = values
    return result


class DuracellDiagnosticTests(unittest.TestCase):
    def test_candidate_decoding_preserves_width_sign_word_order_and_scale(self):
        values = diagnostic.candidate_values(register_snapshot())
        self.assertEqual(values["battery_soc_pct"], 85)
        self.assertEqual(values["battery_voltage_v"], 53.6)
        self.assertEqual(values["battery_power_w_sign_unverified"], -1200)
        self.assertEqual(values["mppt1_power_w"], 7000)
        self.assertEqual(values["mppt2_power_w"], 1000)
        self.assertEqual(values["grid_power_w_sign_unverified"], 250)
        self.assertEqual(values["grid_tied_load_w"], 1200)

    def test_missing_register_does_not_become_zero(self):
        registers = register_snapshot()
        del registers[0x1013]
        with self.assertRaises(KeyError):
            diagnostic.candidate_values(registers)

    def test_read_parameters_use_single_unit_and_holding_function(self):
        client = mock.Mock()
        client.read_holding_registers.return_value = response([85, 0])
        self.assertEqual(diagnostic.read_block(client, 8192, 2, 1), [85, 0])
        client.read_holding_registers.assert_called_once_with(
            address=8192, count=2, device_id=1,
        )

    def test_malformed_blocks_are_rejected(self):
        for values in (None, [], [1], [1, 2, 3], [1, -1], [1, 65536], [1, True]):
            with self.subTest(values=values):
                client = mock.Mock()
                client.read_holding_registers.return_value = response(values)
                with self.assertRaises(diagnostic.DiagnosticError):
                    diagnostic.read_block(client, 8192, 2, 1)

    def test_exception_and_missing_responses_are_rejected(self):
        error = mock.Mock()
        error.isError.return_value = True
        for result in (None, error):
            with self.subTest(result=result):
                client = mock.Mock()
                client.read_holding_registers.return_value = result
                with self.assertRaises(diagnostic.DiagnosticError):
                    diagnostic.read_block(client, 8192, 2, 1)

    def test_three_reads_are_bounded_and_client_is_closed(self):
        registers = register_snapshot()
        with (
            mock.patch.object(diagnostic, "ModbusTcpClient") as factory,
            mock.patch.object(diagnostic.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            client = factory.return_value
            client.read_holding_registers.side_effect = [
                response([registers[address + offset] for offset in range(count)])
                for address, count in diagnostic.BLOCKS
            ]
            self.assertEqual(diagnostic.diagnose("test-inverter"), 0)
            factory.assert_called_once_with(
                "test-inverter", port=502, timeout=3, retries=0,
            )
            self.assertEqual(client.read_holding_registers.call_args_list, [
                mock.call(address=address, count=count, device_id=1)
                for address, count in diagnostic.BLOCKS
            ])
            client.write_register.assert_not_called()
            client.write_registers.assert_not_called()
            client.close.assert_called_once()
        self.assertIn("UNVERIFIED", output.getvalue())
        self.assertIn("No data saved", output.getvalue())

    def test_failures_stop_without_retry_or_candidate_output(self):
        for error in (TimeoutError("timed out"), ModbusIOException("no response")):
            with (
                self.subTest(error=error),
                mock.patch.object(diagnostic, "ModbusTcpClient") as factory,
                contextlib.redirect_stdout(io.StringIO()) as output,
                contextlib.redirect_stderr(io.StringIO()) as errors,
            ):
                client = factory.return_value
                client.read_holding_registers.side_effect = error
                self.assertEqual(diagnostic.diagnose("test-inverter"), 1)
                client.read_holding_registers.assert_called_once()
                client.close.assert_called_once()
                self.assertNotIn("CANDIDATE decoding:", output.getvalue())
                self.assertIn("Diagnostic failed:", errors.getvalue())

    def test_connection_failure_never_reads(self):
        with (
            mock.patch.object(diagnostic, "ModbusTcpClient") as factory,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            client = factory.return_value
            client.connect.return_value = False
            self.assertEqual(diagnostic.diagnose("test-inverter"), 1)
            client.read_holding_registers.assert_not_called()
            client.close.assert_called_once()

    def test_invalid_soc_is_flagged_not_hidden(self):
        registers = register_snapshot()
        registers[0x2000] = 65535
        with (
            mock.patch.object(diagnostic, "ModbusTcpClient") as factory,
            mock.patch.object(diagnostic.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            factory.return_value.read_holding_registers.side_effect = [
                response([registers[address + offset] for offset in range(count)])
                for address, count in diagnostic.BLOCKS
            ]
            self.assertEqual(diagnostic.diagnose("test-inverter"), 0)
        self.assertIn("SOC outside 0..100%", output.getvalue())

    def test_cli_requires_host_and_rejects_unsafe_limits(self):
        cases = [
            [], ["--host", ""], ["--host", "test", "--unit", "0"],
            ["--host", "test", "--unit", "248"],
            ["--host", "test", "--timeout", "nan"],
            ["--host", "test", "--timeout", "0"],
            ["--host", "test", "--timeout", "11"],
            ["--host", "test", "--port", "0"],
        ]
        for args in cases:
            with (
                self.subTest(args=args),
                mock.patch("sys.argv", ["diagnostic", *args]),
                mock.patch.object(diagnostic, "diagnose") as run,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(SystemExit) as raised:
                    diagnostic.main()
                self.assertEqual(raised.exception.code, 2)
                run.assert_not_called()

    def test_real_client_wire_requests_are_only_function03(self):
        """Exercise actual pymodbus framing against a local fake inverter."""
        requests = []
        failures = []
        registers = register_snapshot()

        def receive_exact(peer, count):
            data = b""
            while len(data) < count:
                chunk = peer.recv(count - len(data))
                if not chunk:
                    raise ConnectionError("Client closed before completing request")
                data += chunk
            return data

        def serve(listener):
            try:
                peer, _ = listener.accept()
                with peer:
                    peer.settimeout(2)
                    for _ in diagnostic.BLOCKS:
                        frame = receive_exact(peer, 12)
                        tid, protocol, length, unit, function, address, count = (
                            struct.unpack(">HHHBBHH", frame)
                        )
                        requests.append((protocol, length, unit, function, address, count))
                        words = [registers[address + offset] for offset in range(count)]
                        pdu = bytes([3, count * 2]) + struct.pack(f">{count}H", *words)
                        packet = struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu
                        peer.sendall(packet)
            except (OSError, KeyError) as exc:
                failures.append(exc)

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(3)
            thread = threading.Thread(target=serve, args=(listener,), daemon=True)
            thread.start()
            with contextlib.redirect_stdout(io.StringIO()):
                result = diagnostic.diagnose("127.0.0.1", listener.getsockname()[1], timeout=1)
            thread.join(timeout=4)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(result, 0)
        self.assertEqual(requests, [
            (0, 6, 1, 3, address, count) for address, count in diagnostic.BLOCKS
        ])


if __name__ == "__main__":
    unittest.main()
