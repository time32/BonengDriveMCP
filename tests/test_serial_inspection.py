from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from boneng_mcp.serial_inspection import SerialInspection
from boneng_mcp.original_probe import EXPECTED_RELEASE_SHA256
from boneng_mcp.paths import UserInputError


class SerialInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.firmware = Mock(); self.firmware._active.return_value = None
        self.inspector = SerialInspection(self.root, self.root, self.firmware)
        self.inspector.vendor.write_bytes(b"vendor fixture")
        self.inspector.bridge = self.root / "bridge.exe"; self.inspector.bridge.write_bytes(b"bridge fixture")
        self.identity = {"platform_type": 3, "communication_type": 0, "parameter_version_raw": 612}
        self.definition = {"code": "A02.00", "group": 0x22, "offset": 0, "name": "CM55软件版本", "attribute_raw": 8192, "access": 2, "width_bits": 16, "high_word_of": None}

    def result(self, operation, **extra):
        return {"status": "passed", "operation": "inspect_"+operation, "port": "COM7", "identity": copy.deepcopy(self.identity),
            "original_sha256": EXPECTED_RELEASE_SHA256, "device_write_commands_sent": False, "result": {}, **extra}

    def catalog(self):
        folder = self.root / "catalog-test"; folder.mkdir(exist_ok=True)
        return self.result("catalog", journal_dir=str(folder), result={"definitions_complete_for_scope": True, "groups": [], "parameters": [copy.deepcopy(self.definition)]})

    def test_bad_modes_ports_and_codes_never_contact_device(self):
        for args in [{"mode": "write"}, {"port": "COM0"}, {"port": "COM7;cmd"}, {"mode": "read", "codes": []}, {"mode": "read", "codes": ["A99.00"]}, {"mode": "identity", "codes": ["A02.00"]}]:
            with self.subTest(args=args), patch.object(self.inspector, "_run") as run, self.assertRaises(UserInputError): self.inspector.inspect(**args)
            run.assert_not_called()

    def test_catalog_uses_device_definitions_without_an_identity(self):
        with patch.object(self.inspector, "_run", return_value=self.catalog()) as run:
            result = self.inspector.inspect("catalog", group="A02", query="CM55")
        self.assertEqual(result["items"][0]["name"], "CM55软件版本")
        self.assertEqual(result["identity"], self.identity)
        self.assertTrue(Path(result["catalog_file"]).is_file())
        run.assert_called_once_with("COM7", "catalog", group_filter="A02")

    def test_read_maps_actual_directory_and_rechecks_definition(self):
        self.definition.update(group=0x23, offset=5)
        with patch.object(self.inspector, "_run", side_effect=[self.catalog(), self.result("read", result={"parameters": [{"code": "A02.00", "raw_value": 612}]})]) as run:
            self.inspector.inspect("read", codes=["A02.00"])
        request = run.call_args.kwargs
        self.assertEqual(request["expected_identity"], self.identity)
        self.assertEqual(request["parameters"][0]["group"], 0x23)
        self.assertEqual(request["parameters"][0]["offset"], 5)
        self.assertEqual(request["parameters"][0]["expected_name"], "CM55软件版本")

    def test_missing_high_word_and_write_only_codes_never_reach_value_read(self):
        for changes in [{"code": "A02.01"}, {"high_word_of": "A02.01"}, {"access": 3}]:
            original = copy.deepcopy(self.definition); self.definition.update(changes)
            with patch.object(self.inspector, "_run", return_value=self.catalog()) as run, self.assertRaises(UserInputError): self.inspector.inspect("read", codes=["A02.00"])
            self.assertEqual(run.call_count, 1)
            self.definition = original

    def test_inspection_rejects_mutating_or_wrong_identity_result(self):
        for changes in [{"device_write_commands_sent": True}, {"port": "COM8"}, {"identity": {**self.identity, "platform_type": 99}}, {"original_sha256": "wrong"}]:
            completed = subprocess.CompletedProcess([], 0, json.dumps(self.result("identity", **changes)), "")
            with patch("boneng_mcp.serial_inspection.subprocess.run", return_value=completed), self.assertRaisesRegex(UserInputError, "contract"):
                self.inspector.inspect()

    def test_version_change_between_catalog_and_read_is_rejected(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps(self.result("read", identity={**self.identity, "parameter_version_raw": 613})), "")
        with patch("boneng_mcp.serial_inspection.subprocess.run", return_value=completed), self.assertRaisesRegex(UserInputError, "changed"):
            self.inspector._run("COM7", "read", expected_identity=self.identity, parameters=[])

    def test_successful_identity_is_explicitly_not_unique_device_identification(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps(self.result("identity")), "")
        with patch("boneng_mcp.serial_inspection.subprocess.run", return_value=completed) as launch:
            result = self.inspector.inspect()
        self.assertFalse(result["physical_unique_identity_verified"])
        self.assertFalse(result["write_control_capture_upgrade_supported_by_this_tool"])
        self.assertEqual(launch.call_args.kwargs["stdin"], subprocess.DEVNULL)


if __name__ == "__main__": unittest.main()
