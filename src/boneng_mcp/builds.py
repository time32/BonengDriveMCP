"""Immutable registrations for user-compiled AN PN SPI application images."""
from __future__ import annotations

import hashlib
import json
import re
import struct
import time
import uuid
from pathlib import Path
from typing import Any

from .paths import UserInputError, read_json

PROFILE = "an_pn_spi_910"
CM55_BUILD_PROFILE = "cm55_ec_spi_612"
PROFILES = {PROFILE: (9, 910), CM55_BUILD_PROFILE: (3, 612)}
IMAGE_BYTES = 1966100


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def image_bytes(path: Path, profile: str = PROFILE) -> bytes:
    if not path.is_absolute() or path.suffix.lower() != ".bin" or not path.is_file():
        raise UserInputError("Supply an absolute path to an existing SPI .bin image.")
    if profile not in PROFILES:
        raise UserInputError("Unsupported firmware profile.")
    size = path.stat().st_size
    if (profile == PROFILE and size != IMAGE_BYTES) or (profile == CM55_BUILD_PROFILE and not 20 <= size <= 0x200000):
        raise UserInputError("SPI image size differs from the selected application profile.")
    data = path.read_bytes()
    if len(data) != size or struct.unpack("<4I", data[:16]) != (0xEE3355BB, size - 4, 0x80000000, 0x80000000):
        raise UserInputError("SPI application header does not match the selected product profile.")
    return data


def build_identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise UserInputError("Use the build_id returned by register_firmware_build.")
    return value


class FirmwareBuilds:
    def __init__(self, workspace: Path):
        self.root = workspace / "firmware" / "builds"

    def register(self, firmware_path: str, label: str, expected_release_hex: str,
                 expected_software_raw: int = 910, expected_parameter_version_raw: int | None = None,
                 source_revision: str = "", profile: str = PROFILE) -> dict[str, Any]:
        if not isinstance(label, str) or not label.strip() or len(label) > 200 or not isinstance(source_revision, str) or len(source_revision) > 200:
            raise UserInputError("Provide a 1–200 character build label and an optional source revision up to 200 characters.")
        if not isinstance(expected_release_hex, str) or not re.fullmatch(r"[0-9A-Fa-f]{4}", expected_release_hex):
            raise UserInputError("Expected firmware release must be four hexadecimal digits, such as 6909.")
        if profile not in PROFILES or type(expected_software_raw) is not int or expected_software_raw != PROFILES[profile][1]:
            raise UserInputError("Software must match the selected AN 9.10 or CM55 EC 6.12 build profile.")
        if expected_parameter_version_raw is not None and (type(expected_parameter_version_raw) is not int or not 0 <= expected_parameter_version_raw <= 65535):
            raise UserInputError("Parameter revision must be a 16-bit integer or null.")
        original = Path(firmware_path)
        data = image_bytes(original, profile)
        digest = hashlib.sha256(data).hexdigest()
        build_id = uuid.uuid4().hex
        folder = self.root / build_id
        folder.mkdir(parents=True)
        sealed = folder / "firmware.bin"
        with sealed.open("xb") as output:
            output.write(data)
        manifest = {"schema_version": 1, "build_id": build_id, "label": label.strip(), "profile": profile,
            "original_path": str(original.resolve()), "firmware_path": str(sealed.resolve()), "firmware_sha256": digest,
            "bytes": len(data), "expected_software_raw": expected_software_raw, "expected_release_hex": expected_release_hex.upper(),
            "expected_parameter_version_raw": expected_parameter_version_raw, "platform_type": PROFILES[profile][0],
            "source_revision": source_revision, "source_revision_is_user_declared": True, "created_at": time.time(),
            "compatibility_basis": "user-declared product profile plus inspected SPI container; header does not prove application semantics or model compatibility",
            "hardware_contacted": False, "upgrade_started": False}
        with (folder / "build.json").open("x", encoding="utf-8") as output:
            json.dump(manifest, output, ensure_ascii=False, indent=2)
        return manifest

    def get(self, build_id: str) -> dict[str, Any]:
        folder = self.root / build_identifier(build_id)
        build = read_json(folder / "build.json")
        path = folder / "firmware.bin"
        profile = build.get("profile")
        if build.get("build_id") != build_id or profile not in PROFILES or build.get("firmware_path") != str(path.resolve()) or (build.get("platform_type"), build.get("expected_software_raw")) != PROFILES[profile]:
            raise UserInputError("Registered build manifest does not match its immutable artifact.")
        image_bytes(path, profile)
        if sha256(path) != build["firmware_sha256"]:
            raise UserInputError("Registered firmware artifact changed; register the new compiled output.")
        return build
