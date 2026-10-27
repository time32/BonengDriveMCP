"""Offline parameter files and batch previews. This module has no device transport."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .catalog import Catalog, bounded_page
from .paths import UserInputError, confined_file, read_json, safe_id


def canonical_value(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise UserInputError("Parameter values must be strings or finite numbers; booleans are not allowed.")
    text = str(value).strip()
    if not text or len(text) > 1024:
        raise UserInputError("Parameter values must contain 1–1024 characters.")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return text
    if not number.is_finite():
        raise UserInputError("Non-finite parameter values are not allowed.")
    # Do not reinterpret radix, enum strings, units or register encodings.
    return text


def equal_values(left: str, right: str) -> bool:
    try:
        return Decimal(left) == Decimal(right)
    except InvalidOperation:
        return left == right


class ParameterSets:
    def __init__(self, workspace: Path, catalog: Catalog):
        self.root = workspace / "parameter_sets"
        self.catalog = catalog

    def _path(self, set_id: str) -> Path:
        return confined_file(self.root, safe_id(set_id) + ".json", ".json")

    def load(self, set_id: str) -> dict[str, Any]:
        data = read_json(self._path(set_id))
        if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("kind") != "offline_parameter_set":
            raise UserInputError("Unsupported parameter file format. This is not an original Drivesoft export parser.")
        if data.get("id") != set_id or not isinstance(data.get("catalog_id"), str):
            raise UserInputError("Parameter file identity is invalid.")
        if data.get("device_verified") is not False or data.get("origin") != "user_supplied_offline_values":
            raise UserInputError("Offline files cannot claim device verification or a live-reading origin.")
        values = data.get("values")
        if not isinstance(values, dict) or not values or len(values) > 10_000:
            raise UserInputError("Parameter file must contain 1–10000 values.")
        self.catalog.dataset(data["catalog_id"])
        for code, value in values.items():
            if not isinstance(code, str) or not code or len(code) > 128:
                raise UserInputError("Invalid parameter code.")
            canonical_value(value)
            self.catalog.definition(data["catalog_id"], code)
        return data

    def save(self, set_id: str, catalog_id: str, values: dict[str, Any], note: str = "") -> dict[str, Any]:
        destination = self._path(set_id)
        if not values or len(values) > 10_000:
            raise UserInputError("Supply 1–10000 parameter values.")
        if len(note) > 2000:
            raise UserInputError("note is limited to 2000 characters.")
        self.catalog.dataset(catalog_id)
        normalized = {}
        for code, value in values.items():
            if not isinstance(code, str) or not code or len(code) > 128:
                raise UserInputError("Invalid parameter code.")
            self.catalog.definition(catalog_id, code)
            normalized[code] = canonical_value(value)
        data = {
            "schema_version": 1, "kind": "offline_parameter_set", "id": set_id,
            "catalog_id": catalog_id, "created_at": datetime.now(timezone.utc).isoformat(),
            "origin": "user_supplied_offline_values", "device_verified": False,
            "note": note, "values": normalized,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
        except FileExistsError as error:
            raise UserInputError("This set ID already exists. Choose a new ID; existing files are preserved.") from error
        return {"id": set_id, "path": str(destination), "parameter_count": len(normalized), "device_verified": False}

    def list(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        result = []
        for path in sorted(self.root.glob("*.json")):
            if len(result) >= 500:
                break
            try:
                data = self.load(path.stem)
                result.append({"id": data["id"], "catalog_id": data["catalog_id"], "parameter_count": len(data["values"]), "device_verified": False})
            except UserInputError:
                result.append({"id": path.stem, "error": "invalid_parameter_set"})
        return result

    def compare(self, baseline_id: str, target_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        bounded_page(limit, offset)
        baseline, target = self.load(baseline_id), self.load(target_id)
        if baseline["catalog_id"] != target["catalog_id"]:
            raise UserInputError("Parameter sets from different catalogs cannot be compared as device configurations.")
        old, new = baseline["values"], target["values"]
        changes = []
        unchanged = 0
        for code in sorted(old.keys() | new.keys()):
            if code in old and code in new and equal_values(str(old[code]), str(new[code])):
                unchanged += 1
                continue
            changes.append({
                "code": code, "before": old.get(code), "after": new.get(code),
                "change": "added" if code not in old else "removed" if code not in new else "modified",
            })
        return {
            "catalog_id": baseline["catalog_id"], "baseline_id": baseline_id, "target_id": target_id,
            "unchanged_count": unchanged, "changes": changes[offset:offset + limit], "offline_only": True,
            "total_changes": len(changes), "offset": offset, "limit": limit,
            "next_offset": offset + limit if offset + limit < len(changes) else None,
        }

    def plan(self, baseline_id: str, target_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        diff = self.compare(baseline_id, target_id, limit, offset)
        operations = []
        for change in diff["changes"]:
            if change["change"] == "removed":
                continue  # Removing a key from a file never means resetting a device parameter.
            definition = self.catalog.definition(diff["catalog_id"], change["code"])
            fields = definition["fields"]
            modification = fields.get("修改级别（2bit）", "")
            operations.append({
                **change, "name": definition["name"], "reference_fields": fields,
                "catalog_read_only": "只读" in str(modification), "device_limits_checked": False,
            })
        return {
            **diff, "operations": operations, "execution_supported": False, "device_verified": False,
            "omitted_parameters_action": "leave_unchanged",
            "pending_protocol_checks": [
                "device_model_and_firmware_match", "register_address_and_encoding", "write_permissions_and_limits",
                "required_device_state_and_write_order", "RAM_or_nonvolatile_persistence", "readback_and_partial_failure_handling",
            ],
        }
