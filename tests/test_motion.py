"""Motion encoding, authorization and coordinate-reference regressions."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_device import PROJECT
from boneng_mcp.device import DeviceOperations
from boneng_mcp.motion import duration_ms, position_pulses, speed_encoding
from boneng_mcp.paths import UserInputError

LIMITS = {"motor_data_group": 0, "motor_type_raw": 1, "rated_rpm": 3000, "motor_max_rpm": 6000,
          "base_rpm": 3000, "rated_frequency_raw": 20000, "pole_pairs": 4, "motor_max_percent_raw": 2000, "motor_min_percent_raw": 1000}


class EncodingTests(unittest.TestCase):
    def test_full_rated_speed_uses_percentage_and_never_wraps_signed_word(self):
        for speed in (300, 1500, 3000, -3000):
            with self.subTest(speed=speed):
                encoded = speed_encoding(speed, 3000, "rpm", LIMITS)
                self.assertEqual(encoded["wire_unit"], "percent")
                self.assertEqual(encoded["effective_speed"], speed)
                self.assertLessEqual(abs(encoded["raw"]), 32767)
                self.assertEqual(encoded["control_idle"], 0x847E)

    def test_high_precision_small_session_and_percentage_quantization(self):
        small = speed_encoding(300, 300, "rpm", LIMITS)
        self.assertEqual(small["raw"], 30000)
        self.assertEqual(small["resolution"], .01)
        encoded = speed_encoding(2500.12, 3000, "rpm", LIMITS)
        self.assertLessEqual(encoded["effective_speed"], 2500.12)
        self.assertLess(2500.12 - encoded["effective_speed"], encoded["resolution"])
        negative = speed_encoding(-2500.12, 3000, "rpm", LIMITS)
        self.assertLessEqual(abs(negative["effective_speed"]), 2500.12)

    def test_same_wire_units_and_decimal_bits_for_all_updates(self):
        initial = speed_encoding(300, 3000, "rpm", LIMITS)
        for speed in (0, 1200, 3000, -1500):
            updated = speed_encoding(speed, 3000, "rpm", LIMITS, allow_zero=True, fixed_encoding=initial)
            self.assertEqual(updated["control_idle"], initial["control_idle"])
            self.assertEqual(updated["divisor"], initial["divisor"])

    def test_invalid_speed_and_motor_limit_are_rejected(self):
        for speed, maximum in [(3001, 3000), (300, 3001), (0, 3000), (True, 3000), (float("nan"), 3000), (0.01, 3000)]:
            with self.subTest(speed=speed), self.assertRaises(UserInputError):
                speed_encoding(speed, maximum, "rpm", LIMITS)
        with self.assertRaises(UserInputError):
            speed_encoding(2000, 3000, "rpm", {**LIMITS, "motor_max_rpm": 1800})

    def test_duration_is_optional_and_longer_than_twenty_seconds(self):
        self.assertEqual(duration_ms(None), 0)
        self.assertEqual(duration_ms(3600), 3600000)
        for value in (-1, 0, True, float("inf"), 2147484):
            with self.subTest(value=value), self.assertRaises(UserInputError): duration_ms(value)

    def test_command_pulses_and_explicit_revolution_conversion(self):
        self.assertEqual(position_pulses(10000, "pulses", None), 10000)
        self.assertEqual(position_pulses(1, "revolutions", 10000), 10000)
        self.assertEqual(position_pulses(180, "degrees", 10000), 5000)
        for value, unit, resolution in [(1, "revolutions", None), (0.1, "pulses", None), (2**31, "pulses", None), (True, "pulses", None)]:
            with self.subTest(value=value), self.assertRaises(UserInputError): position_pulses(value, unit, resolution)


class MotionPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.device = DeviceOperations(PROJECT, self.root, PROJECT / "data", Mock(), Mock())
        self.device.bridge = self.root / "bridge.exe"; self.device.bridge.write_bytes(b"motion v2")
        self.target = {"transport": "tcp", "host": "192.0.2.2", "expected_mac": "02:00:00:00:00:02"}
        self.identity = {"mac": self.target["expected_mac"], "software_raw": 910, "release_hex": "6908"}
        self.target_patch = patch.object(self.device, "_target", return_value=self.target); self.target_patch.start(); self.addCleanup(self.target_patch.stop)
        settings = []
        for group, offset, raw, width, signed in [(0x35,1,0,16,False),(0x30,22,0,16,False),(0x83,20,0,32,True),(0x83,22,300,16,False),(0x83,23,500,16,False)]:
            settings.append({"group":group,"offset":offset,"raw_value":raw,"attribute_raw":0,"name":"live setting","access":1,
                "width_bits":width,"signed":signed,"decimal_attribute_raw":0,"lower_raw":0x80000000 if signed else 0,
                "upper_raw":0x7FFFFFFF if signed else 65535,"limit_is_reference":False})
        self.state = {"state_raw":5,"status_word":0,"position_pulses":1234,"position_fingerprint":"stable gearing", "motor_limits":LIMITS,"settings":settings}
        self.report = {"identity":self.identity,"result":self.state,"release_hex":"6908","device_write_commands_sent":False}
        self.run_patch = patch.object(self.device,"_run",side_effect=lambda request:copy.deepcopy(self.report)); self.run_mock=self.run_patch.start(); self.addCleanup(self.run_patch.stop)

    def reference(self):
        return self.device.configure_position_reference("current_stopped_position", -20000, 20000, "user selected software zero")

    def plan(self, position=10000, mode="relative", **extra):
        return self.device.plan_position_move(position, 1200, "unloaded test", True, mode=mode, allow_mode_change=True, **extra)

    def test_continuous_default_and_rated_ceiling_are_read_from_motor(self):
        with patch.object(self.device,"runtime",return_value=self.report), patch.object(self.device,"motion_limits",return_value={"identity":self.identity,"result":LIMITS}):
            plan=self.device.plan_run(1500,motor_load_description="unloaded",local_stop_available=True)
            self.assertIsNone(plan["duration_s"]); self.assertEqual(plan["maximum_abs_speed"],3000)
            self.assertEqual(plan["speed_encoding"]["effective_speed"],1500)
            long=self.device.plan_run(100,300,"rpm",3600,"unloaded",True)
            self.assertTrue(long["background"])

    def test_software_zero_does_not_write_device_or_assume_encoder_homing(self):
        ref=self.reference()
        self.assertEqual(ref["zero_feedback_pulses"],1234)
        self.assertFalse(ref["physical_zero_verified"])
        self.assertFalse(ref["homing_command_sent"])
        self.assertTrue(all(call.args[0]["operation"]=="position_state" for call in self.run_mock.call_args_list))

    def test_no_optional_travel_bounds_only_limits_integer_representation(self):
        ref=self.device.configure_position_reference("current_stopped_position",reference_description="user requested current point as zero")
        self.assertFalse(ref["software_travel_bounds_supplied"])
        self.assertIsNone(ref["pulses_per_revolution"])
        self.assertEqual(self.plan(10000)["target_position_pulses"],11234)

    def test_absolute_and_relative_plans_use_explicit_zero(self):
        self.reference()
        self.state["position_pulses"]=3234
        absolute=self.plan(10000,"absolute"); relative=self.plan(10000,"relative")
        self.assertEqual(absolute["target_position_pulses"],11234)
        self.assertEqual(absolute["displacement_pulses"],8000)
        self.assertEqual(relative["target_position_pulses"],13234)
        self.assertFalse(absolute["device_write_commands_sent"])
        self.assertTrue(absolute["parameter_changes"])

    def test_reference_is_required_and_invalidated_by_restart_or_gearing_change(self):
        with self.assertRaises(UserInputError): self.plan()
        self.reference(); self.state["position_fingerprint"]="changed"
        with self.assertRaisesRegex(UserInputError,"reference expired"): self.plan()
        self.state["position_fingerprint"]="stable gearing"; self.device.motion_session_id="new process"
        with self.assertRaisesRegex(UserInputError,"reference expired"): self.plan()

    def test_position_limits_zero_move_and_mode_change_requirements(self):
        self.reference()
        for value in (0, 30000, -30000):
            with self.subTest(value=value), self.assertRaises(UserInputError): self.plan(value)
        with self.assertRaisesRegex(UserInputError,"Position mode"):
            self.device.plan_position_move(1000,300,"unloaded",True)

    def test_replacing_reference_invalidates_a_prepared_move(self):
        self.reference(); plan=self.plan(); self.reference()
        with patch.object(self.device,"_start_continuous") as start, self.assertRaisesRegex(UserInputError,"reference changed"):
            self.device.execute_position_move(plan["plan_id"])
        start.assert_not_called()

    def test_speed_update_uses_revision_and_blocks_unapproved_direction(self):
        encoding=speed_encoding(300,3000,"rpm",LIMITS)
        run={"job_id":"a"*32,"target":self.target,"identity":self.identity,"operation":"run_continuous","initial_speed":300,
            "allow_direction_change":False,"maximum_abs_speed":3000,"speed_unit":"rpm","motion_limits":LIMITS,"speed_encoding":encoding}
        with patch.object(self.device,"_running_motion",return_value=run), patch.object(self.device,"_worker_request",return_value={"result":{"control_revision":3,"speed_setpoint":300}}) as queue:
            plan=self.device.prepare_speed_change(1500)
            self.assertEqual(plan["expected_control_revision"],3)
            self.device.apply_speed_change(plan["plan_id"])
            self.assertEqual(queue.call_args.args[0]["operation"],"update_speed")
            with self.assertRaises(UserInputError): self.device.apply_speed_change(plan["plan_id"])
            with self.assertRaisesRegex(UserInputError,"direction"): self.device.prepare_speed_change(-300)


if __name__ == "__main__": unittest.main()
