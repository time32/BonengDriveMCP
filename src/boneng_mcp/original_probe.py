"""A fixed read-only probe that reuses the inspected Drivesoft application."""

from __future__ import annotations

import json
import ipaddress
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .paths import UserInputError


PROJECT = Path(__file__).resolve().parents[2]
EXPECTED_RELEASE_SHA256 = "05b9be5e2e2a2cb74037c32451f89b0117dfa80983e4ff11b7b7c67912553f47"
_PROBE_LOCK = threading.Lock()


def format_mac_words(words: list[int]) -> str:
    """Firmware pdrv_pardatabase.c returns each MAC word's low byte first."""
    if len(words) != 3 or any(type(word) is not int or not 0 <= word <= 0xFFFF for word in words):
        raise UserInputError("MAC address must contain three unsigned 16-bit words.")
    octets = [byte for word in words for byte in (word & 0xFF, word >> 8)]
    return ":".join(f"{byte:02X}" for byte in octets)


class OriginalSoftwareProbe:
    def __init__(self, release_dir: Path, bridge_executable: Path | None = None):
        self.release_executable = release_dir / "BonengDrivesoft.exe"
        self.bridge_executable = bridge_executable or PROJECT / "workspace" / "reuse_bridge" / "ReuseReadProbe.exe"

    def describe(self) -> dict[str, Any]:
        ready = sys.platform == "win32" and self.release_executable.is_file() and self.bridge_executable.is_file()
        return {
            "configured": ready,
            "backend": "original_drivesoft_v1_28",
            "original_executable": str(self.release_executable),
            "bridge_executable": str(self.bridge_executable),
            "required_release_sha256": EXPECTED_RELEASE_SHA256,
            "reads": ["parameter_version", "A02.00", "A00.00", "C05.19", "C05.20", "C05.21"],
            "persistent_connection": False,
            "device_write_supported": False,
            "transports": ["serial", "tcp"],
            "tcp_port": 1000,
            "tcp_requires_selected_scan_mac": True,
        }

    def run(self, port: str = "COM7") -> dict[str, Any]:
        if not isinstance(port, str) or not re.fullmatch(r"COM[1-9][0-9]{0,3}", port):
            raise UserInputError("port must be a Windows serial name such as COM7.")
        return self._run([port])

    def run_tcp(self, host: str, expected_mac: str, interface_ip: str) -> dict[str, Any]:
        try:
            for value in (host, interface_ip):
                address = ipaddress.IPv4Address(value)
                if address.is_unspecified or address.is_multicast or address.is_loopback or str(address) == "255.255.255.255":
                    raise ValueError("Not a unicast device/interface address")
        except (ValueError, TypeError) as error:
            raise UserInputError("TCP requires discovered unicast IPv4 addresses.") from error
        if not isinstance(expected_mac, str) or not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", expected_mac):
            raise UserInputError("TCP requires the selected device's MAC address.")
        return self._run(["--tcp", host, "1000", expected_mac.upper(), interface_ip], expected_mac.upper(), host)

    def _run(self, arguments: list[str], expected_mac: str | None = None, expected_host: str | None = None) -> dict[str, Any]:
        if not self.describe()["configured"]:
            raise UserInputError("Read-only bridge is unavailable. Set BONENG_RELEASE_DIR and run scripts/Build-ReuseBridge.ps1.")
        if not _PROBE_LOCK.acquire(blocking=False):
            raise UserInputError("Another read-only probe is running. Wait for it to finish.")
        try:
            try:
                completed = subprocess.run(
                    [str(self.bridge_executable), str(self.release_executable), *arguments],
                    cwd=self.bridge_executable.parent,
                    capture_output=True,
                    encoding="utf-8-sig",
                    errors="replace",
                    timeout=50,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise UserInputError("Read-only probe timed out; no parameter write was requested.") from error
            except OSError as error:
                raise UserInputError(f"Cannot start the read-only bridge: {error}") from error
            result = None
            for line in completed.stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and "status" in event:
                    result = event
            if result is None:
                raise UserInputError("Read-only bridge returned no result. " + completed.stderr[-1000:])
            if completed.returncode != 0 or result.get("status") != "passed":
                raise UserInputError("Read-only device probe failed: " + str(result.get("error", "unknown bridge error")))
            if result.get("original_sha256") != EXPECTED_RELEASE_SHA256 or result.get("device_write_commands_sent") is not False:
                raise UserInputError("Read-only bridge result did not match the verified backend contract.")
            observations = result.get("observations")
            if not isinstance(observations, list) or len(observations) != 6 or not all(isinstance(item, dict) for item in observations):
                raise UserInputError("Read-only bridge returned incomplete observations. Rebuild it with scripts/Build-ReuseBridge.ps1.")
            version, firmware, state, *mac = observations
            if [item.get("code") for item in observations[1:]] != ["A02.00", "A00.00", "C05.19", "C05.20", "C05.21"]:
                raise UserInputError("Read-only bridge returned unexpected parameter identities.")
            mac_words = [item.get("raw_value") for item in mac]
            actual_mac = format_mac_words(mac_words)
            if expected_mac is not None and (actual_mac != expected_mac or result.get("expected_mac") != expected_mac
                                             or result.get("transport") != "tcp" or result.get("host") != expected_host
                                             or result.get("tcp_port") != 1000):
                raise UserInputError("TCP device identity/endpoint differs from the selected scan result; selection was not changed.")
            return {
                **result,
                "origin": "live_device_read_via_original_software",
                "connection_closed_after_probe": True,
                "parameter_version_raw": version["parameter_version_raw"],
                "firmware_version_raw": firmware["raw_value"],
                "state_raw": state["raw_value"],
                "mac_address": actual_mac,
                "mac_words_raw": mac_words,
                "mac_parameters": [item["code"] for item in mac],
                "catalog_match_verified": False,
            }
        finally:
            _PROBE_LOCK.release()
