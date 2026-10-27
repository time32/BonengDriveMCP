"""Read-only verification of device-defined catalogs through the actual MCP server."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import timedelta
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
            catalog = payload(await session.call_tool("discover_device_parameter_catalog", {}, read_timeout_seconds=timedelta(seconds=90)))
            assert catalog["complete"] and catalog["device_write_commands_sent"] is False
            full = json.loads(Path(catalog["catalog_file"]).read_text(encoding="utf-8"))
            assert full["origin"] == "live_device_B6_B4_B5_B1_B3" and full["identity"]["mac"] == args.expected_mac.upper()
            found = payload(await session.call_tool("search_device_parameters", {"query": "B05.01", "group": "B05"}))
            assert len(found["items"]) == 1 and found["current_values_included"] is False
            channels = payload(await session.call_tool("list_oscilloscope_channels", {"group": "P11", "limit": 100}))
            assert channels["reference_only"] is False and channels["capture_started"] is False
            assert {"P11.03", "P11.04", "P11.05", "P11.06"}.issubset({p["code"] for p in channels["channels"]})
            live = payload(await session.call_tool("read_device_parameters", {"codes": ["A00.00", "A00.06", "B05.01", "P12.07", "P13.45"]}))
            assert live["device_write_commands_sent"] is False and live["identity"] == catalog["identity"]
            values = {p["code"]: p for p in live["result"]["parameters"]}
            assert values["B05.01"]["name"] == found["items"][0]["name"]
            assert values["P13.45"]["width_bits"] == 32
            report = {"status": "passed", "tool_count": len((await session.list_tools()).tools), "catalog": catalog,
                      "search": found, "channels": channels, "live": live,
                      "parameter_write_executed": False, "motor_started": False, "scope_started": False}
            path = PROJECT / "data/online_catalog_mcp_verification.json"
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"status": "passed", "tool_count": report["tool_count"], "identity": catalog["identity"],
                              "major_groups": len(catalog["major_groups"]), "groups": len(catalog["groups"]),
                              "parameter_words": catalog["parameter_words"], "parameter_count": catalog["parameter_count"],
                              "p11_channels": channels["total"], "selected_names": {p["code"]: p["name"] for p in channels["channels"] if p["code"] in {"P11.03", "P11.04", "P11.05", "P11.06"}},
                              "state_raw": values["A00.00"]["raw_value"], "speed": values["A00.06"]["value"],
                              "control_mode_raw": values["B05.01"]["raw_value"], "report": str(path),
                              "device_write_commands_sent": False}, ensure_ascii=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--expected-ip", required=True)
    parser.add_argument("--expected-mac", required=True)
    asyncio.run(verify(parser.parse_args()))
