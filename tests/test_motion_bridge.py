"""Vendor packet motion regressions against loopback, never physical motors."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from test_device_bridge_simulation import BRIDGE, VENDOR, PROJECT, MAC, LoopbackDrive
from test_motion import LIMITS
from boneng_mcp.motion import speed_encoding


@unittest.skipUnless(sys.platform == "win32" and BRIDGE.is_file() and VENDOR.is_file(), "Requires built vendor bridge and BONENG_RELEASE_DIR")
class MotionBridgeTests(unittest.TestCase):
    def request(self, folder, operation, **extra):
        request = {"operation":operation,"transport":"tcp","host":"127.0.0.2","interface_ip":"127.0.0.1","expected_mac":MAC,**extra}
        path=folder/"request.json"; path.write_text(json.dumps(request),encoding="utf-8")
        return [str(BRIDGE),str(VENDOR),str(path)]

    def invoke(self, drive, operation, **extra):
        with tempfile.TemporaryDirectory(dir=PROJECT/"workspace/test_artifacts") as temporary:
            completed=subprocess.run(self.request(Path(temporary),operation,**extra),stdin=subprocess.DEVNULL,capture_output=True,cwd=BRIDGE.parent,timeout=25,creationflags=subprocess.CREATE_NO_WINDOW)
            rows=[json.loads(line) for line in completed.stdout.decode("utf-8-sig").splitlines() if line.strip()]
            self.assertFalse(drive.errors,drive.errors)
            return completed.returncode,rows[-1]

    @staticmethod
    def speed_request(speed=3000):
        encoding=speed_encoding(speed,3000,"rpm",LIMITS)
        return {"speed_raw":encoding["raw"],"max_speed_raw":encoding["maximum_raw"],"speed_encoding":encoding,"maximum_abs_speed":3000,
                "expected_motion_limits":LIMITS,"speed_unit":"rpm","duration_ms":350}

    def test_full_rated_speed_uses_correct_percentage_frame(self):
        with LoopbackDrive() as drive:
            code,result=self.invoke(drive,"run",**self.speed_request())
            self.assertEqual(code,0,result)
            self.assertEqual(result["result"]["speed_setpoint"],3000)
            self.assertIn((0xEC,14,10000),drive.writes)
            self.assertIn((0xEC,11,0x847F),drive.writes)
            self.assertEqual(drive.control,0x8000)

    def test_changed_motor_rating_blocks_all_writes(self):
        with LoopbackDrive() as drive:
            drive.values[(0x50,5)]=2500
            code,result=self.invoke(drive,"run",**self.speed_request())
            self.assertNotEqual(code,0,result); self.assertEqual(drive.writes,[])
            self.assertIn("Motor selection/rating changed",result["error"])

    def test_active_asynchronous_motor_group_uses_its_own_rating_and_speed_base(self):
        with LoopbackDrive() as drive:
            drive.values.update({(0xEC,8):512,(0x53,0):0,(0x53,4):5000,(0x53,5):1450,(0x53,6):1100,(0x53,7):1000,(0x53,9):2})
            code,result=self.invoke(drive,"motion_limits")
            self.assertEqual(code,0,result)
            self.assertEqual(result["result"]["rated_rpm"],1450)
            self.assertEqual(result["result"]["base_rpm"],1500)
            self.assertEqual(result["result"]["motor_max_rpm"],1650)
            self.assertEqual(result["result"]["effective_maximum_rpm"],1450)
            self.assertEqual(result["result"]["rating_parameter_code"],"D03.05")
            self.assertEqual(drive.writes,[])

    def test_read_motion_limits_and_position_state_have_no_writes(self):
        for operation in ("motion_limits","position_state"):
            with self.subTest(operation=operation),LoopbackDrive() as drive:
                code,result=self.invoke(drive,operation)
                self.assertEqual(code,0,result); self.assertFalse(result["device_write_commands_sent"])
                self.assertEqual(drive.writes,[])

    def test_priority_speed_updates_during_capture_and_stale_revision(self):
        with tempfile.TemporaryDirectory(dir=PROJECT/"workspace/test_artifacts") as temporary,LoopbackDrive() as drive:
            folder=Path(temporary); commands=folder/"commands"; commands.mkdir(); stop=folder/"stop.request"
            request=self.speed_request(300); request.update(duration_ms=0,maximum_duration_ms=60000,stop_file=str(stop),command_dir=str(commands),run_job_id="e"*32)
            process=subprocess.Popen(self.request(folder,"run_continuous",**request),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=BRIDGE.parent,creationflags=subprocess.CREATE_NO_WINDOW)
            events=[]; collector=threading.Thread(target=lambda:[events.append(json.loads(line.decode("utf-8"))) for line in process.stdout],daemon=True); collector.start()
            def wait_for(predicate,timeout=10):
                deadline=time.monotonic()+timeout
                while not predicate() and process.poll() is None and time.monotonic()<deadline: time.sleep(.01)
                self.assertTrue(predicate(),events)
            def queue(name,operation,**extra):
                pending=commands/(name+".tmp"); pending.write_text(json.dumps({"operation":operation,"run_job_id":"e"*32,"expected_mac":MAC,**extra}),encoding="utf-8")
                pending.replace(commands/(name+".request.json"))
                return commands/(name+".result.json")
            try:
                wait_for(lambda:any(e.get("phase")=="run_progress" for e in events))
                queue("capture","capture",sample_divisor=1,points=20000,timeout_ms=50000,parameters=[{"code":"P11.03","group":0xEB,"offset":3}])
                wait_for(lambda:drive.scope_started_at is not None)
                started=time.monotonic(); changed=queue("speed","update_speed",expected_control_revision=0,speed_raw=6000,effective_speed=1800)
                wait_for(changed.is_file,5)
                self.assertLess(time.monotonic()-started,3)
                response=json.loads(changed.read_text("utf-8")); self.assertEqual(response["status"],"passed",response)
                self.assertEqual(response["result"]["control_revision"],1)
                self.assertIsNotNone(drive.scope_started_at)
                stale=queue("stale","update_speed",expected_control_revision=0,speed_raw=10000,effective_speed=3000)
                wait_for(stale.is_file,5)
                self.assertEqual(json.loads(stale.read_text("utf-8"))["status"],"failed")
                self.assertNotIn((0xEC,14,10000),drive.writes)
                reverse=queue("reverse","update_speed",expected_control_revision=1,speed_raw=-1000,effective_speed=-300)
                wait_for(reverse.is_file,5)
                self.assertEqual(json.loads(reverse.read_text("utf-8"))["status"],"failed")
                zero=queue("zero","update_speed",expected_control_revision=1,speed_raw=0,effective_speed=0)
                wait_for(zero.is_file,5)
                self.assertEqual(json.loads(zero.read_text("utf-8"))["result"]["control_revision"],2)
                resume=queue("resume","update_speed",expected_control_revision=2,speed_raw=5000,effective_speed=1500)
                wait_for(resume.is_file,5)
                self.assertEqual(json.loads(resume.read_text("utf-8"))["result"]["control_revision"],3)
                stop.touch(); self.assertEqual(process.wait(timeout=15),0)
                collector.join(timeout=2)
                self.assertTrue(events[-1]["result"]["stopped_run_bit_verified"])
                self.assertIsNone(drive.scope_started_at)
                gaps=[b-a for a,b in zip(drive.heartbeat_times,drive.heartbeat_times[1:])]
                self.assertLess(max(gaps),.45)
            finally:
                if process.poll() is None: stop.touch(); process.wait(timeout=15)
                collector.join(timeout=2); process.stdout.close(); process.stderr.close()

    def position_request(self):
        with LoopbackDrive() as drive:
            code,before=self.invoke(drive,"position_state")
            self.assertEqual(code,0,before)
        result=self.speed_request(1200)
        result.update(duration_ms=0,maximum_duration_ms=3500,position_before=before["result"],position_fingerprint=before["result"]["position_fingerprint"],
            start_position_pulses=0,target_position_pulses=10000,displacement_pulses=10000,minimum_position_pulses=-20000,
            maximum_position_pulses=20000,position_tolerance_pulses=1,position_speed_rpm=1200,acceleration_ms=500)
        # Position speed is an explicit smaller session ceiling.
        encoding=speed_encoding(1200,1200,"rpm",LIMITS)
        result.update(speed_encoding=encoding,speed_raw=encoding["raw"],max_speed_raw=encoding["maximum_raw"],maximum_abs_speed=1200)
        return result

    def test_stop_prevents_speed_update_during_scope_cleanup(self):
        class HeldScopeDrive(LoopbackDrive):
            def __init__(self):
                super().__init__(); self.poll_entered=threading.Event(); self.release_poll=threading.Event()
            def response(self,request):
                if request[:4]==bytes.fromhex("EE55FB01") and not self.poll_entered.is_set():
                    self.poll_entered.set(); self.release_poll.wait(.15)
                return super().response(request)
        with tempfile.TemporaryDirectory(dir=PROJECT/"workspace/test_artifacts") as temporary,HeldScopeDrive() as drive:
            folder=Path(temporary); commands=folder/"commands"; commands.mkdir(); stop=folder/"stop.request"
            request=self.speed_request(300); request.update(duration_ms=0,maximum_duration_ms=10000,stop_file=str(stop),command_dir=str(commands),run_job_id="f"*32)
            process=subprocess.Popen(self.request(folder,"run_continuous",**request),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=BRIDGE.parent,creationflags=subprocess.CREATE_NO_WINDOW)
            events=[]; collector=threading.Thread(target=lambda:[events.append(json.loads(line.decode("utf-8"))) for line in process.stdout],daemon=True); collector.start()
            def queue(name,operation,**extra):
                pending=commands/(name+".tmp"); pending.write_text(json.dumps({"operation":operation,"run_job_id":"f"*32,"expected_mac":MAC,**extra}),encoding="utf-8")
                pending.replace(commands/(name+".request.json"))
            try:
                deadline=time.monotonic()+10
                while not any(e.get("phase")=="run_progress" for e in events) and process.poll() is None and time.monotonic()<deadline: time.sleep(.01)
                self.assertTrue(any(e.get("phase")=="run_progress" for e in events),events)
                queue("capture","capture",sample_divisor=1,points=20000,timeout_ms=50000,parameters=[{"code":"P11.03","group":0xEB,"offset":3}])
                self.assertTrue(drive.poll_entered.wait(5))
                queue("speed","update_speed",expected_control_revision=0,speed_raw=7777,effective_speed=2333.1)
                # Queue reader runs independently while the poll response is held.
                time.sleep(.03); stop.touch(); drive.release_poll.set()
                self.assertEqual(process.wait(timeout=15),0)
                self.assertNotIn((0xEC,14,7777),drive.writes)
                self.assertIsNone(drive.scope_started_at)
                collector.join(timeout=2)
                self.assertTrue(events[-1]["result"]["stopped_run_bit_verified"])
            finally:
                drive.release_poll.set()
                if process.poll() is None: stop.touch(); process.wait(timeout=15)
                collector.join(timeout=2); process.stdout.close(); process.stderr.close()

    def test_position_reaches_feedback_target_stops_and_restores_all_settings(self):
        request=self.position_request()
        with tempfile.TemporaryDirectory(dir=PROJECT/"workspace/test_artifacts") as temporary,LoopbackDrive() as drive:
            request["stop_file"]=str(Path(temporary)/"stop.request")
            code,result=self.invoke(drive,"position_move",**request)
            self.assertEqual(code,0,result)
            self.assertTrue(result["result"]["position_reached"])
            self.assertTrue(result["result"]["position_settings_restored"])
            self.assertEqual(drive.position,10000); self.assertEqual(drive.control,0x8000)
            self.assertEqual(drive.values[(0x35,1)],0); self.assertEqual(drive.values[(0x30,22)],0)
            self.assertEqual(drive.values[(0x83,20)],0); self.assertEqual(drive.values[(0x83,22)],300)

    def test_position_timeout_is_reported_as_not_reached_and_stopped(self):
        request=self.position_request(); request["maximum_duration_ms"]=800
        with tempfile.TemporaryDirectory(dir=PROJECT/"workspace/test_artifacts") as temporary,LoopbackDrive() as drive:
            drive.simulate_position=False; request["stop_file"]=str(Path(temporary)/"stop.request")
            code,result=self.invoke(drive,"position_move",**request)
            self.assertEqual(code,0,result); self.assertFalse(result["result"]["position_reached"])
            self.assertTrue(result["result"]["stopped_run_bit_verified"])
            self.assertTrue(result["result"]["position_settings_restored"])

    def test_stale_position_prevents_all_writes(self):
        request=self.position_request()
        with tempfile.TemporaryDirectory(dir=PROJECT/"workspace/test_artifacts") as temporary,LoopbackDrive() as drive:
            drive.position=123; request["stop_file"]=str(Path(temporary)/"stop.request")
            code,result=self.invoke(drive,"position_move",**request)
            self.assertNotEqual(code,0,result); self.assertEqual(drive.writes,[])


if __name__ == "__main__": unittest.main()
