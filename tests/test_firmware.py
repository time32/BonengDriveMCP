from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from boneng_mcp.firmware import FIRMWARE_SHA, TARGET_MAC, FirmwareUpgrades, _CHILDREN
from boneng_mcp.paths import UserInputError


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.network = Mock()
        self.network.selection.return_value = {"selected": True, "device": {"ip": "192.0.2.2", "mac": "02:00:00:00:00:02"},
                                               "interface_ip": "192.0.2.205", "scan_id": "b" * 32, "device_id": "D02"}
        self.upgrades = FirmwareUpgrades(self.root, self.root, self.root, self.network)
        self.files = {"firmware_path": str(self.root / "verified.bin"), "port": "COM7", "transport": "serial",
                      "target_mac": TARGET_MAC, "firmware_sha256": FIRMWARE_SHA, "bridge_sha256": "bridge", "updater_sha256": "updater"}
        self.child = Mock(pid=1900000000)
        self.child.poll.return_value = None
        running = patch.object(self.upgrades, "_running", side_effect=lambda job: self.child.poll() is None)
        running.start(); self.addCleanup(running.stop)
        self.addCleanup(_CHILDREN.clear)

    def plan(self):
        rows = [{"data": {"phase": "preflight_passed", "mac": TARGET_MAC, "software_raw": 910, "state_raw": 1}}]
        with patch.object(self.upgrades, "_files", return_value=self.files), patch.object(self.upgrades, "_read_mode", return_value=rows):
            return self.upgrades.preflight(self.files["firmware_path"], "serial")

    def start(self, plan):
        with patch.object(self.upgrades, "_files", return_value=self.files), patch("boneng_mcp.firmware.start_detached", return_value={"pid": self.child.pid, "launcher": "simulation"}) as launch:
            job = self.upgrades.start(plan["plan_id"])
            self.assertIn("--execute", launch.call_args.args[0])
            self.assertEqual(launch.call_args.args[1], self.upgrades.bridge.parent)
            return job

    def journal(self, job, records):
        Path(job["journal"]).write_text("\n".join(json.dumps({"data": item}) for item in records), encoding="utf-8")

    def test_preflight_does_not_start_upgrade(self):
        with patch("boneng_mcp.firmware.start_detached") as launch:
            result = self.plan()
            self.assertFalse(result["device_write_commands_sent"])
            self.assertEqual(result["identity"]["mac"], TARGET_MAC)
            launch.assert_not_called()

    def test_expired_and_changed_plans_never_start_process(self):
        plan = self.plan()
        with patch("boneng_mcp.firmware.start_detached") as launch:
            with patch("boneng_mcp.firmware.time.time", return_value=plan["expires_at"] + 1), self.assertRaisesRegex(UserInputError, "expired"):
                self.upgrades.start(plan["plan_id"])
            with patch.object(self.upgrades, "_files", return_value={**self.files, "bridge_sha256": "changed"}), self.assertRaisesRegex(UserInputError, "changed"):
                self.upgrades.start(plan["plan_id"])
            launch.assert_not_called()

    def test_plan_can_only_start_once_and_keeps_existing_job(self):
        plan = self.plan()
        job = self.start(plan)
        with patch.object(self.upgrades, "_files", return_value=self.files), patch("boneng_mcp.firmware.start_detached") as launch:
            with self.assertRaisesRegex(UserInputError, "already been used"):
                self.upgrades.start(plan["plan_id"])
            launch.assert_not_called()
        self.assertEqual(self.upgrades._active()["job_id"], job["job_id"])

    def test_transfer_completion_is_not_flash_success(self):
        job = self.start(self.plan())
        self.journal(job, [{"phase": "transfer_finished", "complete_flag": 1, "packet": 100, "total": 101}])
        self.assertEqual(self.upgrades.status(job["job_id"])["status"], "running")
        self.child.poll.return_value = 0
        result = self.upgrades.status(job["job_id"])
        self.assertEqual(result["status"], "result_unknown")
        self.assertFalse(result["flash_readback_verified"])

    def test_flash_success_requires_matching_mac_and_hash(self):
        job = self.start(self.plan())
        terminal = {"status": "flash_verified", "firmware_sha256": FIRMWARE_SHA, "mac": TARGET_MAC, "device_flash_readback_passed": True}
        self.journal(job, [terminal])
        result = self.upgrades.status(job["job_id"])
        self.assertTrue(result["flash_readback_verified"])
        self.assertTrue(result["requires_manual_power_cycle"])
        self.assertFalse(result["automatic_restart_sent"])
        self.journal(job, [{**terminal, "mac": "00:11:22:33:44:55"}])
        with self.assertRaisesRegex(UserInputError, "contract"):
            self.upgrades.status(job["job_id"])

    def test_boot_cannot_be_checked_before_flash_success(self):
        job = self.start(self.plan())
        with patch.object(self.upgrades, "_read_mode") as reader, self.assertRaises(UserInputError):
            self.upgrades.verify_boot(job["job_id"])
        reader.assert_not_called()

    def test_history_is_not_a_new_upgrade(self):
        self.upgrades.historical.write_text(json.dumps({"status": "flash_and_boot_verified"}), encoding="utf-8")
        with patch("boneng_mcp.firmware.start_detached") as launch:
            result = self.upgrades.status()
            self.assertEqual(result["status"], "historical_verified_run")
            self.assertFalse(result["hardware_contacted"])
            self.assertFalse(result["new_upgrade_started"])
            launch.assert_not_called()

    def test_missing_selection_and_invalid_ids_never_connect(self):
        self.network.selection.return_value = {"selected": False}
        with patch("boneng_mcp.firmware.subprocess.run") as launch:
            with self.assertRaisesRegex(UserInputError, "select"):
                self.upgrades.preflight("anything.bin", "selected_tcp")
            for value in ["../outside", "", "A" * 32]:
                with self.subTest(value=value), self.assertRaises(UserInputError):
                    self.upgrades.start(value)
            launch.assert_not_called()

    def test_tcp_command_pins_selected_ip_mac_and_interface(self):
        target = self.upgrades._selected_target()
        files = {**self.files, **target, "transport": "tcp", "port": "TCP"}
        command = self.upgrades._command(files, self.root / "test.jsonl", "--preflight")
        self.assertEqual(command[-4:], ["--tcp", "192.0.2.2", "02:00:00:00:00:02", "192.0.2.205"])
        self.assertIn("--preflight", command)
        self.assertNotIn("--execute", command)

    def test_wrong_preflight_identity_is_rejected(self):
        wrong = [{"data": {"phase": "preflight_passed", "mac": "00:11:22:33:44:55", "software_raw": 910, "state_raw": 1}}]
        with patch.object(self.upgrades, "_files", return_value=self.files), patch.object(self.upgrades, "_read_mode", return_value=wrong), self.assertRaisesRegex(UserInputError, "identity"):
            self.upgrades.preflight(self.files["firmware_path"], "serial")

    def test_invalid_images_are_rejected_without_device_access(self):
        image = self.root / "invalid.bin"
        image.write_bytes(b"invalid")
        with patch.object(self.upgrades, "describe", return_value={"configured": True}), patch("boneng_mcp.firmware.TARGET_MAC", "02:00:00:00:00:01"), patch("boneng_mcp.firmware.subprocess.run") as launch:
            with self.assertRaisesRegex(UserInputError, "SPI image"):
                self.upgrades.preflight(str(image), "serial")
            launch.assert_not_called()

    def test_another_server_start_lock_is_preserved(self):
        plan = self.plan()
        lock = self.upgrades.root / "start.lock"
        lock.write_text("another-server", encoding="utf-8")
        with patch.object(self.upgrades, "_files", return_value=self.files), patch("boneng_mcp.firmware.start_detached") as launch:
            with self.assertRaisesRegex(UserInputError, "Another MCP server"):
                self.upgrades.start(plan["plan_id"])
            launch.assert_not_called()
        self.assertEqual(lock.read_text(encoding="utf-8"), "another-server")

    def test_changing_selected_tcp_device_invalidates_upgrade_plan(self):
        target = self.upgrades._selected_target()
        files = {**self.files, **target, "transport": "tcp", "port": "TCP"}
        rows = [{"data": {"phase": "preflight_passed", "mac": files["target_mac"], "software_raw": 910, "state_raw": 1}}]
        with patch.object(self.upgrades, "_files", return_value=files), patch.object(self.upgrades, "_read_mode", return_value=rows):
            plan = self.upgrades.preflight(files["firmware_path"], "selected_tcp")
        changed = {**files, "host": "192.0.2.3", "target_mac": "00:11:22:33:44:55"}
        with patch.object(self.upgrades, "_files", return_value=changed), patch("boneng_mcp.firmware.start_detached") as launch:
            with self.assertRaisesRegex(UserInputError, "changed"):
                self.upgrades.start(plan["plan_id"])
            launch.assert_not_called()

    def test_boot_verification_uses_read_only_mode_and_checks_contract(self):
        job = self.start(self.plan())
        self.journal(job, [{"status": "flash_verified", "firmware_sha256": FIRMWARE_SHA, "mac": TARGET_MAC, "device_flash_readback_passed": True}])
        def boot(files, journal, mode, expected_status):
            self.assertEqual(mode, "--verify-boot")
            journal.write_text(json.dumps({"data": {"status": "boot_verified", "mac": TARGET_MAC,
                                                   "device_write_commands_sent": False, "upgrade_status_reset_to_zero": True}}), encoding="utf-8")
        with patch.object(self.upgrades, "_files", return_value=self.files), patch.object(self.upgrades, "_read_mode", side_effect=boot):
            result = self.upgrades.verify_boot(job["job_id"])
        self.assertEqual(result["status"], "boot_verified")
        self.assertFalse(result["requires_manual_power_cycle"])

    def test_preflight_accepts_stopped_s5_but_rejects_active_run_bit(self):
        rows = [{"data": {"phase": "preflight_passed", "mac": TARGET_MAC, "software_raw": 910, "state_raw": 5, "run_status_word": 0}}]
        with patch.object(self.upgrades, "_files", return_value=self.files), patch.object(self.upgrades, "_read_mode", return_value=rows):
            self.assertEqual(self.upgrades.preflight(self.files["firmware_path"], "serial")["identity"]["state_raw"], 5)
            rows[0]["data"]["run_status_word"] = 4
            with self.assertRaises(UserInputError): self.upgrades.preflight(self.files["firmware_path"], "serial")

    def test_registered_flash_and_boot_use_own_digest_and_identity(self):
        self.files.update(build_id="a"*32, firmware_sha256="b"*64, expected_release_hex="6909", expected_software_raw=910, expected_parameter_version_raw=11)
        job = self.start(self.plan())
        self.journal(job, [{"status": "flash_verified", "firmware_sha256": "b"*64, "mac": TARGET_MAC, "device_flash_readback_passed": True}])
        self.assertEqual(self.upgrades.status(job["job_id"])["status"], "flash_verified")
        boot = {"status": "boot_verified", "mac": TARGET_MAC, "device_write_commands_sent": False,
                "upgrade_status_reset_to_zero": True, "firmware_sha256": "b"*64, "software_raw": 910, "release_hex": "6908", "parameter_version_raw": 11}
        path = Path(job["journal"]).with_name("boot.jsonl")
        path.write_text(json.dumps({"data": boot}))
        with self.assertRaisesRegex(UserInputError, "registered build"): self.upgrades.status(job["job_id"])
        boot["release_hex"] = "6909"; path.write_text(json.dumps({"data": boot}))
        self.assertEqual(self.upgrades.status(job["job_id"])["status"], "boot_verified")
        identity = {"mac": TARGET_MAC, "platform_type": 9, "communication_type": 0, "software_raw": 910, "release_hex": "6909", "parameter_version_raw": 11}
        self.assertEqual(self.upgrades.capture_profile(TARGET_MAC, identity), "an_pn_spi_910")
        self.assertIsNone(self.upgrades.capture_profile(TARGET_MAC, {**identity, "parameter_version_raw": 12}))


if __name__ == "__main__":
    unittest.main()
