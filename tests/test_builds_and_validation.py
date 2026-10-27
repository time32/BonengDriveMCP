from __future__ import annotations

import copy
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from boneng_mcp.builds import FirmwareBuilds, IMAGE_BYTES, sha256
from boneng_mcp.device import DeviceOperations
from boneng_mcp.firmware import FirmwareUpgrades, UPDATER_SHA
from boneng_mcp.paths import Settings, UserInputError
from boneng_mcp.validation import FirmwareValidation
from boneng_mcp.validation_guard import check_validation_access


class BuildAndValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = Settings(self.root / "vendor", PROJECT / "data", self.root / "workspace")
        self.builds = FirmwareBuilds(self.settings.workspace_dir)
        self.image = self.root / "compiled.bin"
        self.image.write_bytes(struct.pack("<4I", 0xEE3355BB, IMAGE_BYTES-4, 0x80000000, 0x80000000) + bytes(IMAGE_BYTES-16))
        self.build = self.builds.register(str(self.image), "diagnostic build", "6909", expected_parameter_version_raw=11, source_revision="test revision")
        self.target = {"target_mac": "02:00:00:00:00:02", "host": "192.0.2.2", "interface_ip": "192.0.2.205", "scan_id": "a"*32, "device_id": "D02"}
        self.identity = {"mac": self.target["target_mac"], "platform_type": 9, "communication_type": 3,
                         "software_raw": 910, "release_hex": "6909", "parameter_version_raw": 11}
        self.firmware = Mock()
        self.firmware.builds = self.builds
        self.firmware.describe.return_value = {"configured": True}
        self.firmware._selected_target.side_effect = lambda: copy.deepcopy(self.target)
        self.firmware.preflight.return_value = {"plan_id": "c"*32}
        self.firmware.start.return_value = {"job_id": "d"*32}
        self.firmware.status.return_value = {"status": "flash_verified"}
        self.firmware.verify_boot.return_value = {"status": "boot_verified"}
        self.device = Mock()
        self.device._validate_codes.side_effect = DeviceOperations._validate_codes
        self.device.describe.return_value = {"configured": True}
        self.bridge = self.root / "bridge.exe"; self.bridge.write_bytes(b"fixture bridge")
        self.firmware.bridge = self.device.bridge = self.bridge
        self.device.discover_catalog.side_effect = lambda *args: {"identity": copy.deepcopy(self.identity)}
        self.device.read.return_value = {"device_write_commands_sent": False, "result": {"parameters": []}}
        self.capture_number = 0
        self.channel_name = "diagnostic signal"
        def capture(*args):
            self.capture_number += 1
            path = self.settings.workspace_dir / "waveforms" / (str(self.capture_number) + ".csv")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("time_s,P11.03\n0," + str(self.capture_number) + "\n0.002,2\n0.004,3\n", encoding="utf-8")
            return {"csv_file": str(path), "result": {"definitions": [{"name": self.channel_name, "width_bits": 16, "unit": "", "signed": True}], "scope_stopped": True}}
        self.device.capture.side_effect = capture
        self.snapshots = Mock()
        self.snapshots.create.side_effect = [{"snapshot_id": "e"*32}, {"snapshot_id": "f"*32}]
        self.snapshots.compare.return_value = {"counts": {"unchanged": 10}}
        self.validation = FirmwareValidation(self.settings, self.firmware, self.device, self.snapshots)
        self.clock = 0

    def plan(self, **extra):
        return self.validation.prepare(self.build["build_id"], "investigate signal", ["P11.03"], 2, 3, backup_configuration=False, **extra)

    def start(self, plan):
        process = Mock(); process.create_time.return_value = 123.0
        def launch(command, cwd, folder):
            process.cmdline.return_value = command
            return {"pid": 123456, "launcher": "simulation"}
        with patch("boneng_mcp.validation.start_detached", side_effect=launch), patch("psutil.Process", return_value=process):
            result = self.validation.start(plan["plan_id"])
        self.assertEqual(result["status"], "starting")
        return result

    def execute(self, job):
        def sleep(seconds): self.clock += seconds
        return self.validation.execute(job["job_id"], sleep=sleep, now=lambda: self.clock)

    def test_registered_image_is_sealed_from_later_compiles(self):
        expected = self.build["firmware_sha256"]
        self.image.write_bytes(b"next compile")
        self.assertEqual(self.builds.get(self.build["build_id"])["firmware_sha256"], expected)
        sealed = Path(self.build["firmware_path"]); sealed.write_bytes(b"tampered")
        with self.assertRaises(UserInputError): self.builds.get(self.build["build_id"])

    def test_bad_layout_and_incompatible_software_are_rejected(self):
        self.image.write_bytes(b"wrong container")
        with self.assertRaises(UserInputError): self.builds.register(str(self.image), "bad", "6909")
        for release, software in [("oops", 910), ("6909", 920), ("6909", True)]:
            with self.assertRaises(UserInputError): self.builds.register(str(self.image), "bad", release, software)

    def test_new_hash_requires_registration_and_contract_is_pinned(self):
        upgrades = FirmwareUpgrades(self.settings.release_dir, self.settings.workspace_dir, self.settings.data_dir)
        upgrades.updater.parent.mkdir(parents=True, exist_ok=True); upgrades.updater.write_bytes(b"fake updater")
        upgrades.bridge = self.bridge
        real_sha = sha256
        with patch.object(upgrades, "describe", return_value={"configured": True}), patch("boneng_mcp.firmware.TARGET_MAC", "02:00:00:00:00:01"), patch("boneng_mcp.firmware._sha", side_effect=lambda p: UPDATER_SHA if p == upgrades.updater else real_sha(p)):
            with self.assertRaises(UserInputError): upgrades._files(str(self.image), "COM7")
            files = upgrades._files("", "COM7", build_id=self.build["build_id"])
        self.assertEqual(files["firmware_sha256"], self.build["firmware_sha256"])
        self.assertEqual(files["expected_release_hex"], "6909")
        self.assertIn("--contract", upgrades._command(files, self.root / "journal", "--preflight"))

    def test_prepare_is_offline_and_start_is_single_use(self):
        plan = self.plan()
        self.assertFalse(plan["hardware_contacted"])
        self.firmware.preflight.assert_not_called(); self.device.capture.assert_not_called()
        job = self.start(plan); self.execute(job)
        with patch("boneng_mcp.validation.start_detached") as launch, self.assertRaisesRegex(UserInputError, "already started"):
            self.validation.start(plan["plan_id"])
        launch.assert_not_called()
        self.firmware.start.assert_called_once()

    def test_waits_for_verified_reboot_before_capture_and_creates_report(self):
        job = self.start(self.plan())
        self.firmware.status.side_effect = [{"status": "running"}, {"status": "flash_verified"}]
        self.firmware.verify_boot.side_effect = [UserInputError("F3 still 1, no reboot"), {"status": "boot_verified"}]
        result = self.execute(job)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.clock, 5)
        self.firmware.start.assert_called_once(); self.device.capture.assert_called_once()
        report = self.validation.status(job["job_id"])["report"]
        self.assertFalse(report["root_cause_diagnosed"])
        self.assertEqual(report["motor_control"], "none")
        self.assertEqual(report["statistics"]["samples"], 3)

    def test_cm55_flash_reboot_capture_sequence_retains_serial_binding(self):
        from boneng_mcp.builds import CM55_BUILD_PROFILE
        from test_cm55 import TARGET, FINGERPRINT, CM55_PROFILE
        self.build=self.builds.register(str(self.image),"CM55 diagnostic build","6630",612,612,profile=CM55_BUILD_PROFILE)
        selected=copy.deepcopy(TARGET)
        self.device._target.side_effect=lambda *args: copy.deepcopy(selected)
        self.identity={**FINGERPRINT,"mac":None,"device_profile":CM55_PROFILE,"serial_binding_id":selected["serial_binding_id"],
                       "software_raw":612,"release_hex":"6630","parameter_version_raw":612}
        plan=self.plan(transport="serial")
        result=self.execute(self.start(plan))
        self.assertEqual(result["status"],"completed",result)
        self.firmware.start.assert_called_once(); self.device.capture.assert_called_once()
        report=self.validation.status(result["job_id"])["report"]
        self.assertIsNone(report["target"]["target_mac"])
        self.assertEqual(report["target"]["serial_binding_id"],selected["serial_binding_id"])
        preview=self.plan(transport="serial")
        selected["serial_binding_id"]="b"*32
        with self.assertRaisesRegex(UserInputError,"target changed"): self.validation.start(preview["plan_id"])

    def test_wrong_release_prevents_capture(self):
        job = self.start(self.plan()); self.identity["release_hex"] = "6908"
        result = self.execute(job)
        self.assertEqual(result["status"], "failed")
        self.assertIn("catalog identity", result["error"])
        self.firmware.verify_boot.assert_called_once()
        self.device.capture.assert_not_called()

    def test_reboot_timeout_does_not_capture_or_repeat_flash(self):
        job = self.start(self.plan(wait_timeout_s=30))
        self.firmware.verify_boot.side_effect = UserInputError("wrong firmware or not rebooted")
        self.assertEqual(self.execute(job)["status"], "timed_out")
        self.firmware.start.assert_called_once(); self.device.capture.assert_not_called()

    def test_cancel_during_flash_waits_for_flash_result_then_stops(self):
        job = self.start(self.plan())
        def start_upgrade(plan_id):
            (self.validation.root / "jobs" / job["job_id"] / "cancel.request").touch()
            return {"job_id": "d"*32}
        self.firmware.start.side_effect = start_upgrade
        self.firmware.status.side_effect = [{"status": "running"}, {"status": "flash_verified"}]
        self.assertEqual(self.execute(job)["status"], "cancelled")
        self.assertEqual(self.firmware.status.call_count, 2)
        self.firmware.verify_boot.assert_not_called(); self.device.capture.assert_not_called()

    def test_cancel_before_start_prevents_all_device_actions(self):
        job = self.start(self.plan()); self.validation.cancel(job["job_id"])
        # With no actual worker in this fixture, cancellation is persisted directly.
        self.assertEqual(self.validation.status(job["job_id"])["status"], "cancelled")
        self.firmware.start.assert_not_called()

    def test_changed_target_blocks_start_and_independent_device_access(self):
        plan = self.plan(); self.target["host"] = "192.0.2.3"
        with self.assertRaises(UserInputError): self.validation.start(plan["plan_id"])
        self.target["host"] = "192.0.2.2"
        self.start(plan)
        with self.assertRaisesRegex(UserInputError, "owns"):
            check_validation_access(self.settings.workspace_dir)

    def test_optional_backups_compare_actual_round_configuration(self):
        plan = self.validation.prepare(self.build["build_id"], "config regression", ["P11.03"], 2, 3)
        self.assertEqual(self.execute(self.start(plan))["status"], "completed")
        self.assertEqual(self.snapshots.create.call_count, 2)
        self.snapshots.compare.assert_called_once_with("e"*32, "f"*32)

    def test_cross_round_definition_changes_disable_numeric_delta(self):
        first = self.start(self.plan()); self.execute(first)
        self.channel_name = "different meaning"
        second = self.start(self.plan(baseline_job_id=first["job_id"])); self.execute(second)
        report = self.validation.status(second["job_id"])["report"]
        self.assertFalse(report["baseline_waveform_comparable"])
        self.assertNotIn("waveform_statistic_changes", report)

    def test_comparable_rounds_record_numerical_delta(self):
        first = self.start(self.plan()); self.execute(first)
        second = self.start(self.plan(baseline_job_id=first["job_id"]))
        self.assertEqual(self.execute(second)["status"], "completed")
        report = self.validation.status(second["job_id"])["report"]
        self.assertTrue(report["baseline_waveform_comparable"])
        self.assertAlmostEqual(report["waveform_statistic_changes"]["P11.03"]["mean"], 1/3)

    def test_flash_failure_never_verifies_boot_or_retries(self):
        job = self.start(self.plan())
        self.firmware.status.return_value = {"status": "failed", "error": "readback failed"}
        result = self.execute(job)
        self.assertEqual(result["status"], "failed")
        self.assertIn("readback failed", result["error"])
        self.firmware.start.assert_called_once()
        self.firmware.verify_boot.assert_not_called(); self.device.capture.assert_not_called()

    def test_bridge_changed_after_start_prevents_all_device_actions(self):
        job = self.start(self.plan()); self.bridge.write_bytes(b"rebuilt bridge")
        self.assertEqual(self.execute(job)["status"], "failed")
        self.firmware.preflight.assert_not_called(); self.device.capture.assert_not_called()

    def test_dead_worker_cancel_does_not_release_active_flash(self):
        job = self.start(self.plan())
        path = self.validation.root / "jobs" / job["job_id"] / "job.json"
        saved = json.loads(path.read_text()); saved["upgrade_job_id"] = "d"*32
        self.validation._save(saved)
        self.firmware.status.return_value = {"status": "running"}
        cancelled = self.validation.cancel(job["job_id"])
        self.assertTrue(cancelled["cancel_requested"])
        self.assertEqual(cancelled["status"], "result_unknown")
        with self.assertRaises(UserInputError): check_validation_access(self.settings.workspace_dir)

    @unittest.skipUnless(sys.platform == "win32", "Windows detached worker integration")
    def test_actual_detached_worker_obeys_cancel_before_any_hardware_contact(self):
        import time
        from boneng_mcp.detached import start_detached
        def cancelled_launch(command, cwd, folder):
            # Before ready is committed the real worker can only wait, then see cancel.
            (folder / "cancel.request").touch()
            return start_detached(command, cwd, folder)
        with patch("boneng_mcp.validation.start_detached", side_effect=cancelled_launch):
            result = self.validation.start(self.plan()["plan_id"])
        deadline = time.monotonic() + 15
        while result["status"] not in {"cancelled", "failed"} and time.monotonic() < deadline:
            time.sleep(0.1)
            result = self.validation.status(result["job_id"])
        self.assertEqual(result["status"], "cancelled", result)
        self.assertFalse(result.get("device_operations_attempted", False))
        self.firmware.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
