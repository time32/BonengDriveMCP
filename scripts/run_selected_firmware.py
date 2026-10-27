"""Run one explicitly requested TCP upgrade through the MCP tools; never retry it."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT = Path(__file__).resolve().parents[1]


def payload(result):
    if result.isError:
        raise RuntimeError(str(result.content))
    return result.structuredContent


def show(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


async def run(args):
    parameters = StdioServerParameters(
        command=sys.executable, args=["-u", str(PROJECT / "run_server.py")], cwd=str(PROJECT),
        env={**os.environ, "BONENG_RELEASE_DIR": str(args.release_dir),
             "BONENG_WORKSPACE": str(PROJECT / "workspace"), "PYTHONIOENCODING": "utf-8"})
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            plan = payload(await session.call_tool("preflight_firmware_upgrade", {
                "firmware_path": str(args.firmware), "transport": "selected_tcp"}))
            if plan["target_mac"] != args.expected_mac.upper() or plan["host"] != args.expected_ip:
                raise RuntimeError("Selected target differs from the explicitly requested device; no flash started.")
            show({"phase": "preflight_passed", "identity": plan["identity"], "host": plan["host"],
                  "firmware_sha256": plan["firmware_sha256"]})
            if not args.execute:
                return
            job = payload(await session.call_tool("start_firmware_upgrade", {"plan_id": plan["plan_id"]}))
            show({"phase": "job_started", "job_id": job["job_id"], "host": job["host"], "mac": job["target_mac"]})
            deadline = time.monotonic() + 1200
            while True:
                status = payload(await session.call_tool("get_firmware_upgrade_status", {"job_id": job["job_id"]}))
                show({"phase": "status", "job_id": job["job_id"], "status": status["status"],
                      "transfer_percent": status["transfer_percent"], "latest_event": status["latest_event"]})
                if status["status"] != "running":
                    report = PROJECT / "data" / ("tcp_upgrade_" + job["job_id"] + ".json")
                    report.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    show({"report": str(report), "status": status["status"],
                          "requires_manual_power_cycle": status["requires_manual_power_cycle"]})
                    if status["status"] not in {"flash_verified", "boot_verified"}:
                        raise RuntimeError("Upgrade did not return verified success. Inspect this job; no automatic retry.")
                    return
                if time.monotonic() > deadline:
                    raise RuntimeError("Stopped polling after 20 minutes; the job was not interrupted. Query its status; do not retry.")
                await asyncio.sleep(10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--expected-ip", required=True)
    parser.add_argument("--expected-mac", required=True)
    parser.add_argument("--execute", action="store_true", help="Only use after the user requests this target and image.")
    asyncio.run(run(parser.parse_args()))
