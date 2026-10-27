from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from boneng_mcp.device import DeviceOperations
from boneng_mcp.paths import UserInputError


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.network, self.firmware = Mock(), Mock()
        self.selection = {"selected": True, "device": {"mac": "02:00:00:00:00:02", "ip": "192.0.2.2"},
                          "interface_ip": "192.0.2.205", "scan_id": "1" * 32, "device_id": "D02"}
        self.network.selection.side_effect = lambda: copy.deepcopy(self.selection)
        self.device = DeviceOperations(PROJECT, Path(self.temporary.name), PROJECT / "data", self.network, self.firmware)
        # A file pin is tested without requiring the Windows compiler/runtime.
        self.device.bridge = Path(self.temporary.name) / "bridge.exe"
        self.device.bridge.write_bytes(b"test bridge identity")
        self.parameter = {"code": "B04.00", "raw_value": 1000, "attribute_raw": 4104, "access": 1,
                          "name": "在线测试参数", "group": 0x34, "offset": 0,
                          "decimal_attribute_raw": 2, "width_bits": 16, "signed": False,
                          "lower_raw": 0, "upper_raw": 10000, "limit_is_reference": False, "reboot_required": False}
        self.identity = {"mac": self.selection["device"]["mac"], "parameter_version_raw": 11, "platform_type": 9,
                         "communication_type": 3, "software_raw": 910, "release_hex": "6908"}
        self.report = {"identity": self.identity, "release_hex": "6908", "device_write_commands_sent": False, "result": {"parameters": [self.parameter]}}
        self.motion_limits = {"rated_rpm": 3000, "motor_max_rpm": 6000, "base_rpm": 3000, "rated_frequency_raw": 20000}
        self.catalog = {"catalog_id": "test", "identity": self.identity, "target": self.device._target("selected_tcp"),
                        "parameters": [{**self.parameter, "high_word_of": None, "reserved": False, "group_name": "设备 B04"},
                                       {"code": "P11.03", "group": 0xEB, "offset": 3, "name": "当前设备通道", "group_name": "设备 P11", "width_bits": 16, "access": 2, "reserved": False, "high_word_of": None},
                                       {"code": "P13.45", "group": 0xED, "offset": 45, "name": "双字", "width_bits": 32, "high_word_of": None},
                                       {"code": "P13.46", "group": 0xED, "offset": 46, "name": "双字", "width_bits": 32, "high_word_of": "P13.45"}],
                        "complete": True, "loaded_groups": ["B04", "P11", "P13"], "captured_at": 1, "identity_verified_at": 2,
                        "catalog_file": "test.json", "origin": "live_device_B6_B4_B5_B1_B3"}
        self.catalog_patch = patch.object(self.device, "_catalog", return_value=self.catalog)
        self.catalog_mock = self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)

    def test_channel_listing_uses_selected_device_catalog(self):
        result = self.device.channels(group="P11", limit=100)
        self.assertFalse(result["reference_only"])
        self.assertTrue(result["hardware_contacted"])
        self.assertFalse(result["capture_started"])
        self.assertIn("P11.03", [p["code"] for p in result["channels"]])
        self.assertTrue(all(p["width_bits"] == 16 for p in result["channels"]))
        self.assertEqual(result["channels"][0]["name"], "当前设备通道")
        self.catalog_mock.assert_called_once_with("selected_tcp", "COM7", ["P11"])

    def test_invalid_addresses_and_32bit_high_word_rejected(self):
        for codes in [[], ["B04.00", "B04.00"], ["A99.00"], ["../"], ["P13.46"], [True]]:
            with self.subTest(codes=codes), self.assertRaises(UserInputError):
                self.device._addresses(codes, self.catalog)

    def test_project_only_code_is_not_accepted(self):
        with self.assertRaisesRegex(UserInputError, "not advertised"):
            self.device._addresses(["B04.01"], self.catalog)

    def test_same_code_can_have_device_specific_name_width_and_address(self):
        different = copy.deepcopy(self.catalog)
        different["parameters"][0].update(name="其他设备的含义", width_bits=32, group=0x36)
        with patch.object(self.device, "_catalog", return_value=different):
            found = self.device.search_device_parameters("其他设备", group="B04")
        self.assertEqual(found["items"][0]["name"], "其他设备的含义")
        self.assertEqual(found["items"][0]["width_bits"], 32)
        self.assertEqual(self.device._addresses(["B04.00"], different)[0]["group"], 0x36)

    def test_parameter_revision_change_blocks_prepared_write(self):
        with patch.object(self.device, "_run", return_value=self.report) as run:
            plan = self.device.prepare_write("B04.00", "12")
            changed = copy.deepcopy(self.catalog)
            changed["identity"]["parameter_version_raw"] = 12
            with patch.object(self.device, "_catalog", return_value=changed), self.assertRaisesRegex(UserInputError, "version changed"):
                self.device.apply_write(plan["plan_id"])
            self.assertEqual(run.call_count, 1)

    def test_address_mapping_change_blocks_prepared_write(self):
        with patch.object(self.device, "_run", return_value=self.report) as run:
            plan = self.device.prepare_write("B04.00", "12")
            changed = copy.deepcopy(self.catalog)
            changed["parameters"][0]["group"] = 0x36
            with patch.object(self.device, "_catalog", return_value=changed), self.assertRaisesRegex(UserInputError, "address mapping changed"):
                self.device.apply_write(plan["plan_id"])
            self.assertEqual(run.call_count, 1)

    def test_partial_catalog_groups_merge_without_marking_old_definitions_fresh(self):
        groups = [{"code": "B04", "group": 0x34, "name": "在线组", "parameter_words": 1},
                  {"code": "P11", "group": 0xEB, "name": "在线通道组", "parameter_words": 1}]
        def bridge(request):
            if request["operation"] == "identity":
                return {"identity": self.identity}
            parameter = next(copy.deepcopy(p) for p in self.catalog["parameters"] if p["code"][:3] == request["group_filter"])
            return {"identity": self.identity, "journal_dir": "test", "result": {"definitions_complete_for_scope": True,
                    "major_groups": [], "groups": groups, "parameters": [parameter]}}
        with patch.object(self.device, "_run", side_effect=bridge):
            first = DeviceOperations._catalog(self.device, groups=["B04"])
            merged = DeviceOperations._catalog(self.device, groups=["P11"])
        self.assertFalse(first["complete"])
        self.assertTrue(merged["complete"])
        self.assertEqual({p["code"] for p in merged["parameters"]}, {"B04.00", "P11.03"})
        self.assertEqual(merged["group_captured_at"]["B04"], first["group_captured_at"]["B04"])
        self.assertEqual(merged["captured_at"], first["captured_at"])

    def test_cache_is_invalidated_by_device_parameter_revision(self):
        group = {"code": "B04", "group": 0x34, "name": "在线组", "parameter_words": 1}
        current = {"identity": copy.deepcopy(self.identity)}
        calls = []
        def bridge(request):
            calls.append(request["operation"])
            if request["operation"] == "identity":
                return copy.deepcopy(current)
            parameter = copy.deepcopy(self.catalog["parameters"][0])
            parameter["name"] = "设备版本" + str(current["identity"]["parameter_version_raw"])
            return {**copy.deepcopy(current), "journal_dir": "test", "result": {"definitions_complete_for_scope": True,
                    "major_groups": [], "groups": [group], "parameters": [parameter]}}
        with patch.object(self.device, "_run", side_effect=bridge):
            first = DeviceOperations._catalog(self.device, groups=["B04"])
            cached = DeviceOperations._catalog(self.device, groups=["B04"])
            self.assertEqual(calls, ["identity", "catalog", "identity"])
            self.assertEqual(first["catalog_id"], cached["catalog_id"])
            current["identity"]["parameter_version_raw"] = 12
            fresh = DeviceOperations._catalog(self.device, groups=["B04"])
        self.assertEqual(calls[-2:], ["identity", "catalog"])
        self.assertNotEqual(first["catalog_id"], fresh["catalog_id"])
        self.assertEqual(fresh["parameters"][0]["name"], "设备版本12")

    def test_identity_change_during_discovery_does_not_publish_cache(self):
        changed = {**self.identity, "parameter_version_raw": 12}
        with patch.object(self.device, "_run", side_effect=[{"identity": self.identity}, {"identity": changed}]), self.assertRaisesRegex(UserInputError, "changed during"):
            DeviceOperations._catalog(self.device)
        self.assertFalse((self.device.root / "catalogs").exists())

    def test_exact_write_preview_and_one_use(self):
        with patch.object(self.device, "_run", return_value=self.report) as run:
            plan = self.device.prepare_write("B04.00", "12.34")
            self.assertEqual(plan["raw_value"], 1234)
            self.assertFalse(plan["device_write_commands_sent"])
            self.assertEqual(run.call_args.args[0]["operation"], "read")
            self.device.apply_write(plan["plan_id"])
            request = run.call_args.args[0]
            self.assertEqual(request["operation"], "write")
            self.assertEqual(request["parameters"][0]["expected_before"], 1000)
            with self.assertRaises(UserInputError):
                self.device.apply_write(plan["plan_id"])

    def test_write_rejects_imprecise_nonfinite_and_out_of_range(self):
        with patch.object(self.device, "_run", return_value=self.report):
            for value in ["1.234", "nan", "inf", "101", "-1"]:
                with self.subTest(value=value), self.assertRaises(UserInputError):
                    self.device.prepare_write("B04.00", value)

    def test_command_registers_do_not_use_generic_write(self):
        with patch.object(self.device, "_run") as run:
            for code in ["P12.11", "P12.14", "A04.31", "A04.32"]:
                with self.subTest(code=code), self.assertRaises(UserInputError):
                    self.device.prepare_write(code, "1")
            run.assert_not_called()

    def test_changed_selection_or_bridge_blocks_plan(self):
        with patch.object(self.device, "_run", return_value=self.report) as run:
            plan = self.device.prepare_write("B04.00", "12")
            self.selection["device"]["mac"] = "00:11:22:33:44:55"
            run.reset_mock()
            with self.assertRaises(UserInputError):
                self.device.apply_write(plan["plan_id"])
            run.assert_not_called()
            self.selection["device"]["mac"] = "02:00:00:00:00:02"
            self.device.bridge.write_bytes(b"changed bridge")
            with self.assertRaises(UserInputError):
                self.device.apply_write(plan["plan_id"])

    def test_capture_requires_supported_channels_and_sample_limits(self):
        with patch.object(self.device, "_run") as run:
            for codes, period, points in [(["A00.06"], 2, 10), (["P11.03"], 1, 10), (["P11.03"], 2, True), (["P11.03"], 2, 20001), (["P11.03"], 1000, 41)]:
                with self.subTest(codes=codes, period=period, points=points), self.assertRaises(UserInputError):
                    self.device.capture(codes, period, points)
            run.assert_not_called()

    def test_motion_requires_concrete_limits_and_local_stop(self):
        with patch.object(self.device, "runtime", return_value=self.report) as runtime:
            for speed, maximum, duration, local in [(300, 300, None, False), (301, 300, None, True), (True, 300, None, True), (300, 300, -1, True)]:
                with self.subTest(speed=speed, maximum=maximum, duration=duration, local=local), self.assertRaises(UserInputError):
                    self.device.plan_run(speed, maximum, "rpm", duration, "unloaded", local)
            runtime.assert_not_called()
            with patch.object(self.device, "motion_limits", return_value={"identity": self.identity, "result": self.motion_limits}):
                plan = self.device.plan_run(300, 300, "rpm", None, "unloaded; user requested continuous running", True)
            self.assertIsNone(plan["duration_s"])
            self.assertEqual(plan["speed_unit"], "rpm")
            with patch.object(self.device, "_start_continuous", return_value={"status": "starting"}) as start:
                self.device.run(plan["plan_id"])
                self.assertEqual(start.call_args.args[0]["speed"], 300)
            with patch.object(self.device, "motion_limits", return_value={"identity": self.identity, "result": self.motion_limits}):
                bounded = self.device.plan_run(300, 300, "rpm", 10, "unloaded", True, background=True)
            with patch.object(self.device, "_start_continuous", return_value={"status": "starting"}) as start:
                self.device.run(bounded["plan_id"])
                self.assertEqual(start.call_args.args[0]["duration_s"], 10)


if __name__ == "__main__":
    unittest.main()
