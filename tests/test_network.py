from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from boneng_mcp.network import NetworkDevices
from boneng_mcp.original_probe import OriginalSoftwareProbe
from boneng_mcp.paths import UserInputError


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.probe = Mock(spec=OriginalSoftwareProbe)
        self.network = NetworkDevices(Path(self.temporary.name), self.probe)
        self.row = {"name": "an", "mac": "02:00:00:00:00:02", "ip": "192.0.2.2",
                    "netmask": "255.255.255.0", "gateway": "192.0.2.2", "family": "AN"}

    def scan(self, rows=None):
        with patch.object(self.network, "_worker", return_value={"backend": "pnio_dcp", "version": "1.2.0", "items": rows or [self.row, self.row]}):
            return self.network.scan("192.0.2.205")

    def test_scan_deduplicates_without_selecting_or_connecting(self):
        result = self.scan()
        self.assertEqual(len(result["devices"]), 1)
        self.assertFalse(result["tcp_connections_opened"])
        self.assertFalse(self.network.selection()["selected"])
        self.probe.run_tcp.assert_not_called()

    def test_missing_and_unknown_selection_never_connect(self):
        with self.assertRaises(UserInputError):
            self.network.probe_selected()
        scan = self.scan()
        for scan_id, device_id in [("../outside", "D01"), (scan["scan_id"], "D02")]:
            with self.assertRaises(UserInputError):
                self.network.select(scan_id, device_id)
        self.probe.run_tcp.assert_not_called()

    def test_selection_persists_and_probe_uses_both_ip_and_mac(self):
        scan = self.scan()
        selected = self.network.select(scan["scan_id"], "D01")
        self.assertFalse(selected["device_connected"])
        self.probe.run_tcp.assert_not_called()
        restarted = NetworkDevices(Path(self.temporary.name), self.probe)
        self.probe.run_tcp.return_value = {"status": "passed"}
        result = restarted.probe_selected()
        self.probe.run_tcp.assert_called_once_with("192.0.2.2", "02:00:00:00:00:02", "192.0.2.205")
        self.assertTrue(result["device_identity_matches_discovery"])

    def test_new_scan_does_not_replace_user_selection(self):
        first = self.scan()
        self.network.select(first["scan_id"], "D01")
        self.scan([{**self.row, "ip": "192.0.2.3"}])
        self.assertEqual(self.network.selection()["device"]["ip"], "192.0.2.2")

    def test_unconfigured_ip_is_listed_but_not_selected(self):
        result = self.scan([{**self.row, "ip": "0.0.0.0"}])
        self.assertFalse(result["devices"][0]["tcp_connectable"])
        with self.assertRaisesRegex(UserInputError, "no assigned IP"):
            self.network.select(result["scan_id"], "D01")

    def test_tampered_selection_and_changed_identity_are_rejected(self):
        scan = self.scan()
        self.network.select(scan["scan_id"], "D01")
        path = self.network.root / "selection.json"
        selected = json.loads(path.read_text(encoding="utf-8"))
        selected["device"]["ip"] = "192.0.2.99"
        path.write_text(json.dumps(selected), encoding="utf-8")
        with self.assertRaisesRegex(UserInputError, "differs"):
            self.network.probe_selected()
        self.probe.run_tcp.assert_not_called()

    def test_invalid_scan_arguments_do_not_start_discovery(self):
        with patch.object(self.network, "_worker") as worker:
            for address, timeout in [("127.0.0.1", 5), ("239.0.0.1", 5), ("0.0.0.0", 5), ("192.0.2.1;cmd", 5), ("192.0.2.205", True), ("192.0.2.205", 16)]:
                with self.subTest(address=address, timeout=timeout), self.assertRaises(UserInputError):
                    self.network.scan(address, timeout)
            worker.assert_not_called()

    def test_worker_rejects_configuration_change_claim_and_timeout_recovers(self):
        bad = subprocess.CompletedProcess([], 0, json.dumps({"status": "passed", "backend": "pnio_dcp", "version": "1.2.0", "items": [], "device_configuration_changed": True, "tcp_connections_opened": False}), "")
        with patch("boneng_mcp.network.subprocess.run", return_value=bad) as launch:
            with self.assertRaisesRegex(UserInputError, "contract"):
                self.network.interfaces()
            self.assertEqual(launch.call_args.kwargs["stdin"], subprocess.DEVNULL)
        with patch("boneng_mcp.network.subprocess.run", side_effect=subprocess.TimeoutExpired("scan", 20)), self.assertRaisesRegex(UserInputError, "timed out"):
            self.network.scan("192.0.2.205")
        self.assertEqual(len(self.scan()["devices"]), 1)


if __name__ == "__main__":
    unittest.main()
