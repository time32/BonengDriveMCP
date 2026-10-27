"""Registered builds through the real vendor read-only bridge on loopback only."""
from __future__ import annotations

import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_device_bridge_simulation import PROJECT, VENDOR, MAC, LoopbackDrive

sys.path.insert(0, str(PROJECT / "src"))
from boneng_mcp.builds import FirmwareBuilds, IMAGE_BYTES

BRIDGE = PROJECT / "workspace/reuse_bridge/ReuseFirmwareUpgrade.exe"
UPDATER = VENDOR.parent / "Downloadconfig/Downloadconfig.exe"


@unittest.skipUnless(sys.platform == "win32" and BRIDGE.is_file() and UPDATER.is_file(), "Requires built firmware bridge and BONENG_RELEASE_DIR")
class FirmwareBridgeSimulationTests(unittest.TestCase):
    def check_mode(self, mode, *, release=0x6909, revision=11, flash_result=0):
        self.assertIn(mode, {"--preflight", "--verify-boot"})
        with tempfile.TemporaryDirectory(dir=PROJECT / "workspace/test_artifacts") as temporary, LoopbackDrive() as drive:
            folder = Path(temporary)
            image = folder / "compiled.bin"
            image.write_bytes(struct.pack("<4I", 0xEE3355BB, IMAGE_BYTES-4, 0x80000000, 0x80000000) + bytes(IMAGE_BYTES-16))
            builds = FirmwareBuilds(folder)
            build = builds.register(str(image), "loopback only", "6909", expected_parameter_version_raw=11)
            journal = folder / "journal.jsonl"
            drive.release, drive.parameter_version, drive.flash_result = release, revision, flash_result
            drive.short_a1_request = True
            drive.values[(0x22, 11)] = 900
            command = [str(BRIDGE), str(UPDATER), build["firmware_path"], "TCP", str(journal), mode,
                       "--tcp", "127.0.0.2", MAC, "127.0.0.1", "--contract", str(builds.root / build["build_id"] / "build.json")]
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, cwd=BRIDGE.parent,
                                       timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
            events = [json.loads(line)["data"] for line in journal.read_text(encoding="utf-8-sig").splitlines()]
            self.assertFalse(drive.errors, (drive.errors, events))
            self.assertEqual(drive.writes, [])
            # F2/F1 must never occur in these tests.
            self.assertFalse(any(bytes.fromhex(packet)[:3] in {bytes.fromhex("FF55FB"), bytes.fromhex("FF55FA")} for _, packet in drive.request_times))
            return completed.returncode, events

    def test_registered_digest_preflight_accepts_stopped_s5(self):
        code, events = self.check_mode("--preflight")
        self.assertEqual(code, 0, events)
        preflight = next(e for e in events if e.get("phase") == "preflight_passed")
        self.assertEqual(preflight["state_raw"], 5)
        self.assertTrue(events[0]["registered_build"])
        self.assertFalse(events[-1]["device_write_commands_sent"])

    def test_registered_boot_checks_release_and_parameter_revision(self):
        code, events = self.check_mode("--verify-boot")
        self.assertEqual(code, 0, events)
        self.assertEqual(events[-1]["status"], "boot_verified")
        self.assertEqual(events[-1]["parameter_version_raw"], 11)
        self.assertEqual(events[-1]["release_hex"], "6909")

    def test_old_release_does_not_verify_new_build(self):
        code, events = self.check_mode("--verify-boot", release=0x6908)
        self.assertNotEqual(code, 0, events)
        self.assertIn("release/software", events[-1]["error"])

    def test_unrebooted_upgrade_flag_does_not_verify(self):
        code, events = self.check_mode("--verify-boot", flash_result=1)
        self.assertNotEqual(code, 0, events)
        self.assertIn("physical power cycle", events[-1]["error"])

    def test_wrong_parameter_revision_does_not_verify(self):
        code, events = self.check_mode("--verify-boot", revision=12)
        self.assertNotEqual(code, 0, events)
        self.assertIn("parameter revision", events[-1]["error"])


if __name__ == "__main__":
    unittest.main()
