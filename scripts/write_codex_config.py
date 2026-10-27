"""Generate a reviewable Codex MCP configuration; never modify user configuration."""

import json
import os
from pathlib import Path

project = Path(__file__).resolve().parents[1]
configuration = (
    "# Copy this table into Codex MCP settings, or a trusted project's .codex/config.toml.\n"
    "[mcp_servers.boneng_drivesoft]\n"
    f"command = {json.dumps(str(project / '.venv' / 'Scripts' / 'python.exe'), ensure_ascii=False)}\n"
    f"args = [{json.dumps('-u')}, {json.dumps(str(project / 'run_server.py'), ensure_ascii=False)}]\n"
    f"cwd = {json.dumps(str(project), ensure_ascii=False)}\n"
    "enabled = true\nstartup_timeout_sec = 30\ntool_timeout_sec = 180\n"
)
configuration += "\n[mcp_servers.boneng_drivesoft.env]\n"
configuration += "BONENG_WORKSPACE = " + json.dumps(str(project / "workspace"), ensure_ascii=False) + "\n"
configuration += "BONENG_DATA_DIR = " + json.dumps(str(project / "data"), ensure_ascii=False) + "\n"
if os.environ.get("BONENG_RELEASE_DIR"):
    configuration += "BONENG_RELEASE_DIR = " + json.dumps(str(Path(os.environ["BONENG_RELEASE_DIR"]).resolve()), ensure_ascii=False) + "\n"
for name in ("BONENG_SERIAL_MAC", "BONENG_PWSH_PATH"):
    if os.environ.get(name):
        configuration += name + " = " + json.dumps(os.environ[name], ensure_ascii=False) + "\n"
destination = project / "codex.config.toml"
destination.write_text(configuration, encoding="utf-8")
print(f"Generated {destination.name}; no existing Codex settings were changed.")
