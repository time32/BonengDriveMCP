"""Exercise vendor-backed packets against a loopback device, never real hardware."""
from __future__ import annotations

import json
import asyncio
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
BRIDGE = PROJECT / "workspace/reuse_bridge/ReuseDeviceBridge.exe"
VENDOR = Path(os.environ.get("BONENG_RELEASE_DIR", "")) / "BonengDrivesoft.exe"
MAC = "02:AA:BB:CC:DD:EE"


def frame(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return bytes(data) + struct.pack("<H", crc)


class LoopbackDrive:
    def __init__(self):
        self.listener = socket.socket()
        # The bridge's fixed vendor port is bound only on a loopback address.
        self.listener.bind(("127.0.0.2", 1000))
        self.listener.listen(1)
        self.listener.settimeout(15)
        self.writes, self.errors = [], []
        self.control, self.speed = 0, 0
        self.values = {(0x34, 0): 1000, (0x51, 0): 0,
                       (0x50, 0): 1, (0x50, 4): 20000, (0x50, 5): 3000, (0x50, 6): 2000, (0x50, 7): 1000, (0x50, 9): 4,
                       (0x83, 20): 0, (0x83, 22): 300, (0x83, 23): 500}
        self.position, self.position_complete, self.simulate_position = 0, False, True
        self.parameter_version, self.release = 11, 0x6908
        self.flash_result = 0
        self.short_a1_request = False  # Original updater uses 5 bytes; Drivesoft uses 6.
        self.catalog_name = "Simulated parameter"
        self.catalog_invalid_length = False
        self.catalog_groups = [(0x20, 1, "A00"), (0x34, 2, "B04"), (0x51, 2, "D01")]
        self.heartbeat_times = []
        self.request_times = []
        self.scope_started_at, self.scope_channels, self.scope_consumed, self.scope_divisor = None, 0, 0, 1
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.listener.close()
        self.thread.join(timeout=5)

    def word(self, group, offset):
        if group == 0x45:
            return {19: 0xAA02, 20: 0xCCBB, 21: 0xEEDD}.get(offset, 0)
        running = bool(self.control & 1 and self.control & 0x400)
        if (group, offset) == (0x22, 8): return self.release
        if (group, offset) == (0x22, 0): return 910
        if (group, offset) == (0x20, 0): return 12 if running else 5
        if (group, offset) == (0x20, 6): return 300 if running and not self.position_complete else 0
        if (group, offset) == (0xEE, 22): return self.position & 0xFFFF
        if (group, offset) == (0xEE, 23): return self.position >> 16 & 0xFFFF
        if group == 0xE1 and offset in {38, 91}: return int(self.position_complete)
        if (group, offset) == (0xEC, 7): return (0x200 if self.control & 0x400 else 0) | (4 if running else 0)
        if (group, offset) == (0xEC, 11): return self.control
        if (group, offset) == (0xEC, 14): return self.speed
        if (group, offset) == (0x51, 1): return self.values[(0x51, 0)] >> 16
        if (group, offset) == (0x83, 21): return self.values[(0x83, 20)] >> 16 & 0xFFFF
        if group == 0xEE and offset in {66, 67}: return self.values.get((0xEE, 66), 0) >> ((offset - 66) * 16) & 0xFFFF
        return self.values.get((group, offset), 0) & 0xFFFF

    @staticmethod
    def attr(group, offset):
        if group in {0x50,0x53,0x56,0x59} and offset in {4,5,6}: return (1<<12) | ({4:1,5:3,6:5}[offset]<<4)
        if (group,offset)==(0x20,6): return (2<<12) | (1<<18) | (3<<4)
        if group==0x83 and offset in {22,23}: return (1<<12) | ({22:3,23:20}[offset]<<4)
        if (group, offset) in {(0x83, 20), (0xEE, 22)}: return (1 << 29) | (1 << 18) | ((2 if group == 0xEE else 1) << 12)
        if group in {0x30, 0x35, 0x50, 0x83}: return 1 << 12
        if (group, offset) == (0x51, 0): return (1 << 29) | (1 << 18) | (1 << 12)
        return (1 if group == 0x34 else 2) << 12

    def response(self, request):
        self.request_times.append((time.monotonic(), request.hex()))
        assert frame(request[:-2]) == request, "Outgoing vendor CRC invalid"
        if request[:4] == bytes.fromhex("AA55A5A5"):
            return frame(request[:4] + struct.pack("<H", self.parameter_version))
        if request[:3] == bytes.fromhex("AA55FA"):
            return frame(request[:3] + b"\x02" + struct.pack("<HH", 9, 3))
        if request[:3] == bytes.fromhex("FF55FC"):
            return frame(request[:3] + bytes([self.flash_result]))
        if request[:3] == bytes.fromhex("BB55FF"):
            return frame(request[:3] + bytes([len(dict.fromkeys(g[2][0] for g in self.catalog_groups))]))
        if request[:3] == bytes.fromhex("BB55FD"):
            i = request[3]
            letter = list(dict.fromkeys(g[2][0] for g in self.catalog_groups))[i]
            name = (letter + ":测试组\0").encode("gbk")
            return frame(request[:4] + bytes([sum(g[2][0] == letter for g in self.catalog_groups), len(name)]) + name)
        if request[:3] == bytes.fromhex("BB55FE"):
            i = request[3]
            group, count, name = self.catalog_groups[i]
            name = (name + "组\0").encode("gbk")
            return frame(request[:4] + bytes([count, group, 255 if self.catalog_invalid_length else len(name)]) + name)
        if request[:3] == bytes.fromhex("EE55FA"):
            self.scope_divisor = int.from_bytes(request[3:5], "little")
            self.scope_channels, self.scope_started_at, self.scope_consumed = request[5], time.monotonic(), 0
            return frame(request[:3] + b"\0")
        if request[:3] == bytes.fromhex("EE55FB"):
            if request[3] == 0:
                self.scope_started_at = None
                return frame(request[:3] + b"\0\x01")
            available = int((time.monotonic() - self.scope_started_at) / (0.002 * self.scope_divisor)) - self.scope_consumed
            count = max(0, min(40, available)); self.scope_consumed += count
            values = b"".join(struct.pack("<H", 30000 if c == 0 else 0xFF9C) for _ in range(count) for c in range(self.scope_channels))
            return frame(request[:3] + bytes([1, 1, count]) + values)
        sof, kind, group, offset, count = struct.unpack("<H4B", request[:6])
        header = request[:6]
        if sof == 0x55CC and kind == 0xFB:
            values = b"".join(struct.pack("<HB", self.word(group, offset + i), 2 if group == 0x34 or (group in {0x50,0x53,0x56,0x59} and offset+i == 4) else 1 if group in {0x50,0x53,0x56,0x59} and offset+i in {6,7} else 0) for i in range(count))
            return frame(header + values)
        if sof == 0x55BB:
            if kind == 0xFA: data = b"".join(struct.pack("<I", self.attr(group, offset + i)) for i in range(count))
            elif kind == 0xFB: data = struct.pack("<III", 0x80000000, 0x7FFFFFFF, 0) if (group,offset) in {(0x51,0),(0x83,20),(0xEE,22)} else struct.pack("<III", 0, 65535 if group==0x83 and offset==23 else 10000, 0)
            elif kind == 0xFC:
                name = {(0x50,5):"马达额定转速",(0x50,6):"马达最大转速",(0x50,4):"马达额定频率",
                        (0xEE,22):"位置反馈(指令单位)",(0x83,20):"位置JOG1移动位移",(0x83,22):"位置JOG1最大速度",(0x83,23):"位置JOG1加减速时间"}.get((group,offset),self.catalog_name)
                if group in {0x53,0x56,0x59}: name={4:"马达额定频率",5:"马达额定转速",6:"马达最大转速"}.get(offset,name)
                data = name.encode("gbk").ljust(32, b"\0") * count
            else: raise AssertionError("Unexpected metadata request")
            return frame(header + data)
        if sof == 0x55CC and kind in {0xFC, 0xFF}:
            value = int.from_bytes(request[6:-2], "little")
            self.writes.append((group, offset, value))
            if (group, offset) == (0xEC, 11):
                if value & 0x501 == 0x501 and not self.control & 0x100 and self.values.get((0x35,1))==2:
                    delta = self.values[(0x83,20)]; delta = delta-2**32 if delta >= 2**31 else delta
                    self.values[(0xEE,66)] = delta & 0xFFFFFFFF
                    if self.simulate_position: self.position += delta; self.position_complete = True
                self.control = value
                if value & 0x401 == 0x401: self.heartbeat_times.append(time.monotonic())
            elif (group, offset) == (0xEC, 14): self.speed = value
            else: self.values[group, offset] = value
            return frame(header + b"\0")
        raise AssertionError("Unexpected device command")

    def serve(self):
        try:
            connection, _ = self.listener.accept()
            with connection:
                connection.settimeout(10)
                buffered = b""
                while True:
                    while len(buffered) < 5:
                        block = connection.recv(1024)
                        if not block: return
                        buffered += block
                    kind = buffered[2]
                    length = 16 if buffered[0] == 0xEE and kind == 0xFA else 6 if buffered[0] in {0xAA, 0xEE, 0xFF} or (buffered[0] == 0xBB and kind in {0xFD, 0xFE, 0xFF}) else 12 if buffered[0] == 0xCC and kind == 0xFF else 10 if buffered[0] == 0xCC and kind == 0xFC else 8
                    if self.short_a1_request and buffered[:3] == bytes.fromhex("AA55FA"): length = 5
                    while len(buffered) < length:
                        buffered += connection.recv(1024)
                    request, buffered = buffered[:length], buffered[length:]
                    response = self.response(request)
                    # Deliberately fragment replies to verify exact-length framing.
                    connection.sendall(response[:3])
                    connection.sendall(response[3:])
        except Exception as error:
            self.errors.append(error)


@unittest.skipUnless(sys.platform == "win32" and BRIDGE.is_file() and VENDOR.is_file(), "Requires built vendor bridge and BONENG_RELEASE_DIR")
class BridgeSimulationTests(unittest.TestCase):
    def request(self, folder, operation, **extra):
        path = folder / "request.json"
        path.write_text(json.dumps({"operation": operation, "transport": "tcp", "host": "127.0.0.2", "interface_ip": "127.0.0.1", "expected_mac": MAC, **extra}), encoding="utf-8")
        return [str(BRIDGE), str(VENDOR), str(path)]

    def test_registered_new_build_can_capture_with_expected_identity(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / "workspace/test_artifacts") as temporary, LoopbackDrive() as drive:
            drive.release = 0x6909
            identity = {"parameter_version_raw": 11, "platform_type": 9, "communication_type": 3,
                        "software_raw": 910, "release_hex": "6909", "mac": MAC}
            command = self.request(Path(temporary), "capture", expected_identity=identity,
                registered_capture_profile="an_pn_spi_910", sample_divisor=1, points=10, timeout_ms=8000,
                parameters=[{"code": "P11.03", "group": 0xEB, "offset": 3}])
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, cwd=BRIDGE.parent, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            result = json.loads(completed.stdout.decode("utf-8"))
            self.assertEqual(completed.returncode, 0, result)
            self.assertEqual(len(result["result"]["raw_samples"]), 10)
            self.assertEqual(result["result"]["definitions"][0]["code"], "P11.03")
            self.assertTrue(result["result"]["scope_stopped"])
            self.assertEqual(drive.writes, [])
            self.assertFalse(drive.errors)

    def test_registered_capture_profile_does_not_enable_new_build_motor_control(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / "workspace/test_artifacts") as temporary, LoopbackDrive() as drive:
            drive.release = 0x6909
            identity = {"parameter_version_raw": 11, "platform_type": 9, "communication_type": 3,
                        "software_raw": 910, "release_hex": "6909", "mac": MAC}
            command = self.request(Path(temporary), "run", expected_identity=identity, registered_capture_profile="an_pn_spi_910")
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, cwd=BRIDGE.parent, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            result = json.loads(completed.stdout.decode("utf-8"))
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("inspected AN builds", result["error"])
            self.assertFalse(result["device_write_commands_sent"])
            self.assertEqual(drive.writes, [])

    def test_online_catalog_uses_b5_addresses_and_device_names(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / "workspace/test_artifacts") as temporary, LoopbackDrive() as drive:
            drive.catalog_name = "设备自己的定义"
            # Read-only metadata discovery also supports another AN firmware build.
            drive.release = 0x7777
            command = self.request(Path(temporary), "catalog")
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, cwd=BRIDGE.parent, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            result = json.loads(completed.stdout.decode("utf-8"))
            self.assertEqual(completed.returncode, 0, result)
            catalog = result["result"]
            self.assertEqual([g["group"] for g in catalog["groups"]], [0x20, 0x34, 0x51])
            self.assertEqual([p["code"] for p in catalog["parameters"]], ["A00.00", "B04.00", "B04.01", "D01.00", "D01.01"])
            self.assertTrue(all(p["name"] == "设备自己的定义" for p in catalog["parameters"]))
            self.assertEqual(catalog["parameters"][-1]["high_word_of"], "D01.00")
            self.assertEqual(result["identity"]["parameter_version_raw"], 11)
            self.assertFalse(result["device_write_commands_sent"])
            self.assertEqual(drive.writes, [])
            self.assertFalse(drive.errors)

    def test_catalog_rejects_malformed_variable_length_response(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / "workspace/test_artifacts") as temporary, LoopbackDrive() as drive:
            drive.catalog_invalid_length = True
            completed = subprocess.run(self.request(Path(temporary), "catalog"), stdin=subprocess.DEVNULL, capture_output=True, cwd=BRIDGE.parent, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            result = json.loads(completed.stdout.decode("utf-8"))
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("Invalid B5", result["error"])
            self.assertFalse(result["device_write_commands_sent"])
            self.assertEqual(drive.writes, [])

    def test_parameter_revision_change_blocks_write_before_command(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / "workspace/test_artifacts") as temporary, LoopbackDrive() as drive:
            identity = {"parameter_version_raw": 10, "platform_type": 9, "communication_type": 3, "software_raw": 910, "release_hex": "6908", "mac": MAC}
            command = self.request(Path(temporary), "write", expected_identity=identity, parameters=[{"code": "B04.00", "group": 0x34, "offset": 0}])
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, cwd=BRIDGE.parent, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            result = json.loads(completed.stdout.decode("utf-8"))
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("identity changed", result["error"])
            self.assertEqual(drive.writes, [])

    def test_decimal_change_blocks_write_before_command(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / "workspace/test_artifacts") as temporary, LoopbackDrive() as drive:
            command = self.request(Path(temporary), "write", parameters=[{
                "code": "B04.00", "group": 0x34, "offset": 0, "expected_before": 1000,
                "expected_attribute": drive.attr(0x34, 0), "expected_name": drive.catalog_name,
                "expected_lower": 0, "expected_upper": 10000, "expected_decimal": 1, "raw_value": 1200}])
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, cwd=BRIDGE.parent, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            result = json.loads(completed.stdout.decode("utf-8"))
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("definition changed", result["error"])
            self.assertEqual(drive.writes, [])

    def test_continuous_rpm_heartbeat_and_stop(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / "workspace/test_artifacts") as temporary, LoopbackDrive() as drive:
            folder = Path(temporary)
            command = self.request(folder, "run_continuous", speed_raw=30000, max_speed_raw=30000, duration_ms=0, speed_unit="rpm", stop_file=str(folder / "stop.request"), restore_control_mode=2)
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=BRIDGE.parent, creationflags=subprocess.CREATE_NO_WINDOW)
            events = []
            def collect():
                for line in process.stdout:
                    events.append(json.loads(line.decode("utf-8")))
            collector = threading.Thread(target=collect, daemon=True)
            collector.start()
            try:
                deadline = time.monotonic() + 10
                while not any(e.get("phase") == "run_progress" for e in events) and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(any(e.get("run_bit") for e in events), events)
                time.sleep(0.35)
                (folder / "stop.request").touch()
                self.assertEqual(process.wait(timeout=15), 0)
                collector.join(timeout=2)
                self.assertTrue(events[-1]["result"]["stopped_run_bit_verified"])
                self.assertTrue(events[-1]["result"]["control_released"])
                self.assertTrue(events[-1]["result"]["control_mode_restored"])
                self.assertIn((0xEC, 14, 30000), drive.writes)
                self.assertGreaterEqual(drive.writes.count((0xEC, 11, 0x947F)), 3)
                self.assertEqual(drive.writes[-2], (0xEC, 11, 0x8000))
                self.assertEqual(drive.writes[-1], (0x35, 1, 2))
                self.assertFalse(drive.errors)
            finally:
                if process.poll() is None: process.kill()
                process.wait(timeout=5)
                collector.join(timeout=2)
                process.stdout.close(); process.stderr.close()

    def test_signed_32bit_write_and_readback(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / "workspace/test_artifacts") as temporary, LoopbackDrive() as drive:
            command = self.request(Path(temporary), "write", parameters=[{"code": "D01.00", "group": 0x51, "offset": 0, "expected_before": 0, "expected_attribute": drive.attr(0x51, 0), "raw_value": 0xFFFFFFF9}])
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, cwd=BRIDGE.parent, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            result = json.loads(completed.stdout.decode("utf-8"))
            self.assertEqual(completed.returncode, 0, result)
            self.assertEqual(result["result"]["parameters"][0]["value"], -7)
            self.assertEqual(drive.writes, [(0x51, 0, 0xFFFFFFF9)])
            self.assertFalse(drive.errors)

    def test_wrong_mac_prevents_all_writes(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / "workspace/test_artifacts") as temporary, LoopbackDrive() as drive:
            command = self.request(Path(temporary), "run", expected_mac="02:00:00:00:00:01", speed_raw=30000, max_speed_raw=30000, duration_ms=1000, speed_unit="rpm")
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, cwd=BRIDGE.parent, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            result = json.loads(completed.stdout.decode("utf-8"))
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(result["device_write_commands_sent"])
            self.assertEqual(drive.writes, [])

    def test_position_mode_prevents_speed_run_commands(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / "workspace/test_artifacts") as temporary, LoopbackDrive() as drive:
            drive.values[0x35, 1] = 2
            command = self.request(Path(temporary), "run", speed_raw=30000, max_speed_raw=30000, duration_ms=1000, speed_unit="rpm")
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, cwd=BRIDGE.parent, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            result = json.loads(completed.stdout.decode("utf-8"))
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("B05.01", result["error"])
            self.assertFalse(result["device_write_commands_sent"])
            self.assertEqual(drive.writes, [])

    def test_stop_preempts_long_capture_and_clears_scope(self):
        self.exercise_long_capture_stop(False)

    def test_native_duration_limit_preempts_long_capture(self):
        self.exercise_long_capture_stop(True)

    def exercise_long_capture_stop(self, use_duration_limit):
        with tempfile.TemporaryDirectory(dir=PROJECT / "workspace/test_artifacts") as temporary, LoopbackDrive() as drive:
            folder = Path(temporary); commands = folder / "commands"; commands.mkdir()
            job_id = "b" * 32
            command = self.request(folder, "run_continuous", speed_raw=30000, max_speed_raw=30000, duration_ms=0, speed_unit="rpm",
                stop_file=str(folder / "stop.request"), command_dir=str(commands), run_job_id=job_id, maximum_duration_ms=2000 if use_duration_limit else 0)
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       cwd=BRIDGE.parent, creationflags=subprocess.CREATE_NO_WINDOW)
            events = []
            collector = threading.Thread(target=lambda: [events.append(json.loads(line.decode("utf-8"))) for line in process.stdout], daemon=True)
            collector.start()
            try:
                deadline = time.monotonic() + 10
                while not any(e.get("phase") == "run_progress" for e in events) and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(any(e.get("run_bit") for e in events), events)
                request = {"operation": "capture", "run_job_id": job_id, "expected_mac": MAC, "sample_divisor": 1, "points": 20000,
                    "timeout_ms": 50000, "parameters": [{"code": "P11.03", "group": 0xEB, "offset": 3}]}
                temporary_request = commands / "capture.tmp"
                temporary_request.write_text(json.dumps(request), encoding="utf-8")
                temporary_request.replace(commands / "capture.request.json")
                deadline = time.monotonic() + 10
                while drive.scope_started_at is None and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertIsNotNone(drive.scope_started_at, events)
                started_stop = time.monotonic()
                if not use_duration_limit: (folder / "stop.request").touch()
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertLess(time.monotonic() - started_stop, 5)
                collector.join(timeout=2)
                result = json.loads((commands / "capture.result.json").read_text(encoding="utf-8"))
                self.assertEqual(result["status"], "cancelled")
                self.assertIsNone(drive.scope_started_at)
                self.assertTrue(events[-1]["result"]["stopped_run_bit_verified"])
                self.assertFalse(drive.errors)
            finally:
                if process.poll() is None: process.kill()
                process.wait(timeout=5); collector.join(timeout=2)
                process.stdout.close(); process.stderr.close()

    def test_worker_survives_mcp_session_close_and_stops_from_new_session(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def exercise(folder):
            target = {"transport": "tcp", "expected_mac": MAC, "host": "127.0.0.2", "interface_ip": "127.0.0.1", "scan_id": "simulation", "device_id": "simulation"}
            # Only this private test server substitutes a loopback target and
            # offline preview. The real worker still checks identity and state.
            server = folder / "simulation_server.py"
            server.write_text(
                "import sys\nfrom pathlib import Path\n"
                f"sys.path.insert(0,{str(PROJECT / 'src')!r})\n"
                "from boneng_mcp.device import DeviceOperations\n"
                "from boneng_mcp.server import create_server\n"
                "from boneng_mcp.paths import Settings\n"
                f"DeviceOperations._target=lambda *args,**kwargs: {target!r}\n"
                f"DeviceOperations.runtime=lambda *args,**kwargs: {{'release_hex':'6908','identity':{{'mac':{MAC!r},'platform_type':9,'communication_type':3,'software_raw':910,'release_hex':'6908','parameter_version_raw':11}}}}\n"
                f"DeviceOperations.motion_limits=lambda *args,**kwargs: {{'identity':{{'mac':{MAC!r},'platform_type':9,'communication_type':3,'software_raw':910,'release_hex':'6908','parameter_version_raw':11}},'result':{{'motor_data_group':0,'motor_type_raw':1,'rated_rpm':3000,'motor_max_rpm':6000,'base_rpm':3000,'rated_frequency_raw':20000,'pole_pairs':4,'motor_max_percent_raw':2000,'motor_min_percent_raw':1000}}}}\n"
                f"create_server(Settings(Path({str(VENDOR.parent)!r}),Path({str(PROJECT / 'data')!r}),Path({str(folder)!r}))).run()\n", encoding="utf-8")
            params = StdioServerParameters(command=sys.executable, args=[str(server)], cwd=str(PROJECT), env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            def data(result):
                self.assertFalse(result.isError, result.content)
                return result.structuredContent
            stop_file = None
            try:
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        plan = data(await session.call_tool("plan_drive_run", {"speed": 300, "maximum_abs_speed": 300, "speed_unit": "rpm", "duration_s": None, "motor_load_description": "loopback simulation only", "local_stop_available": True, "restore_control_mode": 2}))
                        job = data(await session.call_tool("execute_drive_run", {"plan_id": plan["plan_id"]}))
                        stop_file = Path(job["stop_file"])
                        status = None
                        for _ in range(20):
                            status = data(await session.call_tool("get_drive_run_status", {"job_id": job["job_id"]}))
                            if status["status"] == "running": break
                            await asyncio.sleep(0.2)
                        self.assertEqual(status["status"], "running", status)
                        before = status["latest_progress"]["elapsed_ms"]
                # The original failure happened here: closing the MCP client's
                # Windows Job Object killed the old subprocess implementation.
                await asyncio.sleep(2)
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        status = data(await session.call_tool("get_drive_run_status", {"job_id": job["job_id"]}))
                        self.assertEqual(status["status"], "running", status)
                        self.assertTrue(status["worker_alive"])
                        self.assertGreater(status["latest_progress"]["elapsed_ms"], before)
                        channels = data(await session.call_tool("list_oscilloscope_channels", {"group": "P11"}))
                        self.assertFalse(channels["reference_only"])
                        reading = data(await session.call_tool("read_device_parameters", {"codes": ["P11.03"]}))
                        self.assertTrue(reading["executed_on_run_connection"])
                        capture = data(await session.call_tool("capture_oscilloscope", {"codes": ["P11.03", "P11.04"], "sample_period_ms": 2, "points": 1000}))
                        self.assertTrue(capture["executed_on_run_connection"])
                        self.assertTrue(capture["result"]["scope_stopped"])
                        self.assertTrue(Path(capture["csv_file"]).is_file())
                        snapshot = data(await session.call_tool("create_device_parameter_snapshot", {"label": "running loopback configuration", "groups": ["B04"]}))
                        self.assertEqual(snapshot["parameter_count"], 2)
                        self.assertTrue(json.loads(Path(snapshot["file"]).read_text(encoding="utf-8"))["background_control_writes_continued"])
                        status = data(await session.call_tool("get_drive_run_status", {"job_id": job["job_id"]}))
                        self.assertEqual(status["status"], "running")
                        stopped = data(await session.call_tool("stop_drive_pc_control", {}))
                        self.assertEqual(stopped["status"], "stopped", stopped)
                        self.assertTrue(stopped["terminal_result"]["result"]["stopped_run_bit_verified"])
                        self.assertTrue(stopped["terminal_result"]["result"]["control_mode_restored"])
            finally:
                if stop_file:
                    stop_file.touch(exist_ok=True)

        with tempfile.TemporaryDirectory(dir=PROJECT / "workspace/test_artifacts") as temporary, LoopbackDrive() as drive:
            drive.catalog_groups.append((0xEB, 7, "P11"))
            asyncio.run(exercise(Path(temporary)))
            self.assertFalse(drive.errors)
            self.assertIsNone(drive.scope_started_at)
            gap, start, end = max((b - a, a, b) for a, b in zip(drive.heartbeat_times, drive.heartbeat_times[1:]))
            self.assertLess(gap, 0.4, [(round(t-start, 3), r) for t, r in drive.request_times if start-0.01 <= t <= end+0.01])
            (PROJECT / "data/run_scope_simulation_verification.json").write_text(json.dumps({
                "status": "passed", "hardware_test_performed": False, "backend": "original vendor bridge over loopback TCP / real MCP stdio",
                "capture": {"channels": ["P11.03", "P11.04"], "sample_period_ms": 2, "points": 1000},
                "maximum_heartbeat_gap_s": gap, "heartbeat_count": len(drive.heartbeat_times),
                "mcp_session_close_survived": True, "live_read_during_run": True, "configuration_snapshot_during_run": True,
                "scope_stopped": drive.scope_started_at is None, "normal_stop_and_mode_restore_passed": True,
                "physical_motion_test_performed": False}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
