"""Explicit serial endpoint selection; a port/family fingerprint is not a UID."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .paths import UserInputError, read_json

CM55_PROFILE = "cm55_ec_612"
FINGERPRINT_KEYS = ("platform_type", "communication_type", "board_number_raw", "platform_raw")


class SerialTargets:
    def __init__(self, workspace: Path):
        self.path = workspace / "device" / "serial_selection.json"

    def get(self, port: str) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        target = read_json(self.path)
        if target.get("port") != port:
            return None
        if (target.get("device_profile") != CM55_PROFILE or target.get("transport") != "serial"
                or not re.fullmatch(r"[0-9a-f]{32}", target.get("serial_binding_id", ""))
                or target.get("expected_mac") is not None
                or target.get("expected_serial_identity", {}).get("platform_type") != 3
                or set(target.get("expected_serial_identity", {})) != set(FINGERPRINT_KEYS)):
            raise UserInputError("Invalid serial selection; select the connected serial device again.")
        return target

    def save(self, target: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(target, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
