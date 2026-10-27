"""Source distribution contents and explicit serial target requirements."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from boneng_mcp.device import DeviceOperations
from boneng_mcp.firmware import FirmwareUpgrades
from boneng_mcp.paths import UserInputError


class DistributionTests(unittest.TestCase):
    def test_allowlist_contains_source_and_excludes_runtime(self):
        spec = importlib.util.spec_from_file_location("source_packaging", PROJECT / "scripts/package_github.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        entries = module.content_files(PROJECT)
        for key in ("README.md", "codex.config.example.toml", "src/boneng_mcp/server.py", "scripts/dotnet/ReuseDeviceBridge.cs"):
            self.assertIn(key, entries)
        self.assertNotIn("codex.config.toml", entries)
        self.assertEqual([name for name in entries if name.startswith("data/")], ["data/README.md"])
        self.assertFalse(any(name.startswith(("workspace/", ".venv/", ".codex/")) for name in entries))
        self.assertFalse(any(Path(name).suffix.lower() in {".bin", ".dll", ".exe", ".mdb", ".docx"} for name in entries))

    def test_missing_an_serial_mac_never_reaches_firmware_io(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            firmware = FirmwareUpgrades(root, root, root)
            with patch("boneng_mcp.firmware.TARGET_MAC", None), patch.object(firmware, "_read_mode") as io:
                with self.assertRaisesRegex(UserInputError, "BONENG_SERIAL_MAC"):
                    firmware.preflight("not-selected.bin", "serial")
                io.assert_not_called()

    def test_missing_an_serial_mac_never_reaches_device_io(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            device = DeviceOperations(root, root, root, None, FirmwareUpgrades(root, root, root))
            with patch("boneng_mcp.device.TARGET_MAC", None), patch.object(device, "_run") as io:
                with self.assertRaisesRegex(UserInputError, "BONENG_SERIAL_MAC"):
                    device.select_serial(profile="an_pn_910")
                io.assert_not_called()

    def test_an_upgrade_command_passes_the_explicit_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            firmware = FirmwareUpgrades(root, root, root)
            files = {"firmware_path": "example.bin", "port": "COM7", "transport": "serial", "target_mac": "02:00:00:00:00:01"}
            command = firmware._command(files, root / "journal", "--preflight")
            self.assertEqual(command[-2:], ["--serial-mac", files["target_mac"]])


if __name__ == "__main__": unittest.main()
