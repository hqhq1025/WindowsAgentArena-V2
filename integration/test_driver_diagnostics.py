"""Exercise the actual guest collection script on local fixtures, without a VM."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

import driver_diagnostics as diagnostics
import remote_vm
import vm


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.guest = self.root / "guest"
        self.guest.mkdir()
        self.output = self.root / "episode"
        self.output.mkdir()
        self.binding = {"episode_id": "owned", "container": "waa-owned", "controller_url": "http://unused",
                        "storage": str(self.output / "storage"), "driver_diagnostics_required": True}
        self.container = {"Id": "a" * 64, "Config": {"Labels": {"waa.owner": "waa-v2", "waa.episode": "owned"}}}
        self.patch = patch.object(diagnostics, "GUEST_ROOT", str(self.guest))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def make_session(self, data=b"native provider diagnostic\n", **changes):
        session_id = str(uuid.uuid4())
        directory = self.guest / session_id
        directory.mkdir()
        state = {"limit_bytes": diagnostics.LIMITS["stderr.log"], "received_bytes": len(data),
                 "saved_bytes": len(data), "discarded_bytes": 0, "truncated": False,
                 "reader_done": True, "session_closed": True, "errors": []}
        state.update(changes)
        (directory / "stderr.log").write_bytes(data)
        (directory / "status.json").write_text(json.dumps(state))
        return session_id, directory

    def request(self, url, body, timeout):
        self.assertEqual(url, "http://unused/execute")
        self.assertFalse(body["shell"])
        self.assertGreater(timeout, 0)
        process = subprocess.run([sys.executable, *body["command"][1:]],
                                 capture_output=True, text=True, timeout=timeout)
        return {"returncode": process.returncode, "output": process.stdout}

    def export(self):
        return diagnostics.export_diagnostics(self.binding, self.output, self.request)

    def test_complete_capture_is_read_back_and_source_is_preserved(self):
        session_id, directory = self.make_session()
        report = self.export()
        self.assertTrue(report["export_complete"])
        self.assertTrue(report["diagnostics_complete"])
        self.assertTrue(diagnostics.reusable_export(report))
        target = Path(report["receipt"]).parent / session_id
        self.assertEqual((target / "stderr.log").read_bytes(), (directory / "stderr.log").read_bytes())
        self.assertEqual((target / "status.json").read_bytes(), (directory / "status.json").read_bytes())
        (target / "stderr.log").unlink()
        self.assertFalse(diagnostics.reusable_export(report))

    def test_empty_log_is_distinct_from_no_sessions_and_missing_directory(self):
        report = self.export()
        self.assertEqual(report["status"], "no_sessions")
        self.assertTrue(report["export_complete"])
        self.make_session(b"")
        report = self.export()
        self.assertEqual(report["status"], "exported")
        self.assertEqual(report["sessions"][0]["capture_status"]["received_bytes"], 0)
        with patch.object(diagnostics, "GUEST_ROOT", str(self.root / "absent")):
            report = self.export()
        self.assertEqual(report["inventory_status"], "missing")
        self.assertFalse(report["export_complete"])

    def test_truncation_and_capture_failure_remain_visible_after_successful_export(self):
        self.make_session(truncated=True, discarded_bytes=100)
        self.make_session(errors=["stderr:OSError"])
        report = self.export()
        self.assertTrue(report["export_complete"])
        self.assertFalse(report["diagnostics_complete"])
        states = [item["capture_status"] for item in report["sessions"]]
        self.assertTrue(any(state["truncated"] for state in states))
        self.assertTrue(any(state["errors"] for state in states))

    def test_unclosed_session_cannot_be_claimed_as_complete(self):
        self.make_session(session_closed=False)
        report = self.export()
        self.assertFalse(report["export_complete"])
        self.assertEqual(report["sessions"][0]["status"], "incomplete")

    def test_missing_oversized_and_redirected_files_preserve_failure_status(self):
        _, directory = self.make_session()
        (directory / "status.json").unlink()
        report = self.export()
        self.assertFalse(report["export_complete"])
        self.assertEqual(report["sessions"][0]["status.json"], "missing")
        (directory / "stderr.log").write_bytes(b"x" * (diagnostics.LIMITS["stderr.log"] + 1))
        report = self.export()
        self.assertEqual(report["sessions"][0]["stderr.log"], "oversized")
        (directory / "stderr.log").unlink()
        outside = self.root / "private"
        outside.write_text("must not export")
        (directory / "stderr.log").symlink_to(outside)
        report = self.export()
        self.assertEqual(report["sessions"][0]["stderr.log"], "unsafe_path")
        self.assertEqual(report["files"], [])

    def test_invalid_directory_does_not_export_unrelated_files(self):
        (self.guest / "not-a-session").mkdir()
        (self.guest / "not-a-session" / "stderr.log").write_text("private")
        report = self.export()
        self.assertFalse(report["export_complete"])
        self.assertEqual(report["files"], [])
        with self.assertRaises(ValueError):
            diagnostics.guest_script("../outside")

    def test_timeout_records_only_type_and_preserves_source(self):
        _, directory = self.make_session()
        report = diagnostics.export_diagnostics(self.binding, self.output,
                                                lambda *a, **k: (_ for _ in ()).throw(TimeoutError("token=private")))
        self.assertFalse(report["export_complete"])
        self.assertEqual(report["error_type"], "TimeoutError")
        self.assertNotIn("private", Path(report["receipt"]).read_text())
        self.assertTrue((directory / "stderr.log").is_file())

    def test_inventory_change_prevents_a_complete_seal(self):
        self.make_session()
        calls = 0
        def changing(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.make_session()
            return self.request(*args, **kwargs)
        report = diagnostics.export_diagnostics(self.binding, self.output, changing)
        self.assertTrue(report["inventory_changed"])
        self.assertFalse(report["export_complete"])

    def test_cleanup_exports_before_stop_and_reuses_success_after_absence(self):
        self.make_session()
        order = []
        def stop(binding):
            self.assertTrue(binding["driver_diagnostics_export"]["export_complete"])
            order.append("stop")
            return {"status": "stopped", "storage_preserved": binding["storage"]}
        with patch.object(vm, "inspect_container", return_value=self.container), \
             patch.object(vm, "request", side_effect=self.request), patch.object(vm, "capture_container"), \
             patch.object(vm, "stop_binding", side_effect=stop):
            vm.capture_and_stop(self.binding, self.output)
        self.assertEqual(order, ["stop"])
        self.assertFalse(self.binding["storage_reclamation_blocked"])
        previous = self.binding["driver_diagnostics_export"]
        with patch.object(vm, "inspect_container", return_value=None), patch.object(vm, "request") as request:
            self.assertEqual(vm.capture_driver_diagnostics(self.binding, self.output), previous)
        request.assert_not_called()

    def test_export_failure_stops_container_but_blocks_success_and_storage_reclamation(self):
        owner = self.output / "episode.json"
        owner.write_text(json.dumps(self.binding))
        with patch("sys.argv", ["vm.py", "stop", "--episode", str(owner)]), \
             patch.object(vm, "inspect_container", return_value=self.container), \
             patch.object(vm, "request", side_effect=TimeoutError("private")), \
             patch.object(vm, "capture_container"), patch.object(vm, "stop_binding", return_value={"status": "stopped"}) as stop:
            with self.assertRaisesRegex(RuntimeError, "preserve episode storage"):
                vm.main()
        stop.assert_called_once()
        saved = json.loads(owner.read_text())
        self.assertTrue(saved["storage_reclamation_blocked"])
        self.assertEqual(saved["cleanup"]["status"], "stopped")
        self.assertFalse(saved["driver_diagnostics_export"]["export_complete"])

    def test_remote_cancel_saves_failure_before_propagating_nonzero(self):
        worker = self.root / "worker.json"
        progress = worker.with_suffix(".vm.json")
        progress.write_text(json.dumps(self.binding))
        with patch.object(vm, "inspect_container", return_value=self.container), \
             patch.object(vm, "request", side_effect=TimeoutError("private")), \
             patch.object(vm, "capture_container"), patch.object(vm, "stop_binding", return_value={"status": "stopped"}):
            with self.assertRaisesRegex(RuntimeError, "preserve episode storage"):
                remote_vm.cancel_worker(argparse.Namespace(worker_record=worker))
        self.assertTrue(json.loads(progress.read_text())["storage_reclamation_blocked"])

    def test_wrong_owner_never_reads_guest_diagnostics(self):
        self.container["Config"]["Labels"]["waa.episode"] = "other"
        with patch.object(vm, "inspect_container", return_value=self.container), patch.object(vm, "request") as request:
            with self.assertRaisesRegex(RuntimeError, "ownership"):
                vm.capture_driver_diagnostics(self.binding, self.output)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
