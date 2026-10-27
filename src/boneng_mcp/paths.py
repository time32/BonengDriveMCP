"""Keep generated engineering artifacts separate from the original release."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class UserInputError(ValueError):
    """An actionable, expected tool input error."""


@dataclass(frozen=True)
class Settings:
    release_dir: Path
    data_dir: Path
    workspace_dir: Path

    @classmethod
    def from_environment(cls) -> "Settings":
        project = Path(__file__).resolve().parents[2]
        return cls(
            Path(os.environ.get("BONENG_RELEASE_DIR", project.parent)).resolve(),
            Path(os.environ.get("BONENG_DATA_DIR", project / "data")).resolve(),
            Path(os.environ.get("BONENG_WORKSPACE", project / "workspace")).resolve(),
        )


def safe_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise UserInputError("ID must contain 1–64 ASCII letters, digits, underscores or hyphens.")
    return value


def confined_file(root: Path, relative: str, suffix: str) -> Path:
    if not relative or "\x00" in relative or ":" in relative:
        raise UserInputError("Supply a relative file name inside the MCP workspace.")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path.suffix.lower() != suffix:
        raise UserInputError(f"File must stay inside the MCP workspace and end in {suffix}.")
    return path


def read_json(path: Path, max_bytes: int = 8_000_000) -> Any:
    if not path.is_file():
        raise UserInputError(f"File not found: {path.name}")
    if path.stat().st_size > max_bytes:
        raise UserInputError("File exceeds the size limit.")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=_bad_constant, object_pairs_hook=_unique_keys)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise UserInputError("Invalid UTF-8 JSON file.") from error


def _bad_constant(value: str) -> None:
    raise UserInputError(f"Non-finite JSON number is not allowed: {value}")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise UserInputError(f"Duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result
