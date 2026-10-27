"""Read actual device configurations and compare immutable local snapshots."""
from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from .device import DeviceOperations, identifier
from .paths import UserInputError, read_json


class ParameterSnapshots:
    def __init__(self, workspace: Path, device: DeviceOperations):
        self.root, self.device = workspace / "device" / "snapshots", device

    @staticmethod
    def summary(snapshot: dict[str, Any]) -> dict[str, Any]:
        return {key: snapshot[key] for key in ("snapshot_id", "label", "identity", "created_at", "started_at", "completed_at",
                "catalog_id", "scope", "parameter_count", "excluded_count", "complete", "file", "simultaneous_snapshot")}

    def create(self, label: str, groups: list[str] | None = None, include_monitoring: bool = False,
               transport: str = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        if not isinstance(label, str) or not label.strip() or len(label) > 200 or type(include_monitoring) is not bool:
            raise UserInputError("Provide a 1–200 character label and a boolean include_monitoring.")
        if groups is not None and (not isinstance(groups, list) or not groups or len(groups) > 128 or not all(isinstance(g, str) for g in groups) or len(set(groups)) != len(groups)):
            raise UserInputError("groups must be distinct protocol group names, or null for all groups.")
        for group in groups or []:
            self.device._validate_search("", group, 1, 0)
        started = time.time()
        catalog = self.device._catalog(transport, port, groups)
        advertised = {g["code"] for g in catalog["groups"]}
        if groups and not set(groups).issubset(advertised):
            raise UserInputError("Requested snapshot group was not advertised by the device.")
        candidates = [p for p in catalog["parameters"] if not groups or p["code"][:3] in groups]
        selected = [p for p in candidates if not p["high_word_of"] and not p["reserved"] and p["access"] != 3
                    and (include_monitoring or (p["access"] in {0, 1} and not p["code"].startswith("P")))]
        if not selected:
            raise UserInputError("No readable parameters match the snapshot scope.")
        report = self.device._run({**catalog["target"], "operation": "snapshot", "expected_identity": catalog["identity"],
            "parameters": [{"code": p["code"], "group": p["group"], "offset": p["offset"], "expected_attribute": p["attribute_raw"]} for p in selected]})
        if report.get("identity") != catalog["identity"] or report.get("device_write_commands_sent") is not False:
            raise UserInputError("Snapshot device/read-only contract mismatch.")
        received = report["result"]["parameters"]
        if [p["code"] for p in received] != [p["code"] for p in selected]:
            raise UserInputError("Snapshot is incomplete or parameter order differs.")
        parameters = []
        for definition, value in zip(selected, received):
            if value["attribute_raw"] != definition["attribute_raw"]:
                raise UserInputError("Snapshot definition changed; refresh the device catalog.")
            width, raw, point = value["width_bits"], value["raw_value"], value["decimal_attribute_raw"]
            if width not in {16, 32} or type(raw) is not int or not 0 <= raw < 1 << width or type(point) is not int or not 0 <= point <= 4:
                raise UserInputError("Invalid snapshot value representation.")
            decoded = raw - (1 << width) if value["signed"] and raw & (1 << (width - 1)) else raw
            parameters.append({**definition, **value, "value_exact": format(Decimal(decoded).scaleb(-point), "f")})
        snapshot_id = uuid.uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (snapshot_id + ".json")
        completed = time.time()
        snapshot = {"schema_version": 1, "snapshot_id": snapshot_id, "label": label.strip(), "created_at": completed,
            "started_at": started, "completed_at": completed, "identity": catalog["identity"], "target": catalog["target"],
            "catalog_id": catalog["catalog_id"], "scope": {"groups": groups, "include_monitoring": include_monitoring, "default_is_readable_configuration": not include_monitoring},
            "parameter_count": len(parameters), "excluded_count": len(candidates) - len(selected), "parameters": parameters,
            "complete": True, "simultaneous_snapshot": False, "origin": "live_device_C2_with_B1_verified_online_catalog",
            "device_write_commands_sent": False, "background_control_writes_continued": report.get("background_control_writes_continued", False),
            "journal_dir": report["journal_dir"], "file": str(path)}
        temporary = self.root / (snapshot_id + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(snapshot, output, ensure_ascii=False, indent=2)
            temporary.rename(path)
        finally:
            temporary.unlink(missing_ok=True)
        return self.summary(snapshot)

    def list(self, limit: int = 50) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise UserInputError("limit must be 1–100.")
        paths = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return {"total": len(paths), "snapshots": [self.summary(read_json(p)) for p in paths[:limit]], "hardware_contacted": False}

    def get(self, snapshot_id: str, group: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self.device._validate_search("", group, limit, offset)
        snapshot = read_json(self.root / (identifier(snapshot_id) + ".json"))
        rows = [p for p in snapshot["parameters"] if not group or p["code"].startswith(group + ".")]
        return {**self.summary(snapshot), "total": len(rows), "items": rows[offset:offset + limit], "offset": offset, "limit": limit, "hardware_contacted": False}

    def compare(self, baseline_id: str, target_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self.device._validate_search("", "", limit, offset)
        baseline, target = [read_json(self.root / (identifier(i) + ".json")) for i in (baseline_id, target_id)]
        before, after = [{p["code"]: p for p in s["parameters"]} for s in (baseline, target)]
        changes = []; counts = {"added": 0, "removed": 0, "definition_changed": 0, "value_changed": 0, "unchanged": 0}
        fields = ("group", "offset", "name", "attribute_raw", "width_bits", "signed", "unit_index", "decimal_attribute_raw")
        for code in sorted(before.keys() | after.keys()):
            old, new = before.get(code), after.get(code)
            if old is None: kind = "added"
            elif new is None: kind = "removed"
            elif any(old.get(k) != new.get(k) for k in fields): kind = "definition_changed"
            elif old["raw_value"] != new["raw_value"]: kind = "value_changed"
            else: kind = "unchanged"
            counts[kind] += 1
            if kind != "unchanged": changes.append({"code": code, "kind": kind, "before": old, "after": new})
        identity_fields = ("platform_type", "parameter_version_raw", "software_raw", "release_hex")
        compatible = all(baseline["identity"].get(k) == target["identity"].get(k) for k in identity_fields)
        serial_binding = baseline["identity"].get("serial_binding_id") or target["identity"].get("serial_binding_id")
        same_device = baseline["identity"]["mac"] == target["identity"]["mac"]
        if serial_binding:
            same_device = baseline["identity"].get("serial_binding_id") == target["identity"].get("serial_binding_id")
        return {"baseline": self.summary(baseline), "target": self.summary(target), "counts": counts, "total_changes": len(changes),
                "changes": changes[offset:offset + limit], "offset": offset, "limit": limit,
                "same_device": same_device, "physical_unique_identity_verified": not bool(serial_binding), "same_scope": baseline["scope"] == target["scope"],
                "definition_versions_match": compatible, "restore_supported": False, "hardware_contacted": False,
                "missing_parameter_action": "reported only; never reset or write omitted parameters"}
