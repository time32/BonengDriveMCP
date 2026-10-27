"""Verify live MCP reads; capture only when the operator supplies explicit channels."""
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
    params = StdioServerParameters(command=sys.executable, args=["-u", str(PROJECT / "run_server.py")], cwd=str(PROJECT),
                                  env={**os.environ, "BONENG_RELEASE_DIR": str(args.release_dir), "PYTHONIOENCODING": "utf-8"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool_count = len((await session.list_tools()).tools)
            assert tool_count == 54
            channels = payload(await session.call_tool("list_oscilloscope_channels", {"group": "P11", "limit": 100}))
            runtime = payload(await session.call_tool("read_drive_runtime_status", {}))
            assert runtime["device_write_commands_sent"] is False
            wide = payload(await session.call_tool("read_device_parameters", {"codes": ["P13.45"]}))
            assert wide["result"]["parameters"][0]["width_bits"] == 32 and wide["device_write_commands_sent"] is False
            report = {"status": "passed", "transport": "MCP stdio / selected TCP", "tool_count": tool_count,
                      "runtime": runtime, "wide_read": wide, "p11_channels": channels,
                      "motor_started": False, "parameter_write_executed": False}
            if args.capture_codes:
                report["capture"] = payload(await session.call_tool("capture_oscilloscope", {
                    "codes": args.capture_codes, "sample_period_ms": args.sample_period_ms, "points": args.points}))
            (PROJECT / "data" / "device_mcp_verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"status": "passed", "tool_count": tool_count, "mac": runtime["mac"], "release_hex": runtime["release_hex"],
                              "state_raw": runtime["state_raw"], "wide_read": wide["result"]["parameters"],
                              "capture": report.get("capture"), "motor_started": False, "parameter_write_executed": False}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--capture-codes", nargs="+")
    parser.add_argument("--sample-period-ms", type=int, default=2)
    parser.add_argument("--points", type=int, default=1000)
    asyncio.run(verify(parser.parse_args()))
