"""CM55 product boundaries and vendor-backed operations on loopback only."""
from __future__ import annotations

import copy
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from boneng_mcp.builds import FirmwareBuilds, CM55_BUILD_PROFILE, image_bytes, sha256
from boneng_mcp.device import DeviceOperations
from boneng_mcp.firmware import FirmwareUpgrades, UPDATER_SHA
from boneng_mcp.paths import UserInputError
from boneng_mcp.serial_targets import SerialTargets, CM55_PROFILE
from test_device_bridge_simulation import PROJECT, BRIDGE, VENDOR, LoopbackDrive, frame

FINGERPRINT = {"platform_type": 3, "communication_type": 0, "board_number_raw": 309, "platform_raw": 10301}
TARGET = {"transport": "serial", "port": "COM7", "expected_mac": None, "device_profile": CM55_PROFILE,
          "serial_binding_id": "a"*32, "expected_serial_identity": FINGERPRINT}


class Cm55Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.firmware = FirmwareUpgrades(self.root, self.root, self.root)
        self.device = DeviceOperations(self.root, self.root, self.root, Mock(), self.firmware)
        SerialTargets(self.root).save(copy.deepcopy(TARGET))

    def build(self, size=1509460):
        path = self.root / "compiled.bin"
        path.write_bytes(struct.pack("<4I",0xEE3355BB,size-4,0x80000000,0x80000000)+bytes(size-16))
        return self.firmware.builds.register(str(path), "CM55 test", "6630",612,612,profile=CM55_BUILD_PROFILE)

    def test_cm55_sealed_artifact_and_profile_are_independent_of_an(self):
        build = self.build()
        self.assertEqual(build["platform_type"], 3)
        self.assertEqual(self.firmware.builds.get(build["build_id"]), build)
        with self.assertRaises(UserInputError): image_bytes(Path(build["firmware_path"]))
        with self.assertRaises(UserInputError): self.firmware.builds.register(build["firmware_path"],"wrong software","6630",910,profile=CM55_BUILD_PROFILE)
        Path(build["firmware_path"]).write_bytes(b"recompiled")
        with self.assertRaises(UserInputError): self.firmware.builds.get(build["build_id"])

    def test_raw_dsp_image_is_rejected(self):
        path = self.root / "dsp.bin"; path.write_bytes(bytes(1000))
        with self.assertRaises(UserInputError): image_bytes(path, CM55_BUILD_PROFILE)

    def test_serial_selection_has_no_fabricated_mac(self):
        self.assertEqual(self.device._target("serial"), TARGET)
        with self.assertRaisesRegex(UserInputError,"rpm"):
            self.device.plan_run(10,20,"Hz",1,"unloaded",True,"serial")

    def test_explicit_selection_uses_fresh_binding_and_reports_no_uid(self):
        def identity(request):
            return {"identity":{**FINGERPRINT,"software_raw":612,"serial_binding_id":request["serial_binding_id"]},"journal_dir":"fixture"}
        with patch.object(self.device,"_run",side_effect=identity):
            result=self.device.select_serial()
        self.assertFalse(result["physical_unique_identity_verified"])
        self.assertNotEqual(result["target"]["serial_binding_id"],TARGET["serial_binding_id"])
        self.assertEqual(self.device._target("serial"),result["target"])

    def test_failed_an_reselection_retains_cm55_binding(self):
        with patch.object(self.device,"_run",side_effect=UserInputError("MAC mismatch")), self.assertRaises(UserInputError):
            self.device.select_serial(profile="an_pn_910")
        self.assertEqual(self.device._target("serial"),TARGET)

    def test_reselection_invalidates_prepared_write(self):
        self.device.bridge = self.root / "bridge.exe"; self.device.bridge.write_bytes(b"test")
        plan=self.device._save_plan("parameter_write",{"transport":"serial","port":"COM7","target":copy.deepcopy(TARGET),"bridge_sha256":self.device._bridge_sha()})
        SerialTargets(self.root).save({**TARGET,"serial_binding_id":"b"*32})
        with patch.object(self.device,"_run") as run, self.assertRaisesRegex(UserInputError,"changed"):
            self.device.apply_write(plan["plan_id"])
        run.assert_not_called()

    def test_cm55_rejects_legacy_an_flash_and_unselected_cm55(self):
        with self.assertRaisesRegex(UserInputError,"CM55 requires"):
            self.firmware._files("anything.bin","COM7")
        build=self.build()
        SerialTargets(self.root).path.unlink()
        with self.assertRaisesRegex(UserInputError,"selected CM55"):
            self.firmware._files("","COM7",build_id=build["build_id"])

    def test_serial_firmware_result_cannot_match_by_null_mac_alone(self):
        files={"profile":CM55_BUILD_PROFILE,"serial_binding_id":"a"*32,"expected_serial_identity":FINGERPRINT}
        good={"mac":None,"serial_binding_id":"a"*32,"serial_fingerprint":FINGERPRINT,"physical_unique_identity_verified":False}
        self.firmware._check_serial_result(files,good)
        for change in [{"serial_binding_id":"b"*32},{"serial_fingerprint":{**FINGERPRINT,"board_number_raw":310}},{"physical_unique_identity_verified":True}]:
            with self.assertRaises(UserInputError): self.firmware._check_serial_result(files,{**good,**change})

    def test_upgrade_command_pins_cm55_selection_and_sealed_build(self):
        build=self.build()
        self.firmware.updater.parent.mkdir(parents=True,exist_ok=True)
        self.firmware.updater.write_bytes(b"vendor fixture")
        self.firmware.bridge=self.root/"upgrade.exe"; self.firmware.bridge.write_bytes(b"bridge fixture")
        with patch.object(self.firmware,"describe",return_value={"configured":True}), patch("boneng_mcp.firmware._sha",side_effect=lambda p: UPDATER_SHA if p==self.firmware.updater else sha256(p)):
            files=self.firmware._files("","COM7",build_id=build["build_id"])
        command=self.firmware._command(files,self.root/"journal","--preflight")
        self.assertIsNone(files["target_mac"])
        self.assertEqual(files["serial_binding_id"],TARGET["serial_binding_id"])
        self.assertEqual(files["profile"],CM55_BUILD_PROFILE)
        self.assertIn("--serial-target",command); self.assertNotIn("--tcp",command)
        self.assertIn(files["serial_target_sha256"],command)


