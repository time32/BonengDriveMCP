"""Durable flash -> manual reboot -> identity -> capture experiment sequences."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .builds import build_identifier, sha256, CM55_BUILD_PROFILE
from .detached import start_detached
from .device import DeviceOperations, identifier
from .firmware import FirmwareUpgrades
from .paths import Settings, UserInputError, read_json
from .snapshots import ParameterSnapshots
from .validation_guard import TERMINAL
from .waveforms import summarize_waveform

PROJECT = Path(__file__).resolve().parents[2]


class FirmwareValidation:
    def __init__(self, settings: Settings, firmware: FirmwareUpgrades, device: DeviceOperations, snapshots: ParameterSnapshots):
        self.settings, self.firmware, self.device, self.snapshots = settings, firmware, device, snapshots
        self.root = settings.workspace_dir / "validation"

    def _target(self, transport: str, port: str) -> dict[str, Any]:
        if transport == "selected_tcp":
            return self.firmware._selected_target()
        if transport == "serial":
            target = self.device._target(transport, port)
            if target.get("serial_binding_id"):
                return {**target, "target_mac": None}
            return {"target_mac": target["expected_mac"], "port": port}
        raise UserInputError("Use a selected TCP device or the identified COM7 device.")

    def prepare(self, build_id: str, problem: str, channels: list[str], sample_period_ms: int, points: int,
                transport: str = "selected_tcp", port: str = "COM7", wait_timeout_s: int = 600,
                backup_configuration: bool = True, read_codes: list[str] | None = None,
                baseline_job_id: str = "", hypothesis: str = "") -> dict[str, Any]:
        build = self.firmware.builds.get(build_identifier(build_id))
        target = self._target(transport, port)
        if (build["profile"] == CM55_BUILD_PROFILE) != bool(target.get("serial_binding_id")):
            raise UserInputError("Validation firmware profile differs from the selected AN/CM55 target.")
        if not isinstance(problem, str) or not problem.strip() or len(problem) > 5000 or not isinstance(hypothesis, str) or len(hypothesis) > 5000:
            raise UserInputError("Provide a problem description and optional hypothesis, each up to 5000 characters.")
        self.device._validate_codes(channels)
        if len(channels) > 4 or any(not c.startswith("P") for c in channels):
            raise UserInputError("Choose 1–4 P channels for this validation task.")
        if type(sample_period_ms) is not int or not 2 <= sample_period_ms <= 1000 or sample_period_ms % 2 or type(points) is not int or not 1 <= points <= 20000 or points * sample_period_ms > 40000:
            raise UserInputError("Use an even 2–1000ms period, at most 20000 points and 40 seconds.")
        if type(wait_timeout_s) is not int or not 30 <= wait_timeout_s <= 3600 or type(backup_configuration) is not bool:
            raise UserInputError("Reboot wait must be 30–3600 seconds; backup_configuration must be boolean.")
        if read_codes is None:
            read_codes = ["A00.00", "A00.06", "B05.01", "P12.07"]
        self.device._validate_codes(read_codes)
        baseline = self.status(identifier(baseline_job_id)) if baseline_job_id else None
        if baseline and baseline["status"] != "completed":
            raise UserInputError("Choose a completed validation task as the baseline.")
        plan_id = uuid.uuid4().hex
        bridge_hashes = {str(p): sha256(p) for p in (self.firmware.bridge, self.device.bridge) if p.is_file()}
        plan = {"plan_id": plan_id, "created_at": time.time(), "expires_at": time.time() + 1800,
            "build_id": build_id, "build": build, "problem": problem.strip(), "hypothesis": hypothesis,
            "transport": transport, "port": port, "target": target, "channels": channels,
            "sample_period_ms": sample_period_ms, "points": points, "read_codes": read_codes,
            "wait_timeout_s": wait_timeout_s, "backup_configuration": backup_configuration,
            "baseline_job_id": baseline_job_id, "bridge_hashes": bridge_hashes,
            "motor_control": "none", "restart": "manual", "hardware_contacted": False, "upgrade_started": False,
            "automatic_actions_after_start": ["optional_parameter_backup", "preflight", "single_flash", "wait_manual_reboot",
                "verify_identity", "refresh_online_catalog", "optional_parameter_backup", "capture_once", "save_statistics_report"]}
        folder = self.root / "plans"; folder.mkdir(parents=True, exist_ok=True)
        with (folder / (plan_id + ".json")).open("x", encoding="utf-8") as output:
            json.dump(plan, output, ensure_ascii=False, indent=2)
        return plan

    def _save(self, job: dict[str, Any]) -> None:
        path = self.root / "jobs" / job["job_id"] / "job.json"
        job["updated_at"] = time.time()
        temporary = path.with_name(uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def start(self, plan_id: str) -> dict[str, Any]:
        folder = self.root / "plans"
        plan = read_json(folder / (identifier(plan_id) + ".json"))
        if time.time() > plan["expires_at"] or plan["target"] != self._target(plan["transport"], plan["port"]):
            raise UserInputError("Validation preview expired or selected target changed.")
        if self.firmware.builds.get(plan["build_id"]) != plan["build"]:
            raise UserInputError("Registered build changed since validation preview.")
        for name, digest in plan["bridge_hashes"].items():
            if sha256(Path(name)) != digest:
                raise UserInputError("Device bridge changed since validation preview.")
        if not self.firmware.describe()["configured"] or not self.device.describe()["configured"]:
            raise UserInputError("Build and configure both vendor bridges before starting validation.")
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / "start.lock"
        try:
            with lock.open("x") as output: output.write(plan_id)
        except FileExistsError as error:
            raise UserInputError("Another validation start is being prepared.") from error
        try:
            active = self.root / "active.json"
            if active.is_file():
                previous = self.status(read_json(active)["job_id"])
                if previous["status"] not in TERMINAL:
                    raise UserInputError("A validation task is active or uncertain; inspect/cancel it first.")
                active.unlink()
            try:
                with (folder / (plan_id + ".used")).open("x") as output: output.write(str(time.time()))
            except FileExistsError as error:
                raise UserInputError("This validation plan was already started; never repeat its flash automatically.") from error
            job_id = uuid.uuid4().hex
            destination = self.root / "jobs" / job_id; destination.mkdir(parents=True)
            job = {"job_id": job_id, "plan": plan, "status": "starting", "created_at": time.time(),
                   "events": [], "automatic_motor_start": False, "hardware_bench_verified": False}
            self._save(job)
            with active.open("x", encoding="utf-8") as output: json.dump({"job_id": job_id}, output)
            import sys
            command = [sys.executable, str(PROJECT / "scripts/validation_worker.py"), "--job-id", job_id,
                       "--workspace", str(self.settings.workspace_dir), "--release-dir", str(self.settings.release_dir), "--data-dir", str(self.settings.data_dir)]
            try:
                launched = start_detached(command, PROJECT, destination)
                import psutil
                try: created_at = psutil.Process(launched["pid"]).create_time()
                except psutil.Error: created_at = job["created_at"]
                job.update(pid=launched["pid"], process_created_at=created_at, launcher=launched["launcher"])
            except Exception as error:
                job.update(status="failed", error="Validation worker launch failed: " + str(error))
            # The worker waits for persisted process metadata before device actions.
            self._save(job)
            if job["status"] == "starting" and job.get("pid"):
                (destination / "ready").touch()
            return self.status(job_id)
        finally:
            lock.unlink(missing_ok=True)

    def status(self, job_id: str = "") -> dict[str, Any]:
        if not job_id:
            active = self.root / "active.json"
            if not active.is_file(): return {"status": "no_validation_jobs", "hardware_contacted": False}
            job_id = read_json(active)["job_id"]
        path = self.root / "jobs" / identifier(job_id) / "job.json"
        job = read_json(path)
        alive = False
        if job.get("pid") and job["status"] not in TERMINAL:
            import psutil
            try:
                process = psutil.Process(job["pid"])
                alive = abs(process.create_time() - job["process_created_at"]) < 2 and job_id in process.cmdline()
            except (psutil.Error, OSError): pass
        if job["status"] not in TERMINAL and job.get("pid") and not alive:
            job.update(status="result_unknown", last_persisted_status=job["status"], recovery="No automatic reflash or collection; inspect the firmware job and cancel this validation task.")
        report = read_json(path.with_name("report.json")) if path.with_name("report.json").is_file() else None
        return {**job, "worker_alive": alive, "report": report, "report_file": str(path.with_name("report.json")) if report else None,
                "status_query_contacted_hardware": False, "cancel_requested": path.with_name("cancel.request").exists()}

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.status(identifier(job_id)); path = self.root / "jobs" / job_id / "cancel.request"
        if job["status"] in TERMINAL: return job
        path.touch()
        if not job["worker_alive"]:
            upgrade_id = job.get("upgrade_job_id")
            if upgrade_id and self.firmware.status(upgrade_id)["status"] not in {"failed", "flash_verified", "boot_verified"}:
                return {**job, "cancel_requested": True, "message": "Flash may still be active; cancellation never kills or interrupts the updater."}
            original = read_json(path.with_name("job.json")); original.update(status="cancelled", cancellation="Worker absent; no future validation actions authorized."); self._save(original)
        return self.status(job_id)

    def execute(self, job_id: str, sleep=time.sleep, now=time.monotonic) -> dict[str, Any]:
        job_id = identifier(job_id); folder = self.root / "jobs" / job_id
        lock = folder / "worker.lock"
        with lock.open("x") as output: output.write(str(os.getpid()))
        job = read_json(folder / "job.json"); plan = job["plan"]
        if job["status"] != "starting":
            lock.unlink(missing_ok=True)
            raise UserInputError("Only a fresh starting validation job may execute; never resume a flash automatically.")
        old_owner = os.environ.get("BONENG_VALIDATION_JOB"); os.environ["BONENG_VALIDATION_JOB"] = job_id
        def phase(name, **fields):
            job.update(status=name, **fields); job["events"].append({"phase": name, "at": time.time()}); self._save(job)
        def check():
            if (folder / "cancel.request").exists(): raise InterruptedError("Validation cancelled; no new capture or flash.")
            if self._target(plan["transport"], plan["port"]) != plan["target"]:
                raise UserInputError("Selected target changed during validation; task stopped.")
        try:
            check(); build = self.firmware.builds.get(plan["build_id"])
            if build != plan["build"]: raise UserInputError("Registered build differs from prepared task.")
            for name, digest in plan["bridge_hashes"].items():
                if sha256(Path(name)) != digest: raise UserInputError("Device bridge changed after validation was started.")
            job["device_operations_attempted"] = True
            if plan["backup_configuration"]:
                phase("backing_up_before_flash")
                job["before_snapshot"] = self.snapshots.create("Before " + build["label"], transport=plan["transport"], port=plan["port"])
            check(); phase("preflight")
            preflight = self.firmware.preflight("", plan["transport"], plan["port"], plan["build_id"])
            job["firmware_plan_id"] = preflight["plan_id"]; self._save(job)
            check(); phase("flashing")
            upgrade = self.firmware.start(preflight["plan_id"])
            upgrade_id = upgrade["job_id"]
            job["upgrade_job_id"] = upgrade_id; self._save(job)
            flash_deadline = now() + 1800
            while True:
                # Cancellation is deferred until Flash result: never terminate an updater.
                upgrade = self.firmware.status(upgrade_id)
                if upgrade["status"] in {"flash_verified", "boot_verified"}: break
                if upgrade["status"] in {"failed", "result_unknown"}: raise UserInputError("Flash did not verify; no automatic retry: " + str(upgrade.get("error")))
                if now() >= flash_deadline: raise TimeoutError("Flash wait timed out; inspect updater status, do not restart writing.")
                sleep(2)
            check(); phase("waiting_for_manual_reboot", message="Flash verified. Power-cycle manually; verified boot automatically continues this task.")
            deadline = now() + plan["wait_timeout_s"]
            while True:
                check()
                try:
                    verified = self.firmware.verify_boot(upgrade_id)
                    if verified["status"] == "boot_verified": break
                except UserInputError as error:
                    job["last_boot_read_error"] = str(error)[-2000:]; self._save(job)
                if now() >= deadline: raise TimeoutError("Manual reboot/expected firmware was not verified before the deadline; no collection started.")
                sleep(3)
            check(); phase("refreshing_device_catalog")
            catalog = self.device.discover_catalog(plan["transport"], plan["port"])
            identity = catalog["identity"]
            if plan["target"].get("serial_binding_id") and identity.get("serial_binding_id") != plan["target"]["serial_binding_id"]:
                raise UserInputError("Post-reboot serial binding differs from the validation target.")
            if identity["mac"] != plan["target"]["target_mac"] or identity["software_raw"] != build["expected_software_raw"] or identity["release_hex"] != build["expected_release_hex"] or (build["expected_parameter_version_raw"] is not None and identity["parameter_version_raw"] != build["expected_parameter_version_raw"]):
                raise UserInputError("Post-reboot catalog identity does not match the registered build.")
            observations = self.device.read(plan["read_codes"], plan["transport"], plan["port"])
            if plan["backup_configuration"]:
                check(); phase("backing_up_after_reboot")
                job["after_snapshot"] = self.snapshots.create("After " + build["label"], transport=plan["transport"], port=plan["port"])
            check(); phase("collecting_waveform")
            capture = self.device.capture(plan["channels"], plan["sample_period_ms"], plan["points"], plan["transport"], plan["port"])
            job["capture"] = capture; self._save(job)
            check(); phase("computing_statistics")
            relative = str(Path(capture["csv_file"]).relative_to(self.settings.workspace_dir))
            statistics = summarize_waveform(self.settings.workspace_dir, relative)
            report = {"job_id": job_id, "problem": plan["problem"], "hypothesis": plan["hypothesis"], "build": build,
                "target": plan["target"], "catalog": catalog, "observations": observations, "capture": capture, "statistics": statistics,
                "channels": plan["channels"], "sample_period_ms": plan["sample_period_ms"], "points": plan["points"],
                "motor_control": "none", "automatic_restart_sent": False, "root_cause_diagnosed": False,
                "next_step": "Assistant compares evidence with source/hypothesis; numerical statistics alone do not establish root cause."}
            if job.get("before_snapshot") and job.get("after_snapshot"):
                report["parameter_comparison"] = self.snapshots.compare(job["before_snapshot"]["snapshot_id"], job["after_snapshot"]["snapshot_id"])
            if plan["baseline_job_id"]:
                baseline = self.status(plan["baseline_job_id"])["report"]
                report["baseline_job_id"] = plan["baseline_job_id"]
                fields = ("code", "name", "attribute_raw", "width_bits", "signed", "unit", "unit_index", "decimal_attribute_raw")
                definitions_equal = len(baseline["capture"]["result"]["definitions"]) == len(capture["result"]["definitions"]) and all(
                    all(old.get(k) == new.get(k) for k in fields) for old, new in zip(baseline["capture"]["result"]["definitions"], capture["result"]["definitions"]))
                same_device = baseline["target"]["target_mac"] == report["target"]["target_mac"]
                if plan["target"].get("serial_binding_id") or baseline["target"].get("serial_binding_id"):
                    same_device = baseline["target"].get("serial_binding_id") == plan["target"].get("serial_binding_id")
                comparable = same_device and definitions_equal and all(baseline.get(key) == report[key] for key in ("channels", "sample_period_ms", "points"))
                report["baseline_waveform_comparable"] = comparable
                if comparable:
                    previous = baseline["statistics"]["signals"]
                    report["waveform_statistic_changes"] = {code: {key: value - previous[code][key] for key, value in stats.items() if key in previous[code]}
                        for code, stats in statistics["signals"].items() if code in previous}
                else: report["comparison_note"] = "Device, channel definitions or acquisition settings differ; no numerical delta was computed."
            (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            phase("completed", message="Collection and statistics saved; ready for source-based cause analysis.")
        except InterruptedError as error: phase("cancelled", error=str(error))
        except TimeoutError as error: phase("timed_out", error=str(error))
        except Exception as error: phase("failed", error=str(error))
        finally:
            if old_owner is None: os.environ.pop("BONENG_VALIDATION_JOB", None)
            else: os.environ["BONENG_VALIDATION_JOB"] = old_owner
            lock.unlink(missing_ok=True)
        return job
