"""Exercise firmware MCP preflight/history without starting another flash."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT = Path(__file__).resolve().parents[1]


def payload(result):
    if result.isError:
        raise RuntimeError(str(result.content))
    return result.structuredContent


async def verify(args):
    parameters = StdioServerParameters(command=sys.executable, args=["-u", str(PROJECT / "run_server.py")],
                                      env={**os.environ, "BONENG_RELEASE_DIR": str(args.release_dir),
                                           "BONENG_WORKSPACE": str(PROJECT / "workspace"), "PYTHONIOENCODING": "utf-8"}, cwd=str(PROJECT))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {item.name: item for item in (await session.list_tools()).tools}
            assert len(tools) == 54 and tools["start_firmware_upgrade"].annotations.destructiveHint is True
            history = payload(await session.call_tool("get_firmware_upgrade_status", {}))
            assert history["hardware_contacted"] is False
            plans = {}
            for transport in args.transports:
                plan = payload(await session.call_tool("preflight_firmware_upgrade", {"firmware_path": str(args.firmware), "transport": transport}))
                assert plan["status"] == "preflight_passed" and plan["device_write_commands_sent"] is False
                plans[transport] = plan
            # No call to start_firmware_upgrade is made by this verification script.
            report = {"status": "passed", "verification_transport": "MCP stdio", "tool_count": len(tools),
                      "preflight_plans": plans, "historical_status": history, "new_flash_started": False,
                      "tcp_physical_flash_performed": False}
            (PROJECT / "data" / "firmware_mcp_verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"status": "passed", "tool_count": len(tools), "preflights": {
                name: {"mac": plan["target_mac"], "host": plan.get("host"), "chunk_bytes": plan["identity"]["chunk_bytes"]}
                for name, plan in plans.items()}, "new_flash_started": False}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--transports", nargs="+", choices=["serial", "selected_tcp"], default=["serial", "selected_tcp"])
    asyncio.run(asyncio.wait_for(verify(parser.parse_args()), timeout=120))
