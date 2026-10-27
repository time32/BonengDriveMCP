"""Persistent local worker for an explicitly started firmware validation plan."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from boneng_mcp.paths import Settings
from boneng_mcp.original_probe import OriginalSoftwareProbe
from boneng_mcp.network import NetworkDevices
from boneng_mcp.firmware import FirmwareUpgrades
from boneng_mcp.device import DeviceOperations
from boneng_mcp.snapshots import ParameterSnapshots
from boneng_mcp.validation import FirmwareValidation


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings(args.release_dir, args.data_dir, args.workspace)
    network = NetworkDevices(settings.workspace_dir, OriginalSoftwareProbe(settings.release_dir))
    firmware = FirmwareUpgrades(settings.release_dir, settings.workspace_dir, settings.data_dir, network)
    device = DeviceOperations(settings.release_dir, settings.workspace_dir, settings.data_dir, network, firmware)
    snapshots = ParameterSnapshots(settings.workspace_dir, device)
    worker = FirmwareValidation(settings, firmware, device, snapshots)
    from boneng_mcp.device import identifier
    ready = settings.workspace_dir / "validation/jobs" / identifier(args.job_id) / "ready"
    deadline = time.monotonic() + 30
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not ready.exists(): raise SystemExit("Validation launch metadata was not committed; no device actions performed.")
    worker.execute(args.job_id)
