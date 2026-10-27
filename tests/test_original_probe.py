from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from boneng_mcp.original_probe import EXPECTED_RELEASE_SHA256, OriginalSoftwareProbe, format_mac_words
from boneng_mcp.paths import UserInputError


class OriginalProbeTests(unittest.TestCase):
    def setUp(self):
        self.probe = OriginalSoftwareProbe(PROJECT)

    def completed(self, **overrides):
        report = {
            "status": "passed", "original_sha256": EXPECTED_RELEASE_SHA256,
            "device_write_commands_sent": False,
            "observations": [
                {"parameter_version_raw": 11},
                {"code": "A02.00", "raw_value": 910},
                {"code": "A00.00", "raw_value": 1},
                {"code": "C05.19", "raw_value": 0x1234},
                {"code": "C05.20", "raw_value": 0x5678},
                {"code": "C05.21", "raw_value": 0x9ABC},
            ],
            **overrides,
        }
        return subprocess.CompletedProcess([], 0, json.dumps(report), "")

    def test_invalid_ports_do_not_start_a_process(self):
        with patch("boneng_mcp.original_probe.subprocess.run") as launch:
            for port in ["../COM7", "COM0", "COM7;cmd", "COM7\n", "\\\\.\\COM7"]:
                with self.subTest(port=port), self.assertRaises(UserInputError):
                    self.probe.run(port)
            launch.assert_not_called()

    def test_unconfigured_bridge_does_not_start_a_process(self):
        with patch.object(self.probe, "describe", return_value={"configured": False}), patch("boneng_mcp.original_probe.subprocess.run") as launch:
            with self.assertRaisesRegex(UserInputError, "unavailable"):
                self.probe.run()
            launch.assert_not_called()

    def test_wrong_backend_or_write_claim_is_rejected(self):
        with patch.object(self.probe, "describe", return_value={"configured": True}):
            for report in [self.completed(original_sha256="unexpected"), self.completed(device_write_commands_sent=True)]:
                with patch("boneng_mcp.original_probe.subprocess.run", return_value=report), self.assertRaisesRegex(UserInputError, "contract"):
                    self.probe.run()

    def test_timeout_releases_lock_and_next_call_can_succeed(self):
        with patch.object(self.probe, "describe", return_value={"configured": True}), patch("boneng_mcp.original_probe.subprocess.run", side_effect=[subprocess.TimeoutExpired("probe", 50), self.completed()]):
            with self.assertRaisesRegex(UserInputError, "timed out"):
                self.probe.run()
            result = self.probe.run()
            self.assertEqual(result["firmware_version_raw"], 910)
            self.assertEqual(result["origin"], "live_device_read_via_original_software")
            self.assertEqual(result["mac_address"], "34:12:78:56:BC:9A")

    def test_mac_word_order_matches_firmware_octet_order(self):
        self.assertEqual(format_mac_words([0x0102, 0x0304, 0x0506]), "02:01:04:03:06:05")
        for words in [[1, 2], [True, 2, 3], [-1, 2, 3], [65536, 2, 3]]:
            with self.subTest(words=words), self.assertRaises(UserInputError):
                format_mac_words(words)

    def test_mac_response_identity_must_match_requested_parameters(self):
        wrong = json.loads(self.completed().stdout)
        wrong["observations"][-1]["code"] = "C05.22"
        with patch.object(self.probe, "describe", return_value={"configured": True}), patch("boneng_mcp.original_probe.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(wrong), "")):
            with self.assertRaisesRegex(UserInputError, "identities"):
                self.probe.run()

    def test_tcp_read_requires_scan_mac_and_endpoint_match(self):
        expected = "34:12:78:56:BC:9A"
        valid = self.completed(transport="tcp", host="192.0.2.2", tcp_port=1000, expected_mac=expected)
        with patch.object(self.probe, "describe", return_value={"configured": True}), patch("boneng_mcp.original_probe.subprocess.run", return_value=valid) as launch:
            self.assertEqual(self.probe.run_tcp("192.0.2.2", expected, "192.0.2.205")["mac_address"], expected)
            self.assertEqual(launch.call_args.args[0][-5:], ["--tcp", "192.0.2.2", "1000", expected, "192.0.2.205"])
            with self.assertRaisesRegex(UserInputError, "identity/endpoint"):
                self.probe.run_tcp("192.0.2.2", "00:11:22:33:44:55", "192.0.2.205")

    def test_invalid_tcp_endpoints_do_not_start_bridge(self):
        with patch("boneng_mcp.original_probe.subprocess.run") as launch:
            for host, mac in [("localhost", "00:11:22:33:44:55"), ("127.0.0.1", "00:11:22:33:44:55"), ("192.0.2.2", "bad")]:
                with self.subTest(host=host, mac=mac), self.assertRaises(UserInputError):
                    self.probe.run_tcp(host, mac, "192.0.2.205")
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
