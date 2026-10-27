"""Start a user-requested persistent worker outside a temporary MCP job tree."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .paths import UserInputError

PROJECT = Path(__file__).resolve().parents[2]
PWSH = Path(os.environ.get("BONENG_PWSH_PATH") or shutil.which("pwsh") or r"C:\Program Files\PowerShell\7\pwsh.exe")


def start_detached(command: list[str], cwd: Path, folder: Path) -> dict:
    if not PWSH.is_file():
        raise UserInputError("PowerShell 7 is required for the persistent Windows worker.")
    request = folder / "launch.json"
    request.write_text(json.dumps({"command_line": subprocess.list2cmdline(command), "cwd": str(cwd.resolve())}), encoding="utf-8")
    completed = subprocess.run([str(PWSH), "-NoLogo", "-NoProfile", "-File", str(PROJECT / "scripts/Start-DetachedBridge.ps1"),
                                "-LaunchRequest", str(request.resolve())], stdin=subprocess.DEVNULL, capture_output=True,
                               encoding="utf-8-sig", errors="replace", timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    if completed.returncode:
        raise UserInputError("Persistent worker was not started: " + completed.stderr[-1600:])
    result = json.loads(completed.stdout)
    if type(result.get("pid")) is not int or result["pid"] <= 0:
        raise UserInputError("Windows returned no worker process ID.")
    return result
