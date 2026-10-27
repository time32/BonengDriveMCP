"""MCP wrapper for the vendor upgrade flow already verified on one file/device."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .original_probe import _PROBE_LOCK
from .network import NetworkDevices, ipv4
from .paths import UserInputError, read_json
from .builds import FirmwareBuilds, PROFILE, CM55_BUILD_PROFILE, image_bytes
from .serial_targets import SerialTargets, CM55_PROFILE
from .detached import start_detached
from .validation_guard import check_validation_access

PROJECT = Path(__file__).resolve().parents[2]
FIRMWARE_SHA = "cc039bf224607f71e1a8a2ab5358fa8e17c8cc4230ccb99a91640a9fa8301a07"
UPDATER_SHA = "3cda061d737adee08e582f3f28f323e7a283312dc1a6b84534813a2d4a321334"
TARGET_MAC = os.environ.get("BONENG_SERIAL_MAC", "").strip().upper() or None
_JOB_LOCK = threading.Lock()
_CHILDREN: dict[str, subprocess.Popen] = {}


def _id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise UserInputError("Use an ID returned by the firmware MCP tools.")
    return value


def _sha(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    if path.stat().st_size > 4_000_000:
        raise UserInputError("Firmware journal exceeds the size limit.")
    rows = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # A running process may have only partly flushed its last line.
        if isinstance(row, dict) and isinstance(row.get("data"), dict):
            rows.append(row)
    return rows


class FirmwareUpgrades:
    def __init__(self, release: Path, workspace: Path, data: Path, network: NetworkDevices | None = None):
        self.updater = release / "Downloadconfig" / "Downloadconfig.exe"
        self.bridge = PROJECT / "workspace" / "reuse_bridge" / "ReuseFirmwareUpgrade.exe"
        self.root = workspace / "firmware"
        self.historical = data / "firmware_upgrade_result.json"
        self.network = network
        self.builds = FirmwareBuilds(workspace)
        self.serial_targets = SerialTargets(workspace)

    def describe(self) -> dict[str, Any]:
        return {"configured": sys.platform == "win32" and self.updater.is_file() and self.bridge.is_file(),
                "backend": "original_Downloadconfig_F2_F1_CM25_F3", "transports": ["serial", "selected_tcp"], "serial_port": "COM7",
                "serial_target_mac": TARGET_MAC, "firmware_sha256": FIRMWARE_SHA, "release": "9.10 / 0x6908",
                "other_firmware_supported": True, "registered_build_profile": PROFILE, "arbitrary_platform_images_supported": False,
                "registered_build_profiles": [PROFILE, CM55_BUILD_PROFILE],
                "cm55_serial": {"requires_explicit_serial_selection": True, "requires_registered_build": True,
                                "profile": CM55_BUILD_PROFILE, "transfer_chunk_bytes": 120,
                                "physical_unique_identity_verified": False, "flash_physical_bench": False,
                                "status": "source and software contracts checked; actual CM55 flashing/boot acceptance pending"},
                "tcp_upgrade_supported": True, "tcp_device_selection_required": True,
                "tcp_scope": "selected AN_PN platform 9, software 9.10, stopped states 1/4/5 and run bit clear; identity rechecked before F2",
                "tcp_bench_scope": "physical TCP flash and boot verification passed on a selected AN bench device; records are local and not distributed",
                "manual_power_cycle_required": True, "automatic_restart_supported": False,
                "workflow": ["preflight_firmware_upgrade", "start_firmware_upgrade", "get_firmware_upgrade_status", "verify_firmware_upgrade_boot"],
                "preflight_starts_upgrade": False}

    def _files(self, firmware_path: str, port: str, target: dict[str, Any] | None = None, build_id: str = "") -> dict[str, Any]:
        build = self.builds.get(build_id) if build_id else None
        serial_target = self.serial_targets.get(port) if target is None else None
        cm55 = serial_target is not None
        if cm55 and (not build or build["profile"] != CM55_BUILD_PROFILE):
            raise UserInputError("Selected CM55 requires a registered cm55_ec_spi_612 build; AN images cannot be used.")
        if build and build["profile"] == CM55_BUILD_PROFILE and not cm55:
            raise UserInputError("CM55 firmware requires an explicitly selected CM55 serial endpoint.")
        if target is None and not cm55 and (not TARGET_MAC or not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", TARGET_MAC)):
            raise UserInputError("Set BONENG_SERIAL_MAC to the user-verified AN serial device MAC before preflight, or explicitly select a CM55 serial endpoint.")
        if build:
            if firmware_path and Path(firmware_path).resolve() != Path(build["firmware_path"]):
                raise UserInputError("Use the registered sealed firmware path, or empty firmware_path with build_id.")
            firmware_path = build["firmware_path"]
        if target is None and not cm55 and port != "COM7":
            raise UserInputError("The AN serial upgrade profile supports COM7 and the explicitly configured BONENG_SERIAL_MAC.")
        if target is not None and (port != "TCP" or not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", target.get("target_mac", ""))):
            raise UserInputError("Choose a discovered network device before TCP upgrade preflight.")
        if not isinstance(firmware_path, str) or "\x00" in firmware_path:
            raise UserInputError("Supply the absolute path to the verified .bin file.")
        image = Path(firmware_path)
        if not image.is_absolute() or image.suffix.lower() != ".bin" or not image.is_file():
            raise UserInputError("Supply the absolute path to an existing verified .bin file.")
        if not self.describe()["configured"]:
            raise UserInputError("Upgrade bridge unavailable; configure BONENG_RELEASE_DIR and run Build-ReuseBridge.ps1.")
        image_bytes(image, build["profile"] if build else PROFILE)
        expected_sha = build["firmware_sha256"] if build else FIRMWARE_SHA
        if _sha(image) != expected_sha:
            raise UserInputError("Firmware header/hash differs from the registered or inspected image.")
        if _sha(self.updater) != UPDATER_SHA:
            raise UserInputError("Vendor updater hash differs from the inspected build.")
        connection = {"transport": "serial", "target_mac": TARGET_MAC} if target is None else {
            "transport": "tcp", "target_mac": target["target_mac"], "host": ipv4(target["host"]),
            "interface_ip": ipv4(target["interface_ip"]), "scan_id": target["scan_id"], "device_id": target["device_id"], "tcp_port": 1000}
        files = {"firmware_path": str(image.resolve()), "firmware_sha256": expected_sha, "port": port,
                 **connection, "bridge_sha256": _sha(self.bridge), "updater_sha256": UPDATER_SHA}
        if cm55:
            files.update(target_mac=None, serial_binding_id=serial_target["serial_binding_id"],
                         expected_serial_identity=serial_target["expected_serial_identity"],
                         serial_target_path=str(self.serial_targets.path.resolve()), serial_target_sha256=_sha(self.serial_targets.path),
                         physical_unique_identity_verified=False)
        if build:
            contract = self.builds.root / build_id / "build.json"
            files.update(build_id=build_id, contract_path=str(contract.resolve()), contract_sha256=_sha(contract),
                         expected_software_raw=build["expected_software_raw"], expected_release_hex=build["expected_release_hex"],
                         expected_parameter_version_raw=build["expected_parameter_version_raw"], profile=build["profile"])
        return files

    def _selected_target(self) -> dict[str, Any]:
        selected = self.network.selection() if self.network is not None else {}
        if not selected.get("selected"):
            raise UserInputError("Scan devices and ask the user to select one before TCP upgrade preflight.")
        return {"target_mac": selected["device"]["mac"], "host": selected["device"]["ip"],
                "interface_ip": selected["interface_ip"], "scan_id": selected["scan_id"], "device_id": selected["device_id"]}

    def _command(self, files: dict[str, Any], journal: Path, mode: str) -> list[str]:
        command = [str(self.bridge), str(self.updater), files["firmware_path"], files["port"], str(journal), mode]
        if files.get("transport") == "tcp":
            command += ["--tcp", files["host"], files["target_mac"], files["interface_ip"]]
        if files.get("contract_path"):
            command += ["--contract", files["contract_path"]]
        if files.get("serial_target_path"):
            command += ["--serial-target", files["serial_target_path"], files["serial_target_sha256"], files["serial_binding_id"]]
        elif files.get("transport") == "serial" and files.get("target_mac"):
            command += ["--serial-mac", files["target_mac"]]
        return command

    def _read_mode(self, files: dict[str, Any], journal: Path, mode: str, expected_status: str) -> list[dict[str, Any]]:
        if not _PROBE_LOCK.acquire(blocking=False):
            raise UserInputError("Another device operation is running.")
        try:
            try:
                completed = subprocess.run(self._command(files, journal, mode), stdin=subprocess.DEVNULL,
                                           capture_output=True, encoding="utf-8-sig", errors="replace", timeout=50,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise UserInputError("Firmware read-only check failed: " + str(error)) from error
            rows = _events(journal)
            if completed.returncode != 0 or not rows or rows[-1]["data"].get("status") != expected_status or rows[-1]["data"].get("device_write_commands_sent") is not False:
                error = rows[-1]["data"].get("error", "No verified read-only result") if rows else completed.stderr[-800:]
                raise UserInputError("Firmware read-only check failed: " + str(error))
            return rows
        finally:
            _PROBE_LOCK.release()

    def _running(self, job: dict[str, Any]) -> bool:
        child = _CHILDREN.get(job["job_id"])
        if child is not None:
            return child.poll() is None
        try:
            import psutil
            process = psutil.Process(job["pid"])
            arguments = process.cmdline()
            return abs(process.create_time() - job["process_created_at"]) < 2 and job["journal"] in arguments and "--execute" in arguments
        except (KeyError, OSError, psutil.Error):
            return False

    def _active(self) -> dict[str, Any] | None:
        path = self.root / "active.json"
        return read_json(path) if path.is_file() else None

    def _ensure_idle(self) -> None:
        active = self._active()
        if active:
            status = self.status(active["job_id"])
            if status["status"] not in {"failed", "flash_verified", "boot_verified"}:
                raise UserInputError("A firmware job is running or has an uncertain result. Query its status before another upgrade: " + active["job_id"])

    def preflight(self, firmware_path: str, transport: str, port: str = "COM7", build_id: str = "") -> dict[str, Any]:
        check_validation_access(self.root.parent)
        if transport not in {"serial", "selected_tcp"}:
            raise UserInputError("transport must be serial or selected_tcp.")
        target = self._selected_target() if transport == "selected_tcp" else None
        files = self._files(firmware_path, "TCP" if target else port, target, build_id)
        self._ensure_idle()
        plan_id = uuid.uuid4().hex
        folder = self.root / "plans"
        folder.mkdir(parents=True, exist_ok=True)
        rows = self._read_mode(files, folder / f"{plan_id}.jsonl", "--preflight", "preflight_passed")
        identity = next((row["data"] for row in rows if row["data"].get("phase") == "preflight_passed"), {})
        if identity.get("mac") != files["target_mac"] or identity.get("software_raw") != (612 if files.get("profile") == CM55_BUILD_PROFILE else 910) or identity.get("state_raw") not in {1, 4, 5} or identity.get("run_status_word", 0) & 4:
            raise UserInputError("Preflight device identity/state differs from the inspected target.")
        self._check_serial_result(files, identity)
        plan = {**files, "plan_id": plan_id, "created_at": time.time(), "expires_at": time.time() + 900,
                "identity": identity, "status": "preflight_passed", "device_write_commands_sent": False,
                "manual_power_cycle_required": True}
        with (folder / f"{plan_id}.json").open("x", encoding="utf-8") as target:
            json.dump(plan, target, ensure_ascii=False, indent=2)
        return plan

    def start(self, plan_id: str) -> dict[str, Any]:
        check_validation_access(self.root.parent)
        plan = read_json(self.root / "plans" / f"{_id(plan_id)}.json")
        if plan.get("plan_id") != plan_id or plan.get("status") != "preflight_passed" or time.time() > plan.get("expires_at", 0):
            raise UserInputError("Preflight plan is invalid or expired. Run preflight again.")
        target = self._selected_target() if plan.get("transport") == "tcp" else None
        files = self._files(plan["firmware_path"], plan["port"], target, plan.get("build_id", ""))
        if any(plan.get(key) != value for key, value in files.items()):
            raise UserInputError("Firmware/updater/bridge changed since preflight. Run preflight again.")
        if not _JOB_LOCK.acquire(blocking=False):
            raise UserInputError("Another firmware start is being processed.")
        start_lock = self.root / "start.lock"
        owns_start_lock = False
        try:
            try:
                with start_lock.open("x", encoding="utf-8") as target:
                    target.write(plan_id)
                owns_start_lock = True
            except FileExistsError as error:
                raise UserInputError("Another MCP server is preparing an upgrade; query the active job.") from error
            used = self.root / "plans" / f"{plan_id}.used"
            if used.exists():
                raise UserInputError("This preflight plan has already been used. It cannot start another flash.")
            self._ensure_idle()
            active = self.root / "active.json"
            if active.is_file():
                active.unlink()  # _ensure_idle confirmed the previous job has a terminal result.
            job_id = uuid.uuid4().hex
            folder = self.root / "jobs" / job_id
            folder.mkdir(parents=True)
            journal = folder / "journal.jsonl"
            job = {**files, "job_id": job_id, "plan_id": plan_id, "started_at": time.time(),
                   "journal": str(journal), "status": "starting", "manual_power_cycle_required": True}
            # Exclusive file creation prevents two MCP servers from claiming the same workspace.
            try:
                with active.open("x", encoding="utf-8") as target:
                    json.dump(job, target)
                with used.open("x", encoding="utf-8") as target:
                    target.write(job_id)
            except FileExistsError as error:
                raise UserInputError("This plan has already been used or another upgrade has started.") from error
            job_path = folder / "job.json"
            job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                launched = start_detached(self._command(files, journal, "--execute"), self.bridge.parent, folder)
                import psutil
                try:
                    created_at = psutil.Process(launched["pid"]).create_time()
                except psutil.Error:
                    created_at = job["started_at"]
                job.update(pid=launched["pid"], process_created_at=created_at, launcher=launched["launcher"])
            except (OSError, UserInputError, subprocess.TimeoutExpired) as error:
                job.update(status="failed", error=str(error))
            job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            return {**job, "automatic_restart_sent": False,
                    "next_action": "Poll get_firmware_upgrade_status; keep power and the selected USB/Ethernet link connected until Flash verification succeeds."}
        finally:
            if owns_start_lock:
                start_lock.unlink(missing_ok=True)
            _JOB_LOCK.release()

    def status(self, job_id: str = "") -> dict[str, Any]:
        if not job_id:
            active = self._active()
            if active:
                job_id = active["job_id"]
            elif self.historical.is_file():
                return {"status": "historical_verified_run", "source": str(self.historical), "historical_record": read_json(self.historical),
                        "hardware_contacted": False, "new_upgrade_started": False}
            else:
                return {"status": "no_firmware_jobs", "hardware_contacted": False}
        job = read_json(self.root / "jobs" / _id(job_id) / "job.json")
        rows = _events(Path(job["journal"]))
        payloads = [row["data"] for row in rows]
        terminal = next((row for row in reversed(payloads) if row.get("status") in {"failed", "flash_verified"}), None)
        if terminal and terminal.get("status") == "flash_verified":
            self._check_serial_result(job, terminal)
            if terminal.get("firmware_sha256") != job["firmware_sha256"] or terminal.get("mac") != job["target_mac"] or terminal.get("device_flash_readback_passed") is not True:
                raise UserInputError("Flash success result does not match the verified firmware/device contract.")
            state = "flash_verified"
        elif terminal or job.get("status") == "failed":
            state = "failed"
        else:
            state = "running" if self._running(job) else "result_unknown"
        boot_path = Path(job["journal"]).with_name("boot.jsonl")
        boot_rows = _events(boot_path)
        if state == "flash_verified" and boot_rows and boot_rows[-1]["data"].get("status") == "boot_verified":
            boot = boot_rows[-1]["data"]
            self._check_serial_result(job, boot)
            if boot.get("mac") != job["target_mac"] or boot.get("device_write_commands_sent") is not False or boot.get("upgrade_status_reset_to_zero") is not True:
                raise UserInputError("Boot verification contract mismatch.")
            if job.get("build_id") and (boot.get("software_raw") != job["expected_software_raw"] or boot.get("release_hex") != job["expected_release_hex"] or boot.get("firmware_sha256") != job["firmware_sha256"] or
                    (job.get("expected_parameter_version_raw") is not None and boot.get("parameter_version_raw") != job["expected_parameter_version_raw"])):
                raise UserInputError("Booted firmware identity differs from the registered build.")
            state = "boot_verified"
        progress = next((row for row in reversed(payloads) if row.get("phase") in {"transfer_progress", "transfer_finished"}), {})
        percent = min(99.0, 100 * progress.get("packet", 0) / max(1, progress.get("total", 1)))
        return {**job, "status": state, "latest_event": rows[-1] if rows else None, "transfer_percent": 100.0 if state in {"flash_verified", "boot_verified"} else percent,
                "flash_readback_verified": state in {"flash_verified", "boot_verified"}, "boot_verified": state == "boot_verified",
                "error": terminal.get("error") if terminal else job.get("error"), "hardware_contacted": False,
                "automatic_restart_sent": False, "requires_manual_power_cycle": state == "flash_verified"}

    def capture_profile(self, expected_mac: str, identity: dict[str, Any]) -> str | None:
        active = self._active()
        if not active:
            return None
        job = self.status(active["job_id"])
        if job.get("status") != "boot_verified" or not job.get("build_id") or job.get("target_mac") != expected_mac:
            return None
        boot = _events(Path(job["journal"]).with_name("boot.jsonl"))[-1]["data"]
        if job.get("profile") == CM55_BUILD_PROFILE:
            expected = {**job["expected_serial_identity"], "mac": None, "device_profile": CM55_PROFILE,
                        "serial_binding_id": job["serial_binding_id"], "software_raw": boot["software_raw"],
                        "release_hex": boot["release_hex"], "parameter_version_raw": boot["parameter_version_raw"]}
            return CM55_BUILD_PROFILE if identity == expected else None
        expected = {"mac": expected_mac, "platform_type": 9, "communication_type": 3 if job["transport"] == "tcp" else 0,
                    "software_raw": boot["software_raw"], "release_hex": boot["release_hex"], "parameter_version_raw": boot["parameter_version_raw"]}
        return PROFILE if identity == expected else None

    @staticmethod
    def _check_serial_result(files: dict[str, Any], result: dict[str, Any]) -> None:
        if files.get("profile") != CM55_BUILD_PROFILE:
            return
        if (result.get("serial_binding_id") != files.get("serial_binding_id")
                or result.get("serial_fingerprint") != files.get("expected_serial_identity")
                or result.get("physical_unique_identity_verified") is not False):
            raise UserInputError("Firmware serial binding/result contract mismatch.")

    def verify_boot(self, job_id: str) -> dict[str, Any]:
        check_validation_access(self.root.parent)
        state = self.status(_id(job_id))
        if state["status"] not in {"flash_verified", "boot_verified"}:
            raise UserInputError("Flash readback must be verified before checking the boot result.")
        files = self._files(state["firmware_path"], state["port"], state if state.get("transport") == "tcp" else None, state.get("build_id", ""))
        if any(state.get(key) != value for key, value in files.items()):
            raise UserInputError("Firmware/build contract changed since flashing; boot verification refused.")
        journal = Path(state["journal"]).with_name("boot.jsonl")
        self._read_mode(files, journal, "--verify-boot", "boot_verified")
        return self.status(job_id)
