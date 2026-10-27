"""Call the fixed read-only probe through a real MCP stdio client."""

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


async def verify(release_dir: Path, port: str) -> None:
    environment = {**os.environ, "BONENG_RELEASE_DIR": str(release_dir.resolve()), "PYTHONIOENCODING": "utf-8"}
    parameters = StdioServerParameters(command=sys.executable, args=["-u", str(PROJECT / "run_server.py")], env=environment, cwd=str(PROJECT))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()
            tool = next(item for item in listing.tools if item.name == "probe_device_read_only")
            assert tool.annotations.readOnlyHint is True
            result = await session.call_tool(tool.name, {"port": port})
            if result.isError:
                raise RuntimeError(result.content)
            report = result.structuredContent
            assert report["status"] == "passed"
            assert report["device_write_commands_sent"] is False
            assert report["origin"] == "live_device_read_via_original_software"
            report = {**report, "verification_transport": "MCP stdio", "hardware_tests_performed": True}
            output = PROJECT / "data" / "original_reuse_verification.json"
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--port", default="COM7")
    args = parser.parse_args()
    asyncio.run(asyncio.wait_for(verify(args.release_dir, args.port), timeout=65))
