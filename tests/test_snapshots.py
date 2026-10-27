from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from boneng_mcp.device import DeviceOperations
from boneng_mcp.paths import UserInputError
from boneng_mcp.snapshots import ParameterSnapshots


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.device = Mock()
        self.device._validate_search.side_effect = DeviceOperations._validate_search
        self.identity = {"mac": "02:00:00:00:00:02", "platform_type": 9, "parameter_version_raw": 11, "software_raw": 910, "release_hex": "6908"}
        self.definition = {"code": "B04.00", "group": 0x34, "offset": 0, "name": "配置参数", "width_bits": 32,
            "signed": True, "access": 1, "unit_index": 0, "attribute_raw": 123, "reserved": False, "high_word_of": None}
        self.catalog = {"identity": self.identity, "catalog_id": "catalog", "target": {"expected_mac": self.identity["mac"]},
            "groups": [{"code": "B04"}], "parameters": [self.definition]}
        self.device._catalog.return_value = self.catalog
        self.value = {**self.definition, "raw_value": 0xFFFFFFF9, "decimal_attribute_raw": 2}
        self.device._run.return_value = {"identity": self.identity, "device_write_commands_sent": False, "journal_dir": "test", "result": {"parameters": [self.value]}}
        self.snapshots = ParameterSnapshots(Path(self.temporary.name), self.device)

    def test_live_backup_preserves_signed_raw_and_exact_decimal(self):
        result = self.snapshots.create("before tuning")
        saved = json.loads(Path(result["file"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["parameters"][0]["raw_value"], 0xFFFFFFF9)
        self.assertEqual(saved["parameters"][0]["value_exact"], "-0.07")
        self.assertFalse(saved["simultaneous_snapshot"])
        self.assertEqual(self.device._run.call_args.args[0]["operation"], "snapshot")
        self.assertEqual(self.snapshots.list()["total"], 1)
        self.assertEqual(self.snapshots.get(result["snapshot_id"])["items"][0]["value_exact"], "-0.07")

    def test_partial_or_wrong_device_result_does_not_publish_backup(self):
        report = self.device._run.return_value
        report["result"]["parameters"] = []
        with self.assertRaises(UserInputError): self.snapshots.create("incomplete")
        report["identity"] = {**self.identity, "mac": "00:00:00:00:00:00"}
        with self.assertRaises(UserInputError): self.snapshots.create("wrong identity")
        self.assertEqual(self.snapshots.list()["total"], 0)

    def test_scope_excludes_write_only_reserved_high_words_and_monitoring(self):
        self.catalog["parameters"] += [{**self.definition, "code": "P11.03", "access": 2},
            {**self.definition, "code": "B04.01", "high_word_of": "B04.00"},
            {**self.definition, "code": "B04.02", "reserved": True}, {**self.definition, "code": "B04.03", "access": 3}]
        result = self.snapshots.create("config")
        self.assertEqual(result["parameter_count"], 1)
        self.assertEqual(result["excluded_count"], 4)

    def test_comparison_separates_value_changes_from_definition_changes(self):
        first = self.snapshots.create("before")
        self.value["raw_value"] = 0xFFFFFFF8
        second = self.snapshots.create("after")
        diff = self.snapshots.compare(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(diff["counts"]["value_changed"], 1)
        self.assertFalse(diff["restore_supported"])
        self.value["decimal_attribute_raw"] = 1
        third = self.snapshots.create("new scaling")
        diff = self.snapshots.compare(second["snapshot_id"], third["snapshot_id"])
        self.assertEqual(diff["counts"]["definition_changed"], 1)
        self.assertEqual(diff["counts"]["value_changed"], 0)

    def test_missing_parameters_are_reported_without_device_writes(self):
        first = self.snapshots.create("before")
        path = Path(first["file"])
        other = json.loads(path.read_text(encoding="utf-8"))
        other["snapshot_id"] = "a" * 32; other["parameters"] = []; other["parameter_count"] = 0
        (self.snapshots.root / (other["snapshot_id"] + ".json")).write_text(json.dumps(other), encoding="utf-8")
        self.device.reset_mock()
        diff = self.snapshots.compare(first["snapshot_id"], other["snapshot_id"])
        self.assertEqual(diff["counts"]["removed"], 1)
        self.device._run.assert_not_called()

    def test_unknown_group_and_path_escape_are_rejected(self):
        with self.assertRaises(UserInputError): self.snapshots.create("bad group", groups=["B05"])
        with self.assertRaises(UserInputError): self.snapshots.get("../outside")
        with self.assertRaises(UserInputError): self.snapshots.compare("../outside", "b" * 32)

    def test_null_macs_do_not_identify_two_cm55_bindings_as_same_device(self):
        self.identity.update(mac=None,serial_binding_id="a"*32)
        first=self.snapshots.create("CM55 first")
        self.identity["serial_binding_id"]="b"*32
        second=self.snapshots.create("CM55 replacement")
        result=self.snapshots.compare(first["snapshot_id"],second["snapshot_id"])
        self.assertFalse(result["same_device"])
        self.assertFalse(result["physical_unique_identity_verified"])


if __name__ == "__main__":
    unittest.main()
