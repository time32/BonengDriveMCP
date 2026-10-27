"""Exercise the actual stdio MCP server using the official SDK client."""

from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT = Path(__file__).resolve().parents[1]


def payload(result):
    if result.isError:
        raise AssertionError(result.content)
    data = result.structuredContent
    if data is None:
        raise AssertionError("Expected structured MCP output")
    return data["result"] if set(data) == {"result"} else data


async def run():
    checks = []
    parent = (PROJECT / "workspace" / "test_artifacts").resolve()
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stdio_", dir=parent) as temporary:
        workspace = Path(temporary).resolve()
        assert workspace.is_relative_to(parent)
        (workspace / "wave.csv").write_text("time_s,speed\n0,1\n0.1,2\n0.2,3\n", encoding="utf-8")
        environment = {**os.environ, "BONENG_WORKSPACE": str(workspace), "BONENG_DATA_DIR": str(PROJECT / "data"),
                       "BONENG_SERIAL_MAC": "02:00:00:00:00:01", "PYTHONIOENCODING": "utf-8"}
        parameters = StdioServerParameters(command=sys.executable, args=["-u", str(PROJECT / "run_server.py")], env=environment, cwd=str(PROJECT))
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "boneng-drivesoft"
                checks.append("MCP initialize and stdio framing")
                listing = await session.list_tools()
                tools = {tool.name: tool for tool in listing.tools}
                assert len(tools) == 54
                assert tools["get_capabilities"].annotations.readOnlyHint is True
                assert tools["create_parameter_set"].annotations.readOnlyHint is False
                assert tools["probe_device_read_only"].annotations.readOnlyHint is True
                assert tools["scan_network_devices"].annotations.readOnlyHint is True
                assert tools["select_network_device"].annotations.readOnlyHint is False
                assert tools["probe_selected_device_read_only"].annotations.readOnlyHint is True
                assert tools["start_firmware_upgrade"].annotations.destructiveHint is True
                assert tools["get_firmware_upgrade_status"].annotations.readOnlyHint is True
                assert tools["read_device_parameters"].annotations.readOnlyHint is True
                assert tools["discover_device_parameter_catalog"].annotations.readOnlyHint is True
                assert tools["search_device_parameters"].annotations.readOnlyHint is True
                assert tools["list_oscilloscope_channels"].annotations.openWorldHint is True
                assert tools["create_device_parameter_snapshot"].annotations.destructiveHint is False
                assert tools["create_device_parameter_snapshot"].annotations.openWorldHint is True
                assert tools["compare_device_parameter_snapshots"].annotations.readOnlyHint is True
                assert tools["capture_oscilloscope"].annotations.destructiveHint is True
                assert tools["execute_drive_run"].annotations.destructiveHint is True
                assert tools["register_firmware_build"].annotations.openWorldHint is False
                assert tools["start_firmware_validation"].annotations.destructiveHint is True
                assert tools["get_firmware_validation_status"].annotations.readOnlyHint is True
                assert tools["inspect_serial_device"].annotations.readOnlyHint is True
                checks.append("54 tool schemas and read/write annotations")
                history = payload(await session.call_tool("get_firmware_upgrade_status", {}))
                assert history["status"] in {"no_firmware_jobs", "historical_verified_run"}
                assert history["hardware_contacted"] is False
                checks.append("firmware history query never starts an upgrade")
                assert payload(await session.call_tool("get_firmware_validation_status", {}))["status"] == "no_validation_jobs"
                image = workspace / "compiled.bin"
                image.write_bytes(struct.pack("<4I", 0xEE3355BB, 1966096, 0x80000000, 0x80000000) + bytes(1966100-16))
                build = payload(await session.call_tool("register_firmware_build", {"firmware_path": str(image), "label": "SDK simulation only", "expected_release_hex": "6909", "expected_parameter_version_raw": 11}))
                image.write_bytes(b"next compile cannot overwrite sealed build")
                assert payload(await session.call_tool("get_firmware_build", {"build_id": build["build_id"]}))["firmware_sha256"] == build["firmware_sha256"]
                preview = payload(await session.call_tool("prepare_firmware_validation", {"build_id": build["build_id"], "problem": "SDK offline preview", "channels": ["P11.03"], "sample_period_ms": 2, "points": 1000, "transport": "serial"}))
                assert preview["hardware_contacted"] is False and preview["upgrade_started"] is False
                assert preview["motor_control"] == "none" and preview["restart"] == "manual"
                checks.append("registered build sealing and offline firmware-validation preview through MCP")
                assert payload(await session.call_tool("get_network_selection", {}))["selected"] is False
                assert (await session.call_tool("probe_selected_device_read_only", {})).isError
                assert (await session.call_tool("discover_device_parameter_catalog", {})).isError
                assert (await session.call_tool("list_oscilloscope_channels", {"group": "P11"})).isError
                assert payload(await session.call_tool("list_device_parameter_snapshots", {}))["total"] == 0
                assert (await session.call_tool("create_device_parameter_snapshot", {"label": "no target"})).isError
                checks.append("no network connection without explicit device selection")
                caps = payload(await session.call_tool("get_capabilities", {}))
                assert caps["mode"] in {"offline", "offline_with_read_only_probe", "offline_with_device_tools"} and caps["device_connected"] is False
                checks.append("honest capability and connection status")
                catalogs = payload(await session.call_tool("list_parameter_catalogs", {}))
                cn = next(item for item in catalogs if item["id"] == "cn-v1.11")
                assert cn["row_count"] >= 1400 and "F01.36" in cn["ambiguous_codes"]
                checks.append("real catalog and extraction warnings")
                result = payload(await session.call_tool("search_parameters", {"query": "电机", "catalog_id": "cn-v1.11", "limit": 2}))
                assert result["total"] > 2 and len(result["items"]) == 2
                checks.append("Chinese search and pagination")
                definition = payload(await session.call_tool("get_parameter_definition", {"catalog_id": "cn-v1.11", "code": "B00.00"}))
                assert definition["name"] == "系统预约"
                assert definition["fields"]["通讯地址"] == "0x3000"
                checks.append("correct ACE stored-column ordering")
                state = payload(await session.call_tool("lookup_enum_value", {"catalog_id": "cn-v1.11", "code": "A00.00", "value": "S12"}))
                assert state["matches"] == ["运行"]
                checks.append("reference state enum lookup")
                for name, values in [("baseline", {"B00.00": "0", "D00.00": "0"}), ("target", {"D00.00": "1"})]:
                    payload(await session.call_tool("create_parameter_set", {"set_id": name, "catalog_id": "cn-v1.11", "values": values}))
                assert len(payload(await session.call_tool("list_parameter_sets", {}))) == 2
                assert payload(await session.call_tool("get_parameter_set", {"set_id": "baseline"}))["device_verified"] is False
                checks.append("save/list/load offline parameter sets")
                arguments = {"baseline_id": "baseline", "target_id": "target"}
                diff = payload(await session.call_tool("compare_parameter_sets", arguments))
                assert len(diff["changes"]) == 2
                plan = payload(await session.call_tool("plan_batch_configuration", arguments))
                assert len(plan["operations"]) == 1 and plan["execution_supported"] is False
                assert plan["omitted_parameters_action"] == "leave_unchanged"
                checks.append("batch diff and absent-key preservation")
                wave = payload(await session.call_tool("summarize_waveform", {"filename": "wave.csv"}))
                assert wave["samples"] == 3 and wave["signals"]["speed"]["mean"] == 2
                assert math.isclose(wave["signals"]["speed"]["rms"], math.sqrt(14 / 3))
                checks.append("existing waveform CSV statistics")
                inventory = payload(await session.call_tool("get_release_interfaces", {"type_query": "CCommModbus"}))
                assert inventory["total_matching_types"] >= 1 and inventory["device_calls_executed"] is False
                checks.append("static interface evidence")
                bad_calls = [
                    ("probe_device_read_only", {"port": "../COM7"}),
                    ("inspect_serial_device", {"port": "../COM7"}),
                    ("inspect_serial_device", {"mode": "read", "codes": ["A99.99"]}),
                    ("scan_network_devices", {"interface_ip": "127.0.0.1"}),
                    ("scan_network_devices", {"interface_ip": "192.168.0.205", "timeout_s": True}),
                    ("select_network_device", {"scan_id": "../outside", "device_id": "D01"}),
                    ("preflight_firmware_upgrade", {"firmware_path": "relative.bin", "transport": "serial"}),
                    ("start_firmware_upgrade", {"plan_id": "../outside"}),
                    ("verify_firmware_upgrade_boot", {"job_id": "../outside"}),
                    ("get_firmware_build", {"build_id": "../outside"}),
                    ("start_firmware_validation", {"plan_id": "../outside"}),
                    ("cancel_firmware_validation", {"job_id": "../outside"}),
                    ("get_parameter_definition", {"catalog_id": "cn-v1.11", "code": "F01.36"}),
                    ("get_parameter_set", {"set_id": "../escape"}),
                    ("summarize_waveform", {"filename": "../escape.csv"}),
                    ("create_parameter_set", {"set_id": "invalid", "catalog_id": "cn-v1.11", "values": {"D00.00": True}}),
                ]
                for tool, args in bad_calls:
                    result = await session.call_tool(tool, args)
                    assert result.isError, f"Expected an MCP error from {tool}"
                checks.append("ambiguous codes, path escape and invalid types fail explicitly")
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "status": "passed", "checks": checks, "tools_tested": len(tools), "hardware_tests_performed": False}
    (PROJECT / "data" / "mcp_verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "checks": len(checks), "tools_tested": len(tools), "hardware_tests": False}))


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(run(), timeout=90))
