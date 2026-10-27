"""Keep independent device actions out of an active firmware validation sequence."""
from __future__ import annotations

import os
from pathlib import Path

from .paths import UserInputError, read_json

TERMINAL = {"completed", "failed", "cancelled", "timed_out"}


def check_validation_access(workspace: Path) -> None:
    active = workspace / "validation" / "active.json"
    if not active.is_file():
        return
    owner = read_json(active)["job_id"]
    job = read_json(workspace / "validation" / "jobs" / owner / "job.json")
    if job["status"] not in TERMINAL and os.environ.get("BONENG_VALIDATION_JOB") != owner:
        raise UserInputError("A validation task owns this device workflow. Use get_firmware_validation_status or cancel_firmware_validation before independent device operations.")