class Cm55Drive(LoopbackDrive):
    def __init__(self):
        super().__init__(); self.parameter_version=612; self.release=0x6629; self.fault=False
    def word(self,group,offset):
        if (group,offset)==(0x22,0): return 612
        if (group,offset)==(0x22,10): return 309
        if (group,offset)==(0x22,11): return 10301
        if self.fault and (group,offset)==(0x20,0): return 3
        if self.fault and (group,offset)==(0xEC,7): return 8
        if group==0x45: raise AssertionError("CM55 must never read AN MAC registers")
        return super().word(group,offset)
    def response(self,request):
        if request[:3]==bytes.fromhex("AA55FA"):
            self.request_times.append((0,request.hex()))
            return frame(request[:3]+b"\x02"+struct.pack("<HH",3,0))
        response=super().response(request)
        if request[:5]==bytes.fromhex("CC55FCEC0B") and self.control&0x80: self.fault=False
        return response


@unittest.skipUnless(sys.platform=="win32" and BRIDGE.is_file() and VENDOR.is_file(),"Requires built vendor bridge and BONENG_RELEASE_DIR")
class Cm55BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.harness=PROJECT/"workspace/reuse_bridge/Cm55LoopbackHarness.exe"
        compiler=Path(os.environ["WINDIR"])/"Microsoft.NET/Framework/v4.0.30319/csc.exe"
        subprocess.run([str(compiler),"/nologo","/target:exe","/platform:x86","/reference:System.Web.Extensions.dll",f"/out:{cls.harness}",str(PROJECT/"tests/Cm55LoopbackHarness.cs")],check=True,capture_output=True)

    def invoke(self,drive,operation,**extra):
        with tempfile.TemporaryDirectory(dir=PROJECT/"workspace/test_artifacts") as temporary:
            path=Path(temporary)/"request.json"
            request={**copy.deepcopy(TARGET),"operation":operation,**extra}
            path.write_text(json.dumps(request),encoding="utf-8")
            firmware=operation=="firmware"
            bridge=BRIDGE.with_name("ReuseFirmwareUpgrade.exe") if firmware else BRIDGE
            vendor=VENDOR.parent/"Downloadconfig/Downloadconfig.exe" if firmware else VENDOR
            command=[str(self.harness),str(bridge),str(vendor),str(path),"firmware" if firmware else "device"]
            result=subprocess.run(command,cwd=BRIDGE.parent,capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
            rows=[json.loads(line) for line in result.stdout.decode("utf-8-sig").splitlines() if line.strip()]
            report=rows[-1]
            self.assertFalse(drive.errors,drive.errors)
            return result.returncode,report

    def test_identity_and_owner_attribute_do_not_read_an_mac(self):
        with Cm55Drive() as drive:
            code,row=self.invoke(drive,"read",parameters=[{"code":"P12.07","group":0xEC,"offset":7}])
            self.assertEqual(code,0,row)
            self.assertIsNone(row["result"]["mac"])
            self.assertFalse(row["result"]["physical_unique_identity_verified"])
            self.assertEqual(drive.writes,[])

    def test_changed_board_rejected_before_any_write(self):
        with Cm55Drive() as drive:
            code,row=self.invoke(drive,"write",expected_serial_identity={**FINGERPRINT,"board_number_raw":310})
            self.assertNotEqual(code,0,row); self.assertIn("fingerprint changed",row["error"])
            self.assertEqual(drive.writes,[])

    def test_write_and_readback_use_actual_width(self):
        with Cm55Drive() as drive:
            code,row=self.invoke(drive,"write",parameters=[{"code":"B04.00","group":0x34,"offset":0,"raw_value":1234,"expected_before":1000,"expected_attribute":1<<12}])
            self.assertEqual(code,0,row)
            self.assertEqual(row["result"]["result"]["parameters"][0]["raw_value"],1234)
            self.assertEqual(drive.writes,[(0x34,0,1234)])

    def test_scope_2ms_and_stop_ack(self):
        with Cm55Drive() as drive:
            code,row=self.invoke(drive,"capture",sample_divisor=1,points=50,timeout_ms=8000,
                                 parameters=[{"code":"P11.03","group":0xEB,"offset":3}])
            self.assertEqual(code,0,row)
            self.assertEqual(len(row["result"]["result"]["raw_samples"]),50)
            self.assertTrue(row["result"]["result"]["scope_stopped"])
            self.assertIsNone(drive.scope_started_at)

    def test_registered_new_cm55_build_can_capture_but_wrong_revision_cannot(self):
        for revision in [612,613]:
            with self.subTest(revision=revision), Cm55Drive() as drive:
                drive.release=0x6630
                identity={**FINGERPRINT,"mac":None,"device_profile":CM55_PROFILE,"serial_binding_id":TARGET["serial_binding_id"],"software_raw":612,"release_hex":"6630","parameter_version_raw":revision}
                code,row=self.invoke(drive,"capture",registered_capture_profile=CM55_BUILD_PROFILE,expected_identity=identity,
                                     sample_divisor=2,points=10,timeout_ms=8000,parameters=[{"code":"P11.03","group":0xEB,"offset":3}])
                self.assertEqual(code,0 if revision==612 else 1,row)
                self.assertEqual(drive.writes,[])
                if revision==613: self.assertFalse(any(hexframe.startswith("ee55") for _,hexframe in drive.request_times))

    def test_rpm_heartbeats_and_verified_stop(self):
        with Cm55Drive() as drive:
            code,row=self.invoke(drive,"run",speed_unit="rpm",speed_raw=30000,max_speed_raw=30000,duration_ms=350)
            self.assertEqual(code,0,row)
            self.assertTrue(row["result"]["result"]["stopped_run_bit_verified"])
            self.assertGreaterEqual(len(drive.heartbeat_times),3)
            self.assertIn((0xEC,14,30000),drive.writes)
            self.assertEqual(drive.control,0x8000)

    def test_fault_reset_pulses_ack_without_run_enable(self):
        with Cm55Drive() as drive:
            drive.fault=True
            code,row=self.invoke(drive,"reset_fault")
            self.assertEqual(code,0,row)
            self.assertTrue(row["result"]["result"]["fault_cleared_verified"])
            self.assertFalse(drive.fault)
            self.assertIn((0xEC,11,0x8080),drive.writes)
            self.assertFalse(any(g==0xEC and o==11 and v&0x401==0x401 for g,o,v in drive.writes))

    def test_new_build_cannot_run_with_capture_registration(self):
        with Cm55Drive() as drive:
            drive.release=0x6630
            code,row=self.invoke(drive,"run",registered_capture_profile=CM55_BUILD_PROFILE)
            self.assertNotEqual(code,0,row); self.assertEqual(drive.writes,[])

    def test_continuous_cm55_run_services_capture_on_heartbeat_connection(self):
        with tempfile.TemporaryDirectory() as temporary, Cm55Drive() as drive:
            folder=Path(temporary); commands=folder/"commands"; commands.mkdir()
            def queue():
                deadline=time.monotonic()+10
                while not drive.heartbeat_times and time.monotonic()<deadline: time.sleep(0.01)
                command={**copy.deepcopy(TARGET),"operation":"capture","run_job_id":"d"*32,
                         "sample_divisor":1,"points":100,"timeout_ms":8000,
                         "parameters":[{"code":"P11.03","group":0xEB,"offset":3}]}
                pending=commands/"capture.tmp"; pending.write_text(json.dumps(command),encoding="utf-8")
                pending.replace(commands/"capture.request.json")
            writer=threading.Thread(target=queue); writer.start()
            code,row=self.invoke(drive,"run_continuous",speed_unit="rpm",speed_raw=30000,max_speed_raw=30000,duration_ms=0,
                                 maximum_duration_ms=1200,stop_file=str(folder/"stop.request"),command_dir=str(commands),run_job_id="d"*32)
            writer.join(timeout=2)
            self.assertEqual(code,0,row)
            captured=json.loads((commands/"capture.result.json").read_text(encoding="utf-8"))
            self.assertEqual(captured["status"],"passed",captured)
            self.assertEqual(captured["identity"]["serial_binding_id"],TARGET["serial_binding_id"])
            self.assertTrue(captured["executed_on_run_connection"])
            self.assertEqual(captured["result"]["points"],100)
            gaps=[b-a for a,b in zip(drive.heartbeat_times,drive.heartbeat_times[1:])]
            self.assertLess(max(gaps),0.3)
            self.assertEqual(drive.control,0x8000)

    def test_firmware_preflight_fingerprint_uses_cm55_fields(self):
        with Cm55Drive() as drive:
            code,row=self.invoke(drive,"firmware")
            self.assertEqual(code,0,row); self.assertEqual(row["result"],FINGERPRINT)
            self.assertEqual(drive.writes,[])


if __name__=="__main__": unittest.main()
