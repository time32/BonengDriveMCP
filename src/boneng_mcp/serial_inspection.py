"""Read-only serial bootstrap for CM55, without assuming AN MAC/register layout."""
from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .device import DeviceOperations
from .original_probe import EXPECTED_RELEASE_SHA256, _PROBE_LOCK
from .paths import UserInputError
from .validation_guard import check_validation_access

PROJECT = Path(__file__).resolve().parents[2]


class SerialInspection:
    def __init__(self, release: Path, workspace: Path, firmware):
        self.vendor = release / "BonengDrivesoft.exe"
        self.bridge = PROJECT / "workspace/reuse_bridge/ReuseDeviceBridge.exe"
        self.workspace, self.firmware = workspace, firmware

    def _run(self, port: str, operation: str, **fields) -> dict[str, Any]:
        check_validation_access(self.workspace)
        active = self.firmware._active()
        if active and self.firmware.status(active["job_id"])["status"] not in {"failed", "flash_verified", "boot_verified"}:
            raise UserInputError("An upgrade is active or uncertain; inspect it before contacting a serial device.")
        if not self.vendor.is_file() or not self.bridge.is_file():
            raise UserInputError("Build the vendor bridge and configure BONENG_RELEASE_DIR first.")
        if not _PROBE_LOCK.acquire(blocking=False): raise UserInputError("Another device operation is running.")
        try:
            folder = self.workspace / "serial_inspection" / uuid.uuid4().hex
            folder.mkdir(parents=True)
            request = {"operation": "inspect_" + operation, "transport": "serial", "port": port, **fields}
            path = folder / "request.json"
            path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            try:
                completed = subprocess.run([str(self.bridge), str(self.vendor), str(path)], stdin=subprocess.DEVNULL,
                    cwd=self.bridge.parent, capture_output=True, encoding="utf-8-sig", errors="replace", timeout=65,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise UserInputError("Serial read-only inspection failed: " + str(error)) from error
            (folder / "stdout.json").write_text(completed.stdout, encoding="utf-8")
            try: result = json.loads(completed.stdout)
            except ValueError as error: raise UserInputError("No valid serial inspection result: " + completed.stderr[-800:]) from error
            if completed.returncode or result.get("status") != "passed": raise UserInputError("Serial inspection failed: " + str(result.get("error")))
            identity = result.get("identity", {})
            if result.get("operation") != request["operation"] or result.get("port") != port or result.get("original_sha256") != EXPECTED_RELEASE_SHA256 or result.get("device_write_commands_sent") is not False or identity.get("platform_type") not in {3, 9} or identity.get("communication_type") != 0:
                raise UserInputError("Serial inspection read-only/identity contract mismatch.")
            if fields.get("expected_identity") and identity != fields["expected_identity"]: raise UserInputError("Serial handshake changed between operations.")
            result.pop("frames", None)
            return {**result, "hardware_contacted": True, "physical_unique_identity_verified": False, "journal_dir": str(folder),
                    "origin": "live_serial_device_via_original_drivesoft", "write_control_capture_upgrade_supported_by_this_tool": False}
        finally: _PROBE_LOCK.release()

    def inspect(self, mode: str = "identity", port: str = "COM7", codes: list[str] | None = None,
                group: str = "", query: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if not isinstance(port, str) or not re.fullmatch(r"COM[1-9][0-9]{0,3}", port) or mode not in {"identity", "catalog", "read"}:
            raise UserInputError("Use identity/catalog/read and a Windows serial port such as COM7.")
        DeviceOperations._validate_search(query, group, limit, offset)
        if mode == "identity":
            if codes or group or query: raise UserInputError("Identity inspection accepts only the port.")
            return self._run(port, "identity")
        if mode == "read":
            DeviceOperations._validate_codes(codes)
            groups = {code[:3] for code in codes}
            if group or query: raise UserInputError("Read accepts codes; use catalog for filtering.")
            group = next(iter(groups)) if len(groups) == 1 else ""
        elif codes: raise UserInputError("Use read mode to read codes.")
        catalog = self._run(port, "catalog", group_filter=group)
        if catalog["result"].get("definitions_complete_for_scope") is not True: raise UserInputError("Incomplete device directory; no cached fallback.")
        folder = Path(catalog["journal_dir"])
        full = folder / "catalog.json"
        full.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
        definitions = catalog["result"]["parameters"]
        if mode == "catalog":
            rows = [p for p in definitions if not p["high_word_of"] and (not query or query.casefold() in (p["code"] + " " + p["name"]).casefold())]
            return {**{k: v for k, v in catalog.items() if k != "result"}, "catalog_file": str(full), "groups": catalog["result"]["groups"],
                    "parameter_words": len(definitions), "total": len(rows), "items": rows[offset:offset+limit], "limit": limit, "offset": offset,
                    "next_offset": offset+limit if offset+limit < len(rows) else None, "cached_fallback_used": False}
        by_code = {p["code"]: p for p in definitions}
        parameters = []
        for code in codes:
            p = by_code.get(code)
            if not p or p["high_word_of"] or p["access"] == 3: raise UserInputError("Choose a readable first-word code advertised by this device: " + code)
            parameters.append({"code": code, "group": p["group"], "offset": p["offset"], "expected_attribute": p["attribute_raw"], "expected_name": p["name"]})
        read = self._run(port, "read", expected_identity=catalog["identity"], parameters=parameters)
        if [p.get("code") for p in read["result"]["parameters"]] != codes: raise UserInputError("Serial parameter response does not match requested codes.")
        return {**read, "catalog_file": str(full)}
