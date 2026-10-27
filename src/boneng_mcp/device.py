"""Live device operations with identity checks and reviewable, single-use writes."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import subprocess
import sys
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .firmware import FirmwareUpgrades, TARGET_MAC
from .network import NetworkDevices
from .original_probe import EXPECTED_RELEASE_SHA256, _PROBE_LOCK
from .paths import UserInputError, read_json
from .detached import start_detached
from .validation_guard import check_validation_access
from .serial_targets import SerialTargets, CM55_PROFILE, FINGERPRINT_KEYS
from .motion import decimal_number, duration_ms, speed_encoding, position_pulses

PROJECT = Path(__file__).resolve().parents[2]
_RUN_START_LOCK = threading.Lock()


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise UserInputError("Use the plan ID returned by this tool.")
    return value


class DeviceOperations:
    def __init__(self, release: Path, workspace: Path, data: Path, network: NetworkDevices, firmware: FirmwareUpgrades):
        self.vendor = release / "BonengDrivesoft.exe"
        self.bridge = PROJECT / "workspace" / "reuse_bridge" / "ReuseDeviceBridge.exe"
        self.workspace, self.root, self.network, self.firmware = workspace, workspace / "device", network, firmware
        self.serial_targets = SerialTargets(workspace)
        self.motion_session_id = uuid.uuid4().hex

    def select_serial(self, port: str = "COM7", profile: str = CM55_PROFILE) -> dict[str, Any]:
        if not isinstance(port, str) or not re.fullmatch(r"COM[1-9][0-9]{0,3}", port):
            raise UserInputError("Supply a Windows serial port such as COM7.")
        if self._active_run() and self.run_status()["status"] in {"starting", "running", "stalled", "result_unknown"}:
            raise UserInputError("Stop the current background run before changing serial selection.")
        if profile not in {CM55_PROFILE, "an_pn_910"}:
            raise UserInputError("Choose cm55_ec_612 or the previously identified an_pn_910 serial profile.")
        if profile == "an_pn_910":
            if port != "COM7":
                raise UserInputError("The legacy AN serial profile is pinned to COM7 and its recorded MAC.")
            if not TARGET_MAC or not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", TARGET_MAC):
                raise UserInputError("Set BONENG_SERIAL_MAC to the user-verified AN serial MAC before selecting this profile.")
            target = {"transport": "serial", "port": port, "expected_mac": TARGET_MAC}
            report = self._run({**target, "operation": "identity"})
            self.serial_targets.path.unlink(missing_ok=True)
            return {"selected": True, "target": target, "identity": report["identity"], "hardware_contacted": True,
                    "device_write_commands_sent": False, "physical_unique_identity_verified": True,
                    "journal_dir": report["journal_dir"]}
        target = {"transport": "serial", "port": port, "expected_mac": None,
                  "device_profile": CM55_PROFILE, "serial_binding_id": uuid.uuid4().hex}
        report = self._run({**target, "operation": "identity", "bind_serial": True})
        identity = report["identity"]
        if identity.get("platform_type") != 3 or identity.get("software_raw") != 612 or identity.get("serial_binding_id") != target["serial_binding_id"]:
            raise UserInputError("Serial selection requires the inspected CM55 EC 6.12 profile.")
        target["expected_serial_identity"] = {key: identity[key] for key in FINGERPRINT_KEYS}
        self.serial_targets.save(target)
        return {"selected": True, "target": target, "identity": identity, "hardware_contacted": True,
                "device_write_commands_sent": False, "physical_unique_identity_verified": False,
                "identity_limit": "User-selected serial endpoint and family/board fingerprint; cannot distinguish identical replacement hardware. Reselect after replacing the device.",
                "journal_dir": report["journal_dir"]}

    def describe(self) -> dict[str, Any]:
        return {"configured": sys.platform == "win32" and self.vendor.is_file() and self.bridge.is_file(),
                "backend": "original EntityModbus / ByteUtil / CCommModbus; bounded adapter", "builds": ["AN 9.10/6908", "AN 9.10/6A22", "CM55 EC 6.12/6629 (serial, source/simulation verified)"],
                "transports": ["selected_tcp", "serial"], "maximum_read_parameters": 20,
                "parameter_metadata": "online B6/B4/B5 group discovery, B1 attributes/B3 names; B2 limits/C2 values on demand; no bnproj dependency",
                "catalog_identity": "A/A1 parameter revision/platform/transport plus MAC/software/release; checked before cache use and inside operations",
                "identity_adapter_scope": "AN platform 9 uses MAC. CM55 EC platform 3 requires select_serial_device and serial binding/fingerprint; no physical unique identity. Writes/control require inspected builds; registered boot-verified builds can capture.",
                "cm55": {"profile": CM55_PROFILE, "transport": "serial", "speed_units": ["rpm"], "physical_unique_identity_verified": False,
                         "read_only_physical_bench": "COM7 6.12/6629; online definitions and 16/32-bit values",
                         "physical_bench": {"rpm_300_5s_run_and_stop": "passed with fan warning 50/1",
                            "rpm_500_1000_2000_short_runs": "observed; fan warning 50/1 remained", "online_speed_change_1000_to_2000": "observed on the same worker; stopped and zero speed verified",
                            "position_control": False, "rated_3000rpm_or_long_duration": False, "parameter_write": False, "scope": False, "fault_reset": False, "firmware_upgrade": False},
                         "parameter_codes": "live online catalog; do not borrow AN code meanings", "reselect_after_device_replacement": True},
                "write_policy": "one parameter per one-use preview; stopped states 1/4/5; no automatic retry",
                "write_physical_bench": "B05.01 2→0 via selected TCP AN, live C3 acknowledgement and readback verified; other parameters not individually bench-tested",
                "scope": {"channels": 4, "width_bits": 16, "sample_period_ms": "2 * integer divisor", "trigger": "immediate",
                          "maximum_duration_s": 40, "maximum_points": 20000, "overflow_or_dropped_samples_detectable": False,
                          "physical_bench": "selected TCP AN / 6908, P11.03–06, 2ms, 1000 points; all raw values zero"},
                "run_control": {"speed_units": ["Hz", "rpm"], "resolution": "session encoding dependent; exact effective speed shown in preview", "maximum_bounded_command_duration_s": 2147483.647,
                                "continuous_run_supported": True, "continuous_requires_background_worker": True,
                                "speed_limit": "live active motor rated/max speed and user limit", "duration_default": "continuous; confirm user intent before starting",
                                "online_speed_adjustment": True, "position_control": ["relative", "absolute_with_explicit_reference"],
                                "motion_extensions_physical_bench": {"cm55_612_6629": "short 500/1000/2000rpm runs and same-worker 1000-to-2000rpm adjustment observed; final stopped/zero/owner released; fan warning50/1 remained",
                                    "position_control": False, "full_rated_speed_or_long_duration": False, "other_builds_loads_or_configurations": False},
                                "worker_launcher": "hidden Win32_Process; survives temporary MCP job closure",
                                "requires_user_motion_limits": True, "stop_uses_run_status_bit": True,
                                "motion_physical_bench": "selected AN 6908, B05.01=0, commanded +300rpm, feedback around 300rpm; heartbeat survived MCP client closure",
                                "position_mode_restore_physical_bench": "passed: stopped from +300rpm, actual feedback 0rpm, S5, B05.01 restored to 2, worker exited; data/drive_stop_verification.json"},
                "active_run_limit": "continuous worker queues metadata/reads/snapshots/scope on its connection; generic parameter writes still require stopping",
                "run_connection_queue": {"heartbeat_priority": True, "software_stop_preempts_capture": True, "running_response_deadline_ms": 200},
                "channel_source": "selected device online catalog", "channel_names_reference_only": False}

    @staticmethod
    def _validate_codes(codes: list[str]) -> None:
        if not isinstance(codes, list) or not 1 <= len(codes) <= 20 or not all(isinstance(c, str) for c in codes) or len(set(codes)) != len(codes):
            raise UserInputError("Provide 1–20 distinct function codes.")
        if any(not re.fullmatch(r"[A-GP](?:0[0-9]|1[0-5])\.[0-9]{2}", code) for code in codes):
            raise UserInputError("Use a protocol function code such as B05.01 or P13.45.")

    @staticmethod
    def _digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _catalog(self, transport: str = "selected_tcp", port: str = "COM7", groups: list[str] | None = None, refresh: bool = False) -> dict[str, Any]:
        target = self._target(transport, port)
        identity = self._run({**target, "operation": "identity"})["identity"]
        folder = self.root / "catalogs"
        path = folder / (self._digest(target) + ".json")
        catalog = read_json(path) if path.is_file() else None
        if catalog and (catalog.get("identity") != identity or catalog.get("target") != target or catalog.get("schema_version") != 1):
            catalog = None
        if refresh or catalog is None or (groups is None and not catalog["complete"]) or (groups and not set(groups).issubset(catalog["loaded_groups"])):
            group_filter = groups[0] if groups and len(groups) == 1 else ""
            report = self._run({**target, "operation": "catalog", "group_filter": group_filter, "expected_identity": identity})
            if report["identity"] != identity or self._target(transport, port) != target:
                raise UserInputError("Device identity or selection changed during catalog discovery.")
            result = report["result"]
            if result.get("definitions_complete_for_scope") is not True:
                raise UserInputError("Device catalog is incomplete; no cached fallback is used.")
            loaded = [group_filter] if group_filter else [g["code"] for g in result["groups"]]
            parameters = result["parameters"]
            group_times = {code: time.time() for code in loaded}
            if group_filter and catalog:
                if catalog["groups"] != result["groups"] or catalog["major_groups"] != result["major_groups"]:
                    raise UserInputError("Device group structure changed without a version change; refresh the complete catalog.")
                parameters = [p for p in catalog["parameters"] if p["code"][:3] != group_filter] + parameters
                loaded = sorted(set(catalog["loaded_groups"]) | set(loaded))
                group_times = {**{code: catalog.get("group_captured_at", {}).get(code, catalog["captured_at"]) for code in catalog["loaded_groups"]}, **group_times}
            parameters = sorted(parameters, key=lambda p: (p["group"], p["offset"]))
            if len({p["code"] for p in parameters}) != len(parameters):
                raise UserInputError("Device returned ambiguous function codes.")
            catalog = {"schema_version": 1, "identity": identity, "target": target, "major_groups": result["major_groups"],
                       "groups": result["groups"], "parameters": parameters, "loaded_groups": loaded,
                       "complete": set(loaded) == {g["code"] for g in result["groups"]},
                       "captured_at": min(group_times.values()), "group_captured_at": group_times,
                       "origin": "live_device_B6_B4_B5_B1_B3", "values_are_live": False,
                       "enumeration_text_available": False, "journal_dir": report["journal_dir"]}
            catalog["catalog_id"] = self._digest({k: catalog[k] for k in ("identity", "major_groups", "groups", "parameters")})
            folder.mkdir(parents=True, exist_ok=True)
            temporary = folder / (uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return {**catalog, "catalog_file": str(path), "identity_verified_at": time.time()}

    @staticmethod
    def _catalog_summary(catalog: dict[str, Any]) -> dict[str, Any]:
        return {key: catalog[key] for key in ("catalog_id", "identity", "complete", "loaded_groups", "captured_at", "identity_verified_at", "catalog_file", "origin")}

    def discover_catalog(self, transport: str = "selected_tcp", port: str = "COM7", group: str = "") -> dict[str, Any]:
        if group and not re.fullmatch(r"[A-GP](?:0[0-9]|1[0-5])", group):
            raise UserInputError("Use a group such as B05/P11, or empty for the complete catalog.")
        catalog = self._catalog(transport, port, [group] if group else None, refresh=True)
        return {**self._catalog_summary(catalog), "major_groups": catalog["major_groups"], "groups": catalog["groups"],
                "parameter_words": len(catalog["parameters"]), "parameter_count": sum(not p["high_word_of"] for p in catalog["parameters"]),
                "enumeration_text_available": False, "hardware_contacted": True, "device_write_commands_sent": False}

    def search_device_parameters(self, query: str = "", group: str = "", limit: int = 50, offset: int = 0,
                                 transport: str = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        self._validate_search(query, group, limit, offset)
        catalog = self._catalog(transport, port, [group] if group else None)
        rows = [p for p in catalog["parameters"] if not p["high_word_of"] and (not group or p["code"].startswith(group + "."))
                and (not query or query.casefold() in (p["code"] + " " + p["name"] + " " + p["group_name"]).casefold())]
        return {**self._catalog_summary(catalog), "total": len(rows), "offset": offset, "limit": limit,
                "items": rows[offset:offset + limit], "next_offset": offset + limit if offset + limit < len(rows) else None,
                "device_write_commands_sent": False, "hardware_contacted": True, "current_values_included": False,
                "definition_cache_age_s": max(0, time.time() - catalog["captured_at"])}

    @staticmethod
    def _validate_search(query: str, group: str, limit: int, offset: int) -> None:
        if not isinstance(query, str) or len(query) > 100 or (group and not re.fullmatch(r"[A-GP](?:0[0-9]|1[0-5])", group)):
            raise UserInputError("Use a name/code and an optional protocol group such as B05 or P11.")
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 200 or offset < 0:
            raise UserInputError("limit must be 1–200 and offset nonnegative.")

    def _addresses(self, codes: list[str], catalog: dict[str, Any]) -> list[dict[str, Any]]:
        self._validate_codes(codes)
        definitions = {p["code"]: p for p in catalog["parameters"]}
        result = []
        for code in codes:
            if code not in definitions:
                raise UserInputError("Function code is not advertised by the selected device: " + code)
            p = definitions[code]
            if p["high_word_of"]:
                raise UserInputError("Use the first word of this 32-bit parameter: " + p["high_word_of"])
            result.append({"code": code, "group": p["group"], "offset": p["offset"]})
        return result

    def channels(self, query: str = "", group: str = "", limit: int = 50, offset: int = 0,
                 transport: str = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        self._validate_search(query, group, limit, offset)
        if group and not group.startswith("P"):
            raise UserInputError("Use a P00–P15 oscilloscope group.")
        catalog = self._catalog(transport, port, [group] if group else None)
        rows = [p for p in catalog["parameters"] if p["code"].startswith("P") and p["width_bits"] == 16
                and not p["high_word_of"] and not p["reserved"] and p["access"] != 3
                and (not group or p["code"].startswith(group + "."))
                and (not query or query.casefold() in (p["code"] + " " + p["name"]).casefold())]
        groups = sorted({p["group_name"] for p in rows})
        return {**self._catalog_summary(catalog), "total": len(rows), "offset": offset, "limit": limit, "channels": rows[offset:offset + limit], "groups": groups,
                "reference_only": False, "live_availability_checked_before_capture": True, "simultaneous_channels": 4,
                "hardware_contacted": True, "capture_started": False, "device_write_commands_sent": False,
                "definition_cache_age_s": max(0, time.time() - catalog["captured_at"]),
                "sample_period_ms": "2, 4, 6, ... (2ms task multiplied by E1 divisor)", "trigger_modes": ["immediate"]}

    def _target(self, transport: str, port: str = "COM7") -> dict[str, Any]:
        if transport == "selected_tcp":
            selected = self.network.selection()
            if not selected.get("selected"):
                raise UserInputError("Select a user-chosen scanned device first.")
            return {"transport": "tcp", "expected_mac": selected["device"]["mac"], "host": selected["device"]["ip"],
                    "interface_ip": selected["interface_ip"], "scan_id": selected["scan_id"], "device_id": selected["device_id"]}
        if transport == "serial":
            bound = self.serial_targets.get(port)
            if bound:
                return bound
            if port == "COM7" and TARGET_MAC and re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", TARGET_MAC):
                return {"transport": "serial", "port": port, "expected_mac": TARGET_MAC}
        raise UserInputError("Use selected_tcp, explicitly select a CM55 serial endpoint, or configure BONENG_SERIAL_MAC for the verified COM7 AN device.")

    def _run(self, request: dict[str, Any]) -> dict[str, Any]:
        check_validation_access(self.workspace)
        if not self.describe()["configured"]:
            raise UserInputError("Build the device bridge and configure the inspected original application first.")
        if request["operation"] == "capture":
            profile = self.firmware.capture_profile(request["expected_mac"], request.get("expected_identity", {}))
            if isinstance(profile, str):
                request["registered_capture_profile"] = profile
        if self._active_run():
            run = self.run_status()
            if run["status"] == "running" and run["worker_alive"] and request["operation"] in {"identity", "catalog", "read", "snapshot", "capture", "motion_limits", "position_state"}:
                return self._worker_request(request, run)
            if run["status"] in {"starting", "running", "stalled", "result_unknown"} and (run["worker_alive"] or request["operation"] not in {"identity", "catalog", "read"}):
                raise UserInputError("The background run owns the device connection; use get_drive_run_status or stop_drive_pc_control first.")
        active = self.firmware._active()
        if active and self.firmware.status(active["job_id"])["status"] not in {"failed", "flash_verified", "boot_verified"}:
            raise UserInputError("A firmware job is active or uncertain; inspect it before contacting the drive.")
        if not _PROBE_LOCK.acquire(blocking=False):
            raise UserInputError("Another device operation is running.")
        try:
            folder = self.root / "operations" / uuid.uuid4().hex
            folder.mkdir(parents=True)
            path = folder / "request.json"
            path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            try:
                completed = subprocess.run([str(self.bridge), str(self.vendor), str(path.resolve())], stdin=subprocess.DEVNULL,
                                           cwd=self.bridge.parent, capture_output=True, encoding="utf-8-sig", errors="replace", timeout=65,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise UserInputError("Device operation interrupted; inspect device state before repeating writes: " + str(error)) from error
            (folder / "stdout.jsonl").write_text(completed.stdout, encoding="utf-8")
            (folder / "stderr.log").write_text(completed.stderr, encoding="utf-8")
            result = None
            for line in completed.stdout.splitlines():
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and "status" in row:
                        result = row
                except ValueError:
                    pass
            if not result or completed.returncode or result.get("status") != "passed":
                raise UserInputError("Device operation failed; no automatic retry. " + str((result or {}).get("error", completed.stderr[-800:])) + " Journal: " + str(folder))
            if result.get("mac") != request["expected_mac"] or result.get("original_sha256") != EXPECTED_RELEASE_SHA256 or result.get("operation") != request["operation"]:
                raise UserInputError("Device bridge identity/result contract mismatch.")
            if request.get("device_profile") == CM55_PROFILE and (result.get("identity", {}).get("serial_binding_id") != request.get("serial_binding_id") or result.get("physical_unique_identity_verified") is not False):
                raise UserInputError("Serial binding/result contract mismatch.")
            if request["operation"] in {"identity", "catalog", "read", "snapshot", "motion_limits", "position_state"} and result.get("device_write_commands_sent") is not False:
                raise UserInputError("Read-only result contract mismatch.")
            if request.get("expected_identity") and result.get("identity") != request["expected_identity"]:
                raise UserInputError("Device/parameter identity differs from the online catalog.")
            if request["operation"] in {"read", "write"} and [p.get("code") for p in result["result"]["parameters"]] != [p["code"] for p in request["parameters"]]:
                raise UserInputError("Parameter response codes differ from the request.")
            result["journal_dir"] = str(folder)
            result["origin"] = "live_device_via_original_drivesoft"
            if request["operation"] == "read" and self._active_run():
                prior = self.run_status()
                values = {p["code"]: p for p in result["result"]["parameters"]}
                status = values.get("P12.07")
                if prior["status"] == "result_unknown" and not prior["worker_alive"] and result["mac"] == prior["target"]["expected_mac"] and status and not status["raw_value"] & 4:
                    job_path = self.root / "runs" / prior["job_id"] / "job.json"
                    job = read_json(job_path)
                    job.update(status="failed", error="Background worker exited unexpectedly; live read subsequently verified run bit cleared.",
                               recovery_read_journal=str(folder), recovery_read_at=time.time(), stopped_run_bit_verified=True)
                    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            # Raw protocol frames remain in the journal; avoid flooding the MCP context.
            result.pop("frames", None)
            return result
        finally:
            _PROBE_LOCK.release()

    def _worker_request(self, request: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        if run["target"] != {key: request[key] for key in run["target"]}:
            raise UserInputError("Active run belongs to a different selected device.")
        if run.get("queue_version") != 1:
            raise UserInputError("This older worker has no command queue; stop and create a fresh authorized run.")
        folder = self.root / "runs" / run["job_id"] / "commands"
        command_id = uuid.uuid4().hex
        path = folder / (command_id + ".request.json")
        result_path = folder / (command_id + ".result.json")
        queued = {**request, "run_job_id": run["job_id"]}
        temporary = folder / (command_id + ".tmp")
        temporary.write_text(json.dumps(queued, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        deadline = time.monotonic() + 60
        while not result_path.is_file():
            if time.monotonic() >= deadline:
                raise UserInputError("Queued device operation timed out; result unknown. Inspect the run and command journal before repeating. " + str(result_path))
            state = self.run_status(run["job_id"])
            if state["status"] in {"stopped", "failed", "result_unknown"}:
                # A stop can complete just after the worker publishes the result.
                if result_path.is_file():
                    break
                raise UserInputError("Run ended before the queued operation completed; no automatic retry. " + str(result_path))
            time.sleep(0.02)
        result = read_json(result_path)
        if result.get("status") != "passed":
            raise UserInputError("Queued operation " + str(result.get("status")) + ": " + str(result.get("error")) + ". Journal: " + str(result_path))
        if result.get("mac") != request["expected_mac"] or result.get("original_sha256") != EXPECTED_RELEASE_SHA256 or result.get("operation") != request["operation"] or result.get("run_job_id") != run["job_id"]:
            raise UserInputError("Queued device operation identity/result mismatch.")
        if request.get("expected_identity") and result.get("identity") != request["expected_identity"]:
            raise UserInputError("Queued device operation parameter identity mismatch.")
        if request.get("serial_binding_id") and result.get("identity", {}).get("serial_binding_id") != request["serial_binding_id"]:
            raise UserInputError("Queued serial binding/result mismatch.")
        if request["operation"] not in {"capture", "update_speed"} and result.get("device_write_commands_sent") is not False:
            raise UserInputError("Queued read-only result contract mismatch.")
        if request["operation"] == "update_speed" and result.get("device_write_commands_sent") is not True:
            raise UserInputError("Queued speed update has no verified command write.")
        if request["operation"] in {"read", "snapshot"} and [p["code"] for p in result["result"]["parameters"]] != [p["code"] for p in request["parameters"]]:
            raise UserInputError("Queued parameter response differs from requested codes.")
        result.update(journal_dir=str(folder), command_result_file=str(result_path), origin="live_device_via_original_drivesoft")
        return result

    def read(self, codes: list[str], transport: str = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        self._validate_codes(codes)
        catalog = self._catalog(transport, port, sorted({code[:3] for code in codes}))
        result = self._run({**catalog["target"], "operation": "read", "expected_identity": catalog["identity"], "parameters": self._addresses(codes, catalog)})
        result["catalog_id"] = catalog["catalog_id"]
        return result

    def runtime(self, transport: str = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        return self.read(["A00.00", "P12.05", "P12.06", "P12.07", "P12.08", "P12.11", "P12.14", "A00.03", "A00.04", "A00.06", "A00.08", "B05.01"], transport, port)

    def _save_plan(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        plan = {**data, "kind": kind, "plan_id": uuid.uuid4().hex, "expires_at": time.time() + 600, "device_write_commands_sent": False}
        folder = self.root / "plans"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (plan["plan_id"] + ".json")).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return plan

    def _consume(self, plan_id: str, kind: str) -> dict[str, Any]:
        plan = read_json(self.root / "plans" / (identifier(plan_id) + ".json"))
        if plan.get("kind") != kind or plan.get("plan_id") != plan_id or time.time() > plan.get("expires_at", 0):
            raise UserInputError("Plan expired or does not match this operation; make a fresh preview.")
        if self._target(plan["transport"], plan["port"]) != plan["target"]:
            raise UserInputError("Selected device changed since preview.")
        with self.bridge.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        if digest != plan["bridge_sha256"]:
            raise UserInputError("Bridge changed since preview; create a new plan.")
        try:
            with (self.root / "plans" / (plan_id + ".used")).open("x") as file:
                file.write(str(time.time()))
        except FileExistsError as error:
            raise UserInputError("Plan has already been used. Inspect its result before making another.") from error
        return plan

    def _bridge_sha(self) -> str:
        with self.bridge.open("rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()

    def prepare_write(self, code: str, value: str, transport: str = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        self._validate_codes([code])
        if code.startswith("P") or code in {"A04.31", "A04.32"}:
            raise UserInputError("Use dedicated control or restart tools for command registers.")
        report = self.read([code], transport, port)
        p = report["result"]["parameters"][0]
        if not p["name"].strip() or "预留" in p["name"] or "保留" in p["name"]:
            raise UserInputError("Reserved parameter has no usable live definition.")
        if p["access"] not in {0, 1}:
            raise UserInputError("This parameter is read-only or write-only; generic verified write is unavailable.")
        try:
            number = Decimal(value)
            scaled = number * (10 ** p["decimal_attribute_raw"])
            if not number.is_finite() or scaled != scaled.to_integral_value():
                raise ValueError("Value exceeds the parameter's decimal resolution.")
            numeric_raw = int(scaled)
        except (InvalidOperation, ValueError, TypeError) as error:
            raise UserInputError("Supply an exact finite decimal value in the displayed engineering unit.") from error
        width = p["width_bits"]
        low, high = (-(1 << (width - 1)), (1 << (width - 1)) - 1) if p["signed"] else (0, (1 << width) - 1)
        if not low <= numeric_raw <= high:
            raise UserInputError("Value exceeds this parameter's numeric width.")
        if not p["limit_is_reference"]:
            def decode(v):
                v &= (1 << width) - 1
                return v - (1 << width) if p["signed"] and v & (1 << (width - 1)) else v
            if not decode(p["lower_raw"]) <= numeric_raw <= decode(p["upper_raw"]):
                raise UserInputError("Value is outside the live parameter limits.")
        return self._save_plan("parameter_write", {"transport": transport, "port": port, "target": self._target(transport, port),
                               "bridge_sha256": self._bridge_sha(), "release_hex": report["release_hex"], "before": p,
                               "identity": report["identity"], "catalog_id": report["catalog_id"],
                               "requested_value": value, "raw_value": numeric_raw & ((1 << width) - 1),
                               "persistence": "C3/C6 may save EEPROM according to firmware attributes", "reboot_required": p["reboot_required"]})

    def apply_write(self, plan_id: str) -> dict[str, Any]:
        plan = self._consume(plan_id, "parameter_write")
        catalog = self._catalog(plan["transport"], plan["port"], [plan["before"]["code"][:3]])
        if catalog["identity"] != plan["identity"]:
            raise UserInputError("Device parameter version changed since preview; create a new preview.")
        address = self._addresses([plan["before"]["code"]], catalog)[0]
        if (address["group"], address["offset"]) != (plan["before"]["group"], plan["before"]["offset"]):
            raise UserInputError("Device address mapping changed since preview; create a new preview.")
        return self._run({**plan["target"], "operation": "write", "expected_release": plan["release_hex"], "expected_identity": plan["identity"], "parameters": [
            {**address, "expected_before": plan["before"]["raw_value"], "expected_attribute": plan["before"]["attribute_raw"],
             "expected_name": plan["before"]["name"], "expected_decimal": plan["before"]["decimal_attribute_raw"],
             "expected_lower": plan["before"]["lower_raw"], "expected_upper": plan["before"]["upper_raw"], "raw_value": plan["raw_value"]}]})

    def capture(self, codes: list[str], sample_period_ms: int, points: int, transport: str = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        self._validate_codes(codes)
        if len(codes) > 4 or any(not code.startswith("P") for code in codes):
            raise UserInputError("Select 1–4 P group channels.")
        if type(sample_period_ms) is not int or not 2 <= sample_period_ms <= 1000 or sample_period_ms % 2:
            raise UserInputError("Sampling period must be an even integer from 2 to 1000 ms.")
        if type(points) is not int or not 1 <= points <= 20000 or points * sample_period_ms > 40000:
            raise UserInputError("Capture at most 20000 points and 40 seconds.")
        catalog = self._catalog(transport, port, sorted({code[:3] for code in codes}))
        addresses = self._addresses(codes, catalog)
        result = self._run({**catalog["target"], "operation": "capture", "expected_identity": catalog["identity"], "parameters": addresses,
                            "sample_divisor": sample_period_ms // 2, "points": points,
                            "timeout_ms": min(50000, points * sample_period_ms + 8000)})
        capture = result["result"]
        rows = capture.pop("raw_samples")
        if len(rows) != points or capture.get("scope_stopped") is not True:
            raise UserInputError("Capture is incomplete or scope stop was not acknowledged.")
        folder = self.workspace / "waveforms"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("capture_" + uuid.uuid4().hex + ".csv")
        raw_path = path.with_suffix(".raw.csv")
        definitions = capture["definitions"]
        with path.open("x", newline="", encoding="utf-8") as output, raw_path.open("x", newline="", encoding="utf-8") as raw_output:
            writer, raw_writer = csv.writer(output), csv.writer(raw_output)
            writer.writerow(["time_s", *codes]); raw_writer.writerow(["time_s", *codes])
            for index, row in enumerate(rows):
                if len(row) != len(codes) or any(type(v) is not int or not 0 <= v <= 65535 for v in row):
                    raise UserInputError("Malformed capture samples.")
                values = [(raw - 65536 if p["signed"] and raw >= 32768 else raw) / 10 ** p["decimal_attribute_raw"] for raw, p in zip(row, definitions)]
                writer.writerow([index * sample_period_ms / 1000, *values]); raw_writer.writerow([index * sample_period_ms / 1000, *row])
        result.update(csv_file=str(path), raw_csv_file=str(raw_path), sample_period_ms=sample_period_ms,
                      time_axis="nominal firmware sampling period; no device timestamps or continuity guarantee")
        path.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def motion_limits(self, transport: str = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        return self._run({**self._target(transport, port), "operation": "motion_limits"})

    def plan_run(self, speed: float, maximum_abs_speed: float | None = None, speed_unit: str = "rpm", duration_s: float | None = None, motor_load_description: str = "",
                 local_stop_available: bool = False, transport: str = "selected_tcp", port: str = "COM7", restore_control_mode: int | None = None,
                 background: bool = False, allow_direction_change: bool = False) -> dict[str, Any]:
        if self._target(transport, port).get("device_profile") == CM55_PROFILE and speed_unit != "rpm":
            raise UserInputError("CM55 run control currently supports rpm; Hz scaling requires separate validation.")
        requested = decimal_number(speed, "Speed")
        if speed_unit not in {"Hz", "rpm"} or requested == 0:
            raise UserInputError("Use rpm or Hz and a nonzero initial speed.")
        if maximum_abs_speed is not None and (decimal_number(maximum_abs_speed, "Maximum speed") <= 0 or abs(requested) > decimal_number(maximum_abs_speed, "Maximum speed")):
            raise UserInputError("Speed exceeds the authorized maximum.")
        duration_ms(duration_s)
        if type(local_stop_available) is not bool or not local_stop_available or not motor_load_description.strip():
            raise UserInputError("User must supply motor/load state and confirm a local stop is available.")
        if type(background) is not bool or type(allow_direction_change) is not bool:
            raise UserInputError("background and allow_direction_change must be booleans.")
        if restore_control_mode is not None and (type(restore_control_mode) is not int or restore_control_mode not in {0, 1, 2}):
            raise UserInputError("restore_control_mode must be an explicitly authorized original B05.01 value (0, 1 or 2).")
        report = self.runtime(transport, port)
        limits_report = self.motion_limits(transport, port)
        if limits_report["identity"] != report["identity"]:
            raise UserInputError("Device/firmware changed while reading motion limits.")
        limits = limits_report["result"]
        if maximum_abs_speed is None:
            maximum_abs_speed = min(limits["rated_rpm"], limits["motor_max_rpm"])
            if speed_unit == "Hz":
                maximum_abs_speed = float(decimal_number(maximum_abs_speed, "Maximum speed") / decimal_number(limits["base_rpm"], "Motor base") * decimal_number(limits["rated_frequency_raw"], "Frequency") / 100)
        encoding = speed_encoding(speed, maximum_abs_speed, speed_unit, limits)
        return self._save_plan("bounded_run", {"transport": transport, "port": port, "target": self._target(transport, port),
                               "bridge_sha256": self._bridge_sha(), "release_hex": report["release_hex"],
                               "speed": speed, "maximum_abs_speed": maximum_abs_speed, "speed_unit": speed_unit, "duration_s": duration_s,
                               "restore_control_mode": restore_control_mode,
                               "background": background or duration_s is None or duration_s > 20,
                               "allow_direction_change": allow_direction_change, "motion_limits": limits, "speed_encoding": encoding,
                               "motor_load_description": motor_load_description, "local_stop_available": local_stop_available,
                               "before": report, "physical_bench_scope": "selected AN 6908 +300rpm; selected CM55 612/6629 short +500/+1000/+2000rpm and online 1000-to-2000; warning50/1 remained; not full-range/load/duration acceptance",
                               "timing_limit": "command interval; communications and deceleration add stopping time",
                               "full_range_physical_acceptance": False})

    @staticmethod
    def _motion_fields(plan: dict[str, Any]) -> dict[str, Any]:
        result = {"speed_unit": plan["speed_unit"], "speed_raw": plan["speed_encoding"]["raw"], "max_speed_raw": plan["speed_encoding"]["maximum_raw"],
                  "speed_encoding": plan["speed_encoding"], "maximum_abs_speed": plan["maximum_abs_speed"], "expected_motion_limits": plan["motion_limits"],
                  "allow_direction_change": plan.get("allow_direction_change", False)}
        if plan.get("kind") == "position_move":
            result.update({key: plan[key] for key in ("position_before", "position_fingerprint", "start_position_pulses", "target_position_pulses",
                "displacement_pulses", "minimum_position_pulses", "maximum_position_pulses", "position_tolerance_pulses", "position_speed_rpm", "acceleration_ms")})
        return result

    def run(self, plan_id: str) -> dict[str, Any]:
        plan = self._consume(plan_id, "bounded_run")
        if plan["duration_s"] is None or plan.get("background"):
            return self._start_continuous(plan)
        restore = {} if plan.get("restore_control_mode") is None else {"restore_control_mode": plan["restore_control_mode"]}
        return self._run({**plan["target"], **restore, "operation": "run", "expected_release": plan["release_hex"], "expected_identity": plan["before"]["identity"],
                          **self._motion_fields(plan), "duration_ms": duration_ms(plan["duration_s"])})

    def _running_motion(self, job_id: str = "") -> dict[str, Any]:
        check_validation_access(self.workspace)
        run = self.run_status(job_id)
        if run.get("status") != "running" or not run.get("worker_alive") or run.get("motion_api_version") != 2:
            raise UserInputError("A running version-2 motion worker is required; inspect/start a fresh authorized run.")
        return run

    def prepare_speed_change(self, speed: float, job_id: str = "") -> dict[str, Any]:
        run = self._running_motion(job_id)
        if run.get("operation") == "position_move":
            raise UserInputError("Online speed changes are available for speed sessions; position JOG uses its prepared profile.")
        requested = decimal_number(speed, "Speed")
        if requested and not run["allow_direction_change"] and (requested > 0) != (run["initial_speed"] > 0):
            raise UserInputError("This run did not authorize changing direction.")
        encoding = speed_encoding(speed, run["maximum_abs_speed"], run["speed_unit"], run["motion_limits"], allow_zero=True, fixed_encoding=run["speed_encoding"])
        current = self._worker_request({**run["target"], "operation": "motion_state", "expected_identity": run["identity"]}, run)["result"]
        return self._save_plan("speed_change", {"transport": "selected_tcp" if run["target"]["transport"] == "tcp" else "serial", "port": run["target"].get("port", "COM7"),
            "target": run["target"], "bridge_sha256": self._bridge_sha(), "job_id": run["job_id"], "identity": run["identity"],
            "expected_control_revision": current["control_revision"], "before_speed": current["speed_setpoint"], "speed_encoding": encoding,
            "speed_unit": run["speed_unit"], "maximum_abs_speed": run["maximum_abs_speed"], "external_ramp_generated": False})

    def apply_speed_change(self, plan_id: str) -> dict[str, Any]:
        plan = self._consume(plan_id, "speed_change")
        run = self._running_motion(plan["job_id"])
        if run["target"] != plan["target"] or run["identity"] != plan["identity"]:
            raise UserInputError("Motion target/firmware changed since speed preview.")
        return self._worker_request({**plan["target"], "operation": "update_speed", "expected_identity": plan["identity"],
            "expected_control_revision": plan["expected_control_revision"], "speed_raw": plan["speed_encoding"]["raw"],
            "effective_speed": plan["speed_encoding"]["effective_speed"]}, run)

    def configure_position_reference(self, zero_method: str, minimum_position: float | None = None, maximum_position: float | None = None, reference_description: str = "",
                                     unit: str = "pulses", pulses_per_revolution: int | None = None,
                                     transport: str = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        if zero_method not in {"current_stopped_position", "existing_device_zero"} or not reference_description.strip():
            raise UserInputError("Specify the user's coordinate-zero method and how the zero/reference was established.")
        if pulses_per_revolution is not None and (type(pulses_per_revolution) is not int or not 0 < pulses_per_revolution <= 2_147_483_647):
            raise UserInputError("pulses_per_revolution must be a positive integer.")
        position_pulses(0, unit, pulses_per_revolution)
        if (minimum_position is None) != (maximum_position is None):
            raise UserInputError("Supply both software travel bounds, or omit both.")
        target = self._target(transport, port)
        report = self._run({**target, "operation": "position_state"})
        state = report["result"]
        if state["state_raw"] not in {1, 4, 5} or state["status_word"] & 4:
            raise UserInputError("Coordinate zero/reference can be established only while stopped.")
        origin = state["position_pulses"] if zero_method == "current_stopped_position" else 0
        low, high = (-2_147_483_648 - origin, 2_147_483_647 - origin) if minimum_position is None else (
            position_pulses(minimum_position, unit, pulses_per_revolution), position_pulses(maximum_position, unit, pulses_per_revolution))
        if low >= high:
            raise UserInputError("Minimum position must be below maximum position.")
        if not -2_147_483_648 <= origin + low <= state["position_pulses"] <= origin + high <= 2_147_483_647:
            raise UserInputError("Current feedback must lie inside the requested signed-32-bit travel range.")
        reference = {"reference_id": uuid.uuid4().hex, "mcp_session_id": self.motion_session_id, "target": target, "identity": report["identity"],
            "zero_method": zero_method, "zero_feedback_pulses": origin, "minimum_position_pulses": low, "maximum_position_pulses": high,
            "unit": unit, "pulses_per_revolution": pulses_per_revolution, "reference_description": reference_description.strip(),
            "software_travel_bounds_supplied": minimum_position is not None,
            "travel_bound_basis": "user-supplied software range" if minimum_position is not None else "signed 32-bit representation only; no mechanical travel limit configured",
            "position_fingerprint": state["position_fingerprint"], "created_at": time.time(), "physical_zero_verified": False,
            "validity": "this MCP process/session only; re-establish after driver power-cycle, reset, rehoming, gearing/direction change or hardware replacement",
            "hardware_contacted": True, "device_write_commands_sent": False, "homing_command_sent": False}
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "position_reference.json").write_text(json.dumps(reference, ensure_ascii=False, indent=2), encoding="utf-8")
        return reference

    def _position_reference(self, target: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        reference = read_json(self.root / "position_reference.json")
        if reference.get("mcp_session_id") != self.motion_session_id or reference["target"] != target or reference["identity"] != report["identity"] or reference["position_fingerprint"] != report["result"]["position_fingerprint"]:
            raise UserInputError("Position reference expired or device/gearing/direction changed; establish the coordinate reference again.")
        return reference

    def position_status(self, transport: str = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        target = self._target(transport, port)
        report = self._run({**target, "operation": "position_state"})
        try:
            reference = self._position_reference(target, report)
        except UserInputError as error:
            return {**report, "coordinate_reference_valid": False, "coordinate_error": str(error)}
        return {**report, "coordinate_reference_valid": True, "reference": reference,
                "position_from_zero_pulses": report["result"]["position_pulses"] - reference["zero_feedback_pulses"]}

    def plan_position_move(self, position: float, maximum_speed_rpm: float, motor_load_description: str, local_stop_available: bool,
                           mode: str = "relative", unit: str = "pulses", acceleration_ms: int = 500, position_tolerance_pulses: int = 1,
                           timeout_s: float | None = None, allow_mode_change: bool = False, transport: str = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        if mode not in {"relative", "absolute"} or type(local_stop_available) is not bool or not local_stop_available or not motor_load_description.strip():
            raise UserInputError("Specify relative/absolute motion, motor/load conditions and a local stop.")
        if type(acceleration_ms) is not int or not 0 <= acceleration_ms <= 65535 or type(position_tolerance_pulses) is not int or not 0 <= position_tolerance_pulses <= 2_147_483_647 or type(allow_mode_change) is not bool:
            raise UserInputError("Use 0–65535ms acceleration and a nonnegative integer pulse tolerance.")
        duration_ms(timeout_s)
        target = self._target(transport, port)
        report = self._run({**target, "operation": "position_state"})
        state = report["result"]
        if state["state_raw"] not in {4, 5} or state["status_word"] & 0x204:
            raise UserInputError("Position motion requires a stopped, unowned drive.")
        reference = self._position_reference(target, report)
        requested = position_pulses(position, unit, reference["pulses_per_revolution"])
        start = state["position_pulses"]
        end = start + requested if mode == "relative" else reference["zero_feedback_pulses"] + requested
        delta = end - start
        low, high = (reference["zero_feedback_pulses"] + reference[key] for key in ("minimum_position_pulses", "maximum_position_pulses"))
        if delta == 0 or not -2_147_483_648 <= delta <= 2_147_483_647 or not low <= start <= high or not low <= end <= high:
            raise UserInputError("Move is zero, overflows signed command pulses or exceeds the coordinate travel limits.")
        speed = decimal_number(maximum_speed_rpm, "Position speed")
        if speed <= 0 or speed != speed.to_integral_value():
            raise UserInputError("Position JOG maximum speed must be a positive whole number of rpm.")
        limits = state["motor_limits"]
        encoding = speed_encoding(float(speed), float(speed), "rpm", limits)
        desired = [2, 1, delta & 0xFFFFFFFF, int(speed), acceleration_ms]
        if state["settings"][0]["raw_value"] != 2 and not allow_mode_change:
            raise UserInputError("Position mode is required. Explicitly allow the previewed temporary mode change, or set position mode first.")
        changes = []
        for definition, value in zip(state["settings"], desired):
            if definition["access"] not in {0, 1}:
                raise UserInputError("A required position JOG setting is not writable.")
            width = definition["width_bits"]
            def decode(raw):
                raw &= (1 << width) - 1
                return raw - (1 << width) if definition["signed"] and raw & (1 << (width - 1)) else raw
            if not definition["limit_is_reference"] and not decode(definition["lower_raw"]) <= decode(value) <= decode(definition["upper_raw"]):
                raise UserInputError("Position profile exceeds live parameter limits: " + definition["name"])
            if definition["raw_value"] != value:
                changes.append({"name": definition["name"], "group": definition["group"], "offset": definition["offset"],
                    "before_raw": definition["raw_value"], "requested_raw": value, "restore_after_stop": True, "may_persist_to_eeprom": True})
        return self._save_plan("position_move", {"transport": transport, "port": port, "target": target, "bridge_sha256": self._bridge_sha(),
            "release_hex": report["identity"]["release_hex"], "before": report, "reference_id": reference["reference_id"], "motion_session_id": self.motion_session_id,
            "mode": mode, "requested_position": position, "position_unit": unit, "position_before": state, "position_fingerprint": state["position_fingerprint"],
            "start_position_pulses": start, "target_position_pulses": end, "displacement_pulses": delta, "minimum_position_pulses": low, "maximum_position_pulses": high,
            "position_tolerance_pulses": position_tolerance_pulses, "position_speed_rpm": float(speed), "acceleration_ms": acceleration_ms,
            "speed": float(speed), "maximum_abs_speed": float(speed), "speed_unit": "rpm", "speed_encoding": encoding, "motion_limits": limits,
            "duration_s": timeout_s, "background": True, "parameter_changes": changes, "motor_load_description": motor_load_description,
            "local_stop_available": True, "completion": "target feedback within pulse tolerance, command complete, in-position, speed near zero; then OFF1/release and restore settings",
            "physical_zero_verified": False, "motion_extensions_physical_bench": False})

    def execute_position_move(self, plan_id: str) -> dict[str, Any]:
        plan = self._consume(plan_id, "position_move")
        reference = read_json(self.root / "position_reference.json")
        if reference["reference_id"] != plan["reference_id"] or plan["motion_session_id"] != self.motion_session_id:
            raise UserInputError("Position coordinate reference changed since preview.")
        return self._start_continuous(plan)

    def stop(self, transport: str = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        if self._active_run():
            status = self.run_status()
            if status["status"] in {"starting", "running", "stalled", "result_unknown"}:
                if status["target"] != self._target(transport, port):
                    raise UserInputError("Active run belongs to a different target; use its recorded transport/selection.")
                Path(status["stop_file"]).touch(exist_ok=True)
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    state = self.run_status(status["job_id"])
                    if state["status"] in {"stopped", "failed", "result_unknown"}:
                        return state
                    time.sleep(0.2)
                return {**self.run_status(status["job_id"]), "stop_requested": True,
                        "warning": "Stop not yet confirmed. Use the local stop if motion continues."}
        return self._run({**self._target(transport, port), "operation": "stop"})

    def reset_fault(self, transport: str = "selected_tcp", port: str = "COM7") -> dict[str, Any]:
        result = self._run({**self._target(transport, port), "operation": "reset_fault"})
        (self.root / "position_reference.json").unlink(missing_ok=True)
        return result

    def _active_run(self) -> dict[str, Any] | None:
        path = self.root / "active_run.json"
        return read_json(path) if path.is_file() else None

    def _start_continuous(self, plan: dict[str, Any]) -> dict[str, Any]:
        if not _RUN_START_LOCK.acquire(blocking=False):
            raise UserInputError("Another run start is being processed.")
        start_lock = self.root / "run_start.lock"
        owns_lock = False
        try:
            try:
                with start_lock.open("x") as file:
                    file.write(plan["plan_id"])
                owns_lock = True
            except FileExistsError as error:
                raise UserInputError("Another MCP process is preparing a run.") from error
            active = self._active_run()
            if active and self.run_status(active["job_id"])["status"] not in {"stopped", "failed"}:
                raise UserInputError("A run is already active or its stop is uncertain.")
            active_path = self.root / "active_run.json"
            if active:
                active_path.unlink()
            job_id = uuid.uuid4().hex
            folder = self.root / "runs" / job_id
            folder.mkdir(parents=True)
            stop_file, journal = folder / "stop.request", folder / "journal.jsonl"
            command_dir = folder / "commands"
            command_dir.mkdir()
            operation = "position_move" if plan["kind"] == "position_move" else "run_continuous"
            request = {**plan["target"], "operation": operation, "expected_release": plan["release_hex"], "expected_identity": plan["before"]["identity"],
                       **self._motion_fields(plan), "duration_ms": 0, "stop_file": str(stop_file), "run_journal": str(journal),
                       "command_dir": str(command_dir), "run_job_id": job_id}
            if plan["duration_s"] is not None:
                request["maximum_duration_ms"] = duration_ms(plan["duration_s"])
            if plan.get("restore_control_mode") is not None:
                request["restore_control_mode"] = plan["restore_control_mode"]
            request_path = folder / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            job = {"job_id": job_id, "plan_id": plan["plan_id"], "target": plan["target"], "speed": plan["speed"],
                   "speed_unit": plan["speed_unit"], "started_at": time.time(), "continuous": plan["duration_s"] is None, "duration_s": plan["duration_s"],
                   "stop_file": str(stop_file), "journal": str(journal), "status": "starting", "queue_version": 1}
            job.update(operation=operation, motion_api_version=2, identity=plan["before"]["identity"], initial_speed=plan["speed"],
                       speed_encoding=plan["speed_encoding"], maximum_abs_speed=plan["maximum_abs_speed"], motion_limits=plan["motion_limits"],
                       allow_direction_change=plan.get("allow_direction_change", False))
            job["restore_control_mode"] = plan.get("restore_control_mode")
            try:
                with active_path.open("x", encoding="utf-8") as file:
                    json.dump(job, file)
            except FileExistsError as error:
                raise UserInputError("Another MCP process is starting a run.") from error
            job_path = folder / "job.json"
            try:
                import psutil
                launched = start_detached([str(self.bridge), str(self.vendor), str(request_path)], self.bridge.parent, folder)
                try:
                    created_at = psutil.Process(launched["pid"]).create_time()
                except psutil.Error:
                    created_at = job["started_at"]
                job.update(pid=launched["pid"], process_created_at=created_at, launcher=launched["launcher"])
            except (OSError, UserInputError, subprocess.TimeoutExpired) as error:
                job.update(status="failed", error=str(error))
            job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            return {**job, "next_action": "Query get_drive_run_status; stop with stop_drive_pc_control. The background worker maintains heartbeat independently of this MCP session."}
        finally:
            if owns_lock:
                start_lock.unlink(missing_ok=True)
            _RUN_START_LOCK.release()

    def run_status(self, job_id: str = "") -> dict[str, Any]:
        if not job_id:
            active = self._active_run()
            if not active:
                return {"status": "no_run_jobs", "hardware_contacted": False}
            job_id = active["job_id"]
        job = read_json(self.root / "runs" / identifier(job_id) / "job.json")
        events = []
        path = Path(job["journal"])
        if path.is_file():
            with path.open("rb") as file:
                file.seek(max(0, path.stat().st_size - 100000))
                for line in file.read().decode("utf-8", errors="replace").splitlines():
                    try:
                        row = json.loads(line)
                        if isinstance(row, dict):
                            events.append(row)
                    except ValueError:
                        continue
        terminal = next((r for r in reversed(events) if r.get("status") in {"passed", "failed"}), None)
        progress = next((r for r in reversed(events) if r.get("phase") == "run_progress"), None)
        running = False
        if "pid" in job:
            import psutil
            try:
                process = psutil.Process(job["pid"])
                running = abs(process.create_time() - job["process_created_at"]) < 2 and str(path.with_name("request.json")) in process.cmdline()
            except (psutil.Error, OSError):
                pass
        if terminal and terminal.get("status") == "passed":
            if job["target"].get("serial_binding_id") and terminal.get("identity", {}).get("serial_binding_id") != job["target"]["serial_binding_id"]:
                raise UserInputError("Run terminal serial binding mismatch.")
            if terminal.get("mac") != job["target"]["expected_mac"] or terminal.get("operation") != job.get("operation", "run_continuous") or terminal.get("result", {}).get("stopped_run_bit_verified") is not True:
                raise UserInputError("Run terminal result contract mismatch.")
            state = "stopped"
        elif terminal or job["status"] == "failed":
            state = "failed"
        else:
            state = ("running" if progress else "starting") if running else "result_unknown"
        modified_at = path.stat().st_mtime if path.is_file() else None
        progress_age = max(0, time.time() - modified_at) if modified_at else None
        if state == "running" and progress_age is not None and progress_age > 5:
            state = "stalled"
        if terminal:
            terminal.pop("frames", None)
            if isinstance(terminal.get("result"), dict):
                terminal["result"].pop("states", None)
        speed_event = next((r for r in reversed(events) if r.get("phase") in {"speed_changed", "run_progress"}), None)
        position_event = next((r for r in reversed(events) if r.get("phase") == "position_progress"), None)
        outcome = None
        if terminal and terminal.get("status") == "passed":
            outcome = "position_reached" if terminal["result"].get("position_reached") else "position_not_reached" if job.get("operation") == "position_move" else "stopped"
        return {**job, "status": state, "latest_progress": progress, "terminal_result": terminal,
                "motion_outcome": outcome,
                "speed": speed_event.get("speed_setpoint", job["speed"]) if speed_event else job["speed"],
                "control_revision": speed_event.get("control_revision", 0) if speed_event else 0, "latest_position_progress": position_event,
                "worker_alive": running, "stop_requested": Path(job["stop_file"]).exists(), "hardware_contacted": False,
                "status_is_cached": True, "journal_modified_at": modified_at, "progress_age_s": progress_age,
                "warning": "Use local stop if communication is lost or stopping is unconfirmed." if state in {"failed", "stalled", "result_unknown"} else None}
