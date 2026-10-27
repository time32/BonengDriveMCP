"""Operate an explicitly authorized run via MCP; no automatic restart or retry."""
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
    parameters = StdioServerParameters(command=sys.executable, args=["-u", str(PROJECT / "run_server.py")], cwd=str(PROJECT),
                                      env={**os.environ, "BONENG_RELEASE_DIR": str(args.release_dir), "PYTHONIOENCODING": "utf-8"})
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if args.action == "stop":
                show(payload(await session.call_tool("stop_drive_pc_control", {})))
                return
            if args.action == "status":
                result = payload(await session.call_tool("get_drive_run_status", {}))
                show(result)
                return
            selected = payload(await session.call_tool("get_network_selection", {}))
            if not selected.get("selected") or selected["device"]["mac"] != args.expected_mac or selected["device"]["ip"] != args.expected_ip:
                raise RuntimeError("Selection differs from the explicitly requested device.")
            plan = payload(await session.call_tool("plan_drive_run", {
                "speed": args.speed, "maximum_abs_speed": args.maximum_abs_speed, "speed_unit": args.unit,
                "duration_s": None if args.continuous else args.duration_s,
                "motor_load_description": args.load, "local_stop_available": args.local_stop_available,
                "restore_control_mode": args.restore_control_mode}))
            show({"phase": "plan_ready", "plan_id": plan["plan_id"], "target": plan["target"],
                  "speed": plan["speed"], "speed_unit": plan["speed_unit"], "duration_s": plan["duration_s"]})
            if not args.execute:
                return
            result = payload(await session.call_tool("execute_drive_run", {"plan_id": plan["plan_id"]}))
            if not args.continuous:
                show(result)
                return
            show({"phase": "worker_started", "job_id": result["job_id"], "pid": result.get("pid")})
            deadline = time.monotonic() + 30
            while True:
                status = payload(await session.call_tool("get_drive_run_status", {"job_id": result["job_id"]}))
                show({"phase": "status", "job_id": result["job_id"], "status": status["status"],
                      "latest_progress": status["latest_progress"], "warning": status.get("warning")})
                progress = status.get("latest_progress") or {}
                terminal = status["status"] in {"failed", "stopped", "result_unknown"}
                observed = progress.get("elapsed_ms", 0) >= 10000
                if terminal or observed or time.monotonic() > deadline:
                    output = PROJECT / "data" / ("drive_run_" + result["job_id"] + ".json")
                    output.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
                    show({"report": str(output), "status": status["status"], "worker_continues": status["worker_alive"]})
                    return
                await asyncio.sleep(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--action", choices=["start", "status", "stop"], default="status")
    parser.add_argument("--expected-ip")
    parser.add_argument("--expected-mac")
    parser.add_argument("--speed", type=float)
    parser.add_argument("--maximum-abs-speed", type=float)
    parser.add_argument("--unit", choices=["Hz", "rpm"])
    parser.add_argument("--duration-s", type=float)
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--load")
    parser.add_argument("--local-stop-available", action="store_true")
    parser.add_argument("--restore-control-mode", type=int, choices=[0, 1, 2])
    parser.add_argument("--execute", action="store_true", help="Only when the user explicitly authorized this motion.")
    args = parser.parse_args()
    if args.action == "start" and (None in [args.expected_ip, args.expected_mac, args.speed, args.maximum_abs_speed, args.unit, args.load] or (not args.continuous and args.duration_s is None)):
        parser.error("Start requires target, speed/max/unit, load and duration or --continuous.")
    asyncio.run(run(args))
