"""DCP discovery and explicit local device selection; no automatic TCP connection."""
from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .original_probe import OriginalSoftwareProbe
from .paths import UserInputError, read_json

PROJECT = Path(__file__).resolve().parents[2]
_NETWORK_LOCK = threading.Lock()


def ipv4(value: str, allow_unconfigured: bool = False) -> str:
    try:
        address = ipaddress.IPv4Address(value)
        if address.is_multicast or address.is_loopback or str(address) == "255.255.255.255" or (address.is_unspecified and not allow_unconfigured):
            raise ValueError("Not a usable unicast IPv4 address")
        return str(address)
    except (ValueError, TypeError) as error:
        raise UserInputError("Use an IPv4 address on an active local capture interface or a discovered device.") from error


class NetworkDevices:
    def __init__(self, workspace: Path, probe: OriginalSoftwareProbe):
        self.root = workspace / "network"
        self.probe = probe

    def describe(self) -> dict[str, Any]:
        return {"configured": sys.platform == "win32" and importlib.util.find_spec("pnio_dcp") is not None,
                "discovery_backend": "pnio_dcp==1.2.0 (same library/version as vendor PROFINETScan)",
                "capture_driver_required": "WinPcap or Npcap", "selection_required": True,
                "scan_connects_tcp": False, "persistent_connection": False, "tcp_port": 1000,
                "selection_file": str(self.root / "selection.json")}

    def _worker(self, operation: str, interface_ip: str = "", timeout_s: int = 5) -> dict[str, Any]:
        command = [sys.executable, str(PROJECT / "scripts" / "network_discovery_worker.py"), operation]
        if operation == "scan":
            command += ["--interface-ip", interface_ip, "--timeout", str(timeout_s)]
        try:
            # The discovery worker must not inherit the MCP protocol's stdin handle.
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8-sig", errors="replace",
                                       timeout=timeout_s + 15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                       env={**os.environ, "PYTHONIOENCODING": "utf-8"}, check=False)
        except subprocess.TimeoutExpired as error:
            detail = error.stderr or ""
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            raise UserInputError("Network discovery timed out; no TCP connection or device configuration was requested. " + detail[-1800:]) from error
        except OSError as error:
            raise UserInputError(f"Cannot start network discovery: {error}") from error
        try:
            result = json.loads(completed.stdout)
        except ValueError as error:
            raise UserInputError("Discovery returned no valid result. " + completed.stderr[-800:]) from error
        if completed.returncode != 0 or result.get("status") != "passed":
            raise UserInputError("Network discovery failed: " + str(result.get("error", "unknown error")))
        if (result.get("backend"), result.get("version"), result.get("device_configuration_changed"), result.get("tcp_connections_opened")) != ("pnio_dcp", "1.2.0", False, False) or not isinstance(result.get("items"), list):
            raise UserInputError("Discovery backend contract mismatch.")
        return result

    def interfaces(self) -> dict[str, Any]:
        result = self._worker("interfaces")
        return {"interfaces": result["items"], "device_connected": False, "device_configuration_changed": False}

    def scan(self, interface_ip: str, timeout_s: int = 5) -> dict[str, Any]:
        interface_ip = ipv4(interface_ip)
        if type(timeout_s) is not int or not 2 <= timeout_s <= 15:
            raise UserInputError("timeout_s must be an integer between 2 and 15 seconds.")
        if not _NETWORK_LOCK.acquire(blocking=False):
            raise UserInputError("Another network discovery is running.")
        try:
            result = self._worker("scan", interface_ip, timeout_s)
            unique = {}
            for row in result["items"]:
                if not isinstance(row, dict) or not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", str(row.get("mac", ""))):
                    raise UserInputError("Discovery returned an invalid device identity.")
                clean = {key: str(row.get(key, ""))[:255] for key in ("name", "mac", "ip", "netmask", "gateway", "family")}
                clean["ip"] = ipv4(clean["ip"], allow_unconfigured=True)
                clean["mac"] = clean["mac"].upper()
                clean["tcp_connectable"] = clean["ip"] != "0.0.0.0"
                unique[(clean["mac"], clean["ip"])] = clean
            devices = [dict(row, device_id=f"D{index:02d}") for index, row in enumerate(sorted(unique.values(), key=lambda x: (ipaddress.IPv4Address(x["ip"]), x["mac"])), 1)]
            scan_id = uuid.uuid4().hex
            snapshot = {"scan_id": scan_id, "scanned_at": datetime.now(timezone.utc).isoformat(), "interface_ip": interface_ip,
                        "backend": result["backend"], "backend_version": result["version"], "devices": devices,
                        "selection_required": True, "device_connected": False, "tcp_connections_opened": False,
                        "device_configuration_changed": False}
            folder = self.root / "discoveries"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / f"{scan_id}.json").open("x", encoding="utf-8") as target:
                json.dump(snapshot, target, ensure_ascii=False, indent=2)
            return snapshot
        finally:
            _NETWORK_LOCK.release()

    def _device(self, scan_id: str, device_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(scan_id, str) or not re.fullmatch(r"[0-9a-f]{32}", scan_id):
            raise UserInputError("Choose a scan_id returned by scan_network_devices.")
        if not isinstance(device_id, str) or not re.fullmatch(r"D[0-9]{2,4}", device_id):
            raise UserInputError("Choose a device_id from the scan result.")
        snapshot = read_json(self.root / "discoveries" / f"{scan_id}.json")
        device = next((row for row in snapshot.get("devices", []) if row.get("device_id") == device_id), None)
        if device is None:
            raise UserInputError("That device is not present in the chosen scan result.")
        return snapshot, device

    def select(self, scan_id: str, device_id: str) -> dict[str, Any]:
        snapshot, device = self._device(scan_id, device_id)
        if not device.get("tcp_connectable"):
            raise UserInputError("This device has no assigned IP. MCP will not assign or change its IP.")
        selection = {"scan_id": scan_id, "device_id": device_id, "selected_at": datetime.now(timezone.utc).isoformat(),
                     "interface_ip": snapshot["interface_ip"], "device": device, "tcp_port": 1000,
                     "device_connected": False, "selection_only": True, "device_configuration_changed": False}
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f"selection-{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.root / "selection.json")
        finally:
            temporary.unlink(missing_ok=True)
        return selection

    def selection(self) -> dict[str, Any]:
        path = self.root / "selection.json"
        if not path.is_file():
            return {"selected": False, "device_connected": False}
        selection = read_json(path)
        snapshot, device = self._device(selection.get("scan_id"), selection.get("device_id"))
        if selection.get("device") != device or selection.get("interface_ip") != snapshot.get("interface_ip"):
            raise UserInputError("Saved selection differs from its discovery snapshot. Select a device again.")
        return {**selection, "selected": True, "device_connected": False}

    def probe_selected(self) -> dict[str, Any]:
        selected = self.selection()
        if not selected.get("selected"):
            raise UserInputError("Scan devices and ask the user to choose one before connecting.")
        device = selected["device"]
        report = self.probe.run_tcp(device["ip"], device["mac"], selected["interface_ip"])
        return {**report, "selection": selected, "device_identity_matches_discovery": True}
