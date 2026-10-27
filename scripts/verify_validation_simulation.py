"""Reproducible offline orchestration and vendor loopback verification; never flash."""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tests"))
from test_builds_and_validation import BuildAndValidationTests
from boneng_mcp import __version__


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.discover(str(PROJECT / "tests"), pattern=pattern) for pattern in (
        "test_builds_and_validation.py", "test_firmware.py", "test_firmware_bridge_simulation.py"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful(): raise SystemExit(1)
    case = BuildAndValidationTests(); case.setUp()
    try:
        job = case.start(case.plan())
        finished = case.execute(job)
        assert finished["status"] == "completed", finished
        report = case.validation.status(job["job_id"])["report"]
        evidence = {"checked_at": datetime.now(timezone.utc).isoformat(), "server_version": __version__,
            "status": "passed", "tests_run": result.testsRun, "tests_skipped": len(result.skipped),
            "hardware_tests_performed": False, "real_flash_performed": False,
            "orchestration_backend": "mocked firmware/device, real build sealing, job files and CSV statistics",
            "vendor_boundary": "actual vendor assemblies, read-only preflight/boot against 127.0.0.2 only; skipped without BONENG_RELEASE_DIR",
            "verified_scenarios": ["registered artifact/hash", "single flash per plan", "manual reboot wait", "wrong release/revision rejection",
                "timeout", "cancel deferred during flash", "detached worker cancellation", "bridge/target changes", "online catalog refresh",
                "configuration backups", "cross-round channel meaning check", "numerical deltas only for matching definitions"],
            "example_phases": [event["phase"] for event in finished["events"]],
            "example_statistics": report["statistics"], "root_cause_diagnosed": False}
        evidence["example_statistics"].pop("filename", None)
        path = PROJECT / "data/firmware_validation_simulation_verification.json"
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "passed", "tests": result.testsRun, "skipped": len(result.skipped), "report": str(path)}))
    finally:
        case.doCleanups()
