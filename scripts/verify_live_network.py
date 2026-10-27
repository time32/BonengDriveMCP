"""Verify MCP discovery and a device that the user has already explicitly selected."""
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
    value = result.structuredContent
    if not isinstance(value, dict):
        raise RuntimeError("Expected structured MCP output")
    return value["result"] if set(value) == {"result"} else value


async def verify(args):
    environment = {**os.environ, "BONENG_RELEASE_DIR": str(args.release_dir),
                   "BONENG_WORKSPACE": str(PROJECT / "workspace"), "PYTHONIOENCODING": "utf-8"}
    parameters = StdioServerParameters(command=sys.executable, args=["-u", str(PROJECT / "run_server.py")],
                                      env=environment, cwd=str(PROJECT))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            assert len(listing.tools) == 54
            interfaces = payload(await session.call_tool("list_network_interfaces", {}))
            assert args.interface_ip in {row["ip"] for row in interfaces["interfaces"]}
            before = payload(await session.call_tool("get_network_selection", {}))
            scan = payload(await session.call_tool("scan_network_devices", {"interface_ip": args.interface_ip}))
            assert scan["tcp_connections_opened"] is False and scan["device_configuration_changed"] is False
            after = payload(await session.call_tool("get_network_selection", {}))
            assert before == after, "Discovery must not change selection."
            # The exact IP and MAC are provided from the human's choice; no heuristic auto-selection.
            matches = [row for row in scan["devices"] if row["ip"] == args.selected_ip and row["mac"] == args.selected_mac.upper()]
            if len(matches) != 1:
                raise RuntimeError("The user's selected identity is absent or ambiguous in this scan. No TCP connection opened.")
            selection = payload(await session.call_tool("select_network_device", {"scan_id": scan["scan_id"], "device_id": matches[0]["device_id"]}))
            assert selection["selection_only"] is True and selection["device_connected"] is False
            probe = payload(await session.call_tool("probe_selected_device_read_only", {}))
            assert probe["device_identity_matches_discovery"] is True and probe["device_write_commands_sent"] is False
            assert probe["connection_closed_after_probe"] is True
            assert probe["mac_address"] == args.selected_mac.upper()
            report = {"status": "passed", "verification_transport": "MCP stdio", "hardware_tests_performed": True,
                      "tool_count": len(listing.tools), "interfaces": interfaces, "discovery": scan,
                      "selection": selection, "probe": probe}
            (PROJECT / "data" / "network_verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"status": "passed", "tool_count": len(listing.tools), "discovered": scan["devices"],
                              "selected_ip": args.selected_ip, "mac": probe["mac_address"],
                              "firmware_version_raw": probe["firmware_version_raw"], "state_raw": probe["state_raw"],
                              "connection_closed": True}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--interface-ip", required=True)
    parser.add_argument("--selected-ip", required=True, help="Exact IP explicitly chosen by the user")
    parser.add_argument("--selected-mac", required=True, help="Exact scan MAC explicitly chosen by the user")
    asyncio.run(asyncio.wait_for(verify(parser.parse_args()), timeout=90))
