"""Verify the source distribution through MCP stdio without vendor data/hardware."""
from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT = Path(__file__).resolve().parents[1]


async def main() -> None:
    checks = []
    parent = PROJECT / "workspace" / "test_artifacts"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clean_stdio_", dir=parent) as temporary:
        root = Path(temporary).resolve()
        workspace, data = root / "workspace", root / "data"
        workspace.mkdir(); data.mkdir()
        (workspace / "example.csv").write_text("time_s,speed\n0,1\n0.002,2\n0.004,3\n", encoding="utf-8")
        env = {**os.environ, "BONENG_RELEASE_DIR": str(root / "no_vendor"),
               "BONENG_DATA_DIR": str(data), "BONENG_WORKSPACE": str(workspace),
               "BONENG_SERIAL_MAC": "", "PYTHONIOENCODING": "utf-8"}
        parameters = StdioServerParameters(command=sys.executable,
            args=["-u", str(PROJECT / "run_server.py")], cwd=str(PROJECT), env=env)
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                async def call(name, args, expect_error=False):
                    result = await session.call_tool(name, args)
                    assert result.isError == expect_error, (name, result.content)
                    if expect_error: return None
                    value = result.structuredContent
                    assert isinstance(value, dict), name
                    return value["result"] if set(value) == {"result"} else value

                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                assert len(tools) == 54
                assert tools["read_device_parameters"].annotations.readOnlyHint is True
                assert tools["execute_drive_run"].annotations.destructiveHint is True
                assert tools["start_firmware_upgrade"].annotations.destructiveHint is True
                assert tools["apply_drive_speed_change"].annotations.destructiveHint is True
                assert tools["execute_drive_position_move"].annotations.destructiveHint is True
                assert tools["read_drive_motion_limits"].annotations.readOnlyHint is True
                assert tools["plan_drive_run"].inputSchema["properties"]["duration_s"]["default"] is None
                checks.append("54 tool schemas and operation annotations")
                caps = await call("get_capabilities", {})
                assert not caps["device_connected"] and not caps["device_operations"]["configured"]
                assert caps["catalogs"] == []
                checks.append("starts with empty data and no vendor executable")
                for name in ("list_parameter_catalogs", "get_release_interfaces"):
                    await call(name, {}, True)
                checks.append("missing proprietary reference data is explicit")
                assert not (await call("get_network_selection", {}))["selected"]
                assert (await call("get_drive_run_status", {}))["status"] == "no_run_jobs"
                assert (await call("get_firmware_upgrade_status", {}))["status"] == "no_firmware_jobs"
                checks.append("no inherited target or job")
                for name, args in [
                    ("read_device_parameters", {"codes": ["A00.00"]}),
                    ("probe_selected_device_read_only", {}),
                    ("select_serial_device", {"port": "../COM7"}),
                    ("inspect_serial_device", {"port": "COM0"}),
                    ("scan_network_devices", {"interface_ip": "127.0.0.1"}),
                    ("capture_oscilloscope", {"codes": ["P11.03"], "sample_period_ms": 2, "points": 0}),
                    ("execute_drive_run", {"plan_id": "../escape"}),
                    ("execute_drive_position_move", {"plan_id": "../escape"}),
                    ("apply_drive_speed_change", {"plan_id": "../escape"}),
                    ("read_drive_motion_limits", {}),
                    ("start_firmware_upgrade", {"plan_id": "../escape"}),
                    ("summarize_waveform", {"filename": "../escape.csv"}),
                ]:
                    await call(name, args, True)
                checks.append("unselected targets and invalid operations are rejected")
                waveform = await call("summarize_waveform", {"filename": "example.csv"})
                assert waveform["samples"] == 3 and waveform["signals"]["speed"]["mean"] == 2
                checks.append("CSV summary")
                for profile, software, size, release in [
                    ("an_pn_spi_910", 910, 1966100, "6909"),
                    ("cm55_ec_spi_612", 612, 1044, "6630"),
                ]:
                    image = root / (profile + ".bin")
                    image.write_bytes(struct.pack("<4I", 0xEE3355BB, size - 4, 0x80000000, 0x80000000) + bytes(size - 16))
                    build = await call("register_firmware_build", {"firmware_path": str(image),
                        "label": "Synthetic artifact; no hardware", "expected_release_hex": release,
                        "expected_software_raw": software, "profile": profile})
                    image.write_bytes(b"later compile")
                    sealed = await call("get_firmware_build", {"build_id": build["build_id"]})
                    assert sealed["firmware_sha256"] == build["firmware_sha256"]
                    assert not sealed["hardware_contacted"] and not sealed["upgrade_started"]
                checks.append("both build profiles seal artifacts without starting a flash")
        print(json.dumps({"status": "passed", "tools": len(tools), "checks": checks,
                          "hardware_contacted": False, "vendor_data_required": False}))


if __name__ == "__main__":
    asyncio.run(main())
