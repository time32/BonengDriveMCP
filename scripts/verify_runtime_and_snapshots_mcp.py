"""Verify live parameter backups over MCP; never starts or controls a motor."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import timedelta, datetime, timezone
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
            selection = payload(await session.call_tool("get_network_selection", {}))
            assert selection["selected"] and selection["device"]["ip"] == args.expected_ip and selection["device"]["mac"] == args.expected_mac.upper()
            before = payload(await session.call_tool("read_device_parameters", {"codes": ["A00.00", "A00.06", "B05.01", "P12.07"]}))
            backups = []
            for label in ("MCP configuration baseline", "MCP configuration repeat"):
                backups.append(payload(await session.call_tool("create_device_parameter_snapshot", {"label": label}, read_timeout_seconds=timedelta(seconds=90))))
            diff = payload(await session.call_tool("compare_device_parameter_snapshots", {"baseline_id": backups[0]["snapshot_id"], "target_id": backups[1]["snapshot_id"]}))
            assert diff["same_device"] and diff["same_scope"] and diff["definition_versions_match"]
            assert diff["counts"]["definition_changed"] == 0 and diff["counts"]["added"] == 0 and diff["counts"]["removed"] == 0
            listing = payload(await session.call_tool("list_device_parameter_snapshots", {}))
            detail = payload(await session.call_tool("get_device_parameter_snapshot", {"snapshot_id": backups[0]["snapshot_id"], "group": "B05"}))
            assert detail["total"] > 0 and listing["total"] >= 2
            report = {"status": "passed", "verified_at": datetime.now(timezone.utc).isoformat(), "before": before,
                "snapshots": backups, "comparison": diff, "detail": detail, "motor_started": False, "parameter_write_executed": False,
                "running_scope_hardware_test_performed": False}
            if args.capture_codes:
                assert not next(p for p in before["result"]["parameters"] if p["code"] == "P12.07")["raw_value"] & 4
                report["stopped_scope"] = payload(await session.call_tool("capture_oscilloscope", {
                    "codes": args.capture_codes, "sample_period_ms": 2, "points": 1000}))
            after = payload(await session.call_tool("read_device_parameters", {"codes": ["A00.00", "A00.06", "B05.01", "P12.07"]}))
            report["after"] = after
            path = PROJECT / "data/runtime_and_snapshots_mcp_verification.json"
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            values = {p["code"]: p["value"] for p in after["result"]["parameters"]}
            print(json.dumps({"status": "passed", "snapshots": backups, "difference_counts": diff["counts"],
                "after": values, "report": str(path), "motor_started": False, "parameter_write_executed": False}, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--expected-ip", required=True)
    parser.add_argument("--expected-mac", required=True)
    parser.add_argument("--capture-codes", nargs="+")
    asyncio.run(verify(parser.parse_args()))
