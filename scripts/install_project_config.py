"""Register this tested local server in a new project config, without overwriting settings."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create the project's .codex/config.toml if absent.")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    target = project / ".codex" / "config.toml"
    if not target.resolve().is_relative_to(project):
        raise SystemExit("Project config path resolves outside this MCP project.")
    source = (project / "codex.config.toml").read_text(encoding="utf-8")
    expected = tomllib.loads(source)
    entry = expected["mcp_servers"]["boneng_drivesoft"]
    if set(expected) != {"mcp_servers"} or set(expected["mcp_servers"]) != {"boneng_drivesoft"}:
        raise SystemExit("Refusing configuration that contains unrelated settings.")
    if entry["command"] != str(project / ".venv" / "Scripts" / "python.exe") or entry["args"] != ["-u", str(project / "run_server.py")]:
        raise SystemExit("Launch command does not match the tested source-tree server.")
    if target.exists():
        existing = tomllib.loads(target.read_text(encoding="utf-8-sig"))
        if existing.get("mcp_servers", {}).get("boneng_drivesoft") == entry:
            print(json.dumps({"status": "already_registered", "scope": "project"}))
            return
        raise SystemExit("Project config already exists. Preserve it and merge codex.config.toml manually.")
    if not args.apply:
        print(json.dumps({"status": "ready_to_register", "scope": "project", "target": str(target)}))
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(source)
    print(json.dumps({"status": "registered", "scope": "project", "reload_required": True}))


if __name__ == "__main__":
    main()
