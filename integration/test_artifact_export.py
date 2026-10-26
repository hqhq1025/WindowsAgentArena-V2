"""Actual output preservation must not depend on scoring or screenshot success."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import artifact_export
from artifact_export import declared_files, export_phase
from model_agent import PhaseDeadline
import waa_compare


TASK = {"id": "fixture", "_selection_id": "fixture", "instruction": "Original task",
        "evaluator": {"result": {"type": "vm_file", "path": "C:\\work\\actual.xlsx", "dest": "actual.xlsx"},
                      "expected": {"type": "vm_file", "path": "C:\\hidden\\gold.xlsx", "dest": "gold.xlsx"}}}


class ExportTests(unittest.TestCase):
    def test_declarations_are_generic_and_never_include_expected(self):
        task = {"evaluator": {"result": [TASK["evaluator"]["result"],
            {"type": "vm_file", "multi": True, "path": ["a", "b"], "dest": ["../../a.xlsx", "b.xlsx"]},
            {"type": "other", "path": "not-a-file-getter"}], "expected": TASK["evaluator"]["expected"]}}
        entries = declared_files(task)
        self.assertEqual([x["guest_path"] for x in entries], ["C:\\work\\actual.xlsx", "a", "b"])
        self.assertEqual([x["filename"] for x in entries], ["actual.xlsx", "a.xlsx", "b.xlsx"])

    def test_bad_multi_configuration_is_reported_not_silently_zipped(self):
        entries = declared_files({"evaluator": {"result": {"type": "vm_file", "multi": True,
            "path": ["a", "b"], "dest": ["only-one"]}}})
        self.assertEqual(entries[0]["status"], "invalid_config")

    def test_file_timeout_does_not_prevent_screenshot(self):
        controller = Mock()
        controller.get_file.side_effect = PhaseDeadline("expired")
        controller.get_screenshot.return_value = b"image"
        with tempfile.TemporaryDirectory() as temp:
            report = export_phase(controller, TASK, temp, {}, "pre-evaluator", contextlib.nullcontext,
                                  state_stability="unstable_cleanup_failed")
            self.assertEqual(report["files"][0]["status"], "timeout")
            self.assertEqual(report["screenshot"]["status"], "saved")
            self.assertEqual((Path(temp) / "final.png").read_bytes(), b"image")
            self.assertEqual(report["state_stability"], "unstable_cleanup_failed")

    def test_phase_budget_exhaustion_prevents_further_requests(self):
        controller = Mock()
        controller.get_file.return_value = b"file"
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(artifact_export.time, "monotonic", side_effect=[0, 0, 31]):
            report = export_phase(controller, TASK, temp, {}, "pre-evaluator", contextlib.nullcontext,
                                  state_stability="unknown", timeout=30)
        self.assertEqual(report["files"][0]["status"], "saved")
        self.assertEqual(report["screenshot"]["status"], "timeout")
        controller.get_screenshot.assert_not_called()

    def test_unavailable_file_is_not_mislabeled_missing(self):
        controller = Mock()
        controller.get_file.return_value = None
        controller.get_screenshot.side_effect = OSError("capture failed")
        with tempfile.TemporaryDirectory() as temp:
            report = export_phase(controller, TASK, temp, {}, "post-evaluator", contextlib.nullcontext,
                                  state_stability="unknown")
        self.assertEqual(report["files"][0]["status"], "unavailable")
        self.assertEqual(report["screenshot"]["status"], "error")

    def test_http_error_and_real_stat_absence_produce_relative_missing_evidence(self):
        class MissingFile(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"original guest HTTP file-read failure")
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), MissingFile)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def execute(script):
            result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
            return {"returncode": result.returncode, "output": result.stdout, "error": result.stderr}
        controller = SimpleNamespace(http_server=f"http://127.0.0.1:{server.server_port}",
                                     execute_python_command=execute, get_screenshot=lambda: b"image")
        with tempfile.TemporaryDirectory() as temp:
            guest_path = str(Path(temp) / "absent-output.xlsx")
            task = {"evaluator": {"result": {"type": "vm_file", "path": guest_path, "dest": "output.xlsx"}}}
            report = export_phase(controller, task, temp, {}, "post-evaluator", contextlib.nullcontext,
                                  state_stability="stable")
            item = report["files"][0]
            self.assertEqual(item["status"], "missing")
            self.assertEqual(item["source_path"], guest_path)
            self.assertFalse(Path(item["missing_evidence"]).is_absolute())
            proof = json.loads((Path(temp) / item["missing_evidence"]).read_text())
            self.assertEqual((proof["status"], proof["query_succeeded"], proof["exists"]), ("completed", True, False))
            self.assertEqual(proof["original_read"]["http_status"], 500)
            self.assertEqual(proof["original_read"]["http_error_body"], "original guest HTTP file-read failure")
            self.assertEqual(proof["observation"]["stdout"], proof["query_response"]["output"])
            self.assertEqual(json.loads(proof["observation"]["stdout"]), {"guest_path": guest_path, "exists": False})
            self.assertEqual(proof["observation"]["returncode"], 0)
            self.assertEqual(proof["observation"]["stderr"], "")
            self.assertEqual(report["screenshot"]["status"], "saved")

    def test_unknown_failed_or_mismatched_stat_never_becomes_missing(self):
        path = TASK["evaluator"]["result"]["path"]
        def result(exists=False, guest_path=path, **overrides):
            return {"returncode": 0, "output": json.dumps({"guest_path": guest_path, "exists": exists}),
                    "error": "", **overrides}
        for reply in (None, result(returncode=1), result(error="permission error"), result(True),
                      result(0), result(guest_path="different"), result(output="not json"),
                      TimeoutError("transport timeout")):
            with self.subTest(reply=reply), tempfile.TemporaryDirectory() as temp:
                controller = Mock()
                controller.get_file.return_value = None
                controller.get_screenshot.return_value = b"image"
                if isinstance(reply, Exception):
                    controller.execute_python_command.side_effect = reply
                else:
                    controller.execute_python_command.return_value = reply
                report = export_phase(controller, TASK, temp, {}, "pre-evaluator", contextlib.nullcontext,
                                      state_stability="unknown")
                item = report["files"][0]
                self.assertEqual(item["status"], "unavailable")
                self.assertNotIn("missing_evidence", item)
                proof = json.loads((Path(temp) / item["stat_evidence"]).read_text())
                self.assertEqual(proof["original_read"]["status"], "unavailable")
                self.assertEqual(report["screenshot"]["status"], "saved")

    def test_guest_stat_error_is_not_caught_as_file_absence(self):
        controller = Mock()
        controller.get_file.return_value = None
        controller.get_screenshot.return_value = b"image"
        def execute(script):
            with patch("os.stat", side_effect=PermissionError("access denied")):
                with self.assertRaises(PermissionError):
                    exec(script, {})
            return {"returncode": 1, "output": "", "error": "PermissionError: access denied"}
        controller.execute_python_command.side_effect = execute
        with tempfile.TemporaryDirectory() as temp:
            report = export_phase(controller, TASK, temp, {}, "post-evaluator", contextlib.nullcontext,
                                  state_stability="stable")
            self.assertEqual(report["files"][0]["status"], "unavailable")

    def test_stat_cannot_borrow_screenshot_time_budget(self):
        controller = Mock()
        controller.get_file.return_value = None
        controller.get_screenshot.return_value = b"image"
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(artifact_export.time, "monotonic", side_effect=[0, 0, 16, 16]):
            report = export_phase(controller, TASK, temp, {}, "pre-evaluator", contextlib.nullcontext,
                                  state_stability="unknown", timeout=30)
            self.assertEqual(report["files"][0]["status"], "unavailable")
            self.assertEqual(report["files"][0]["stat_query_status"], "timeout")
            self.assertEqual(report["screenshot"]["status"], "saved")
            controller.execute_python_command.assert_not_called()

    def test_missing_screenshot_never_uses_stat_proof(self):
        controller = Mock()
        controller.get_screenshot.return_value = None
        with tempfile.TemporaryDirectory() as temp:
            report = export_phase(controller, {}, temp, {}, "post-evaluator", contextlib.nullcontext,
                                  state_stability="stable")
            self.assertEqual(report["screenshot"]["status"], "unavailable")
            self.assertNotIn("missing_evidence", report["screenshot"])
            controller.execute_python_command.assert_not_called()

    def test_missing_requires_persisted_evidence_and_keeps_original_error(self):
        controller = Mock()
        controller.get_file.side_effect = ConnectionError("original download transport error")
        controller.get_screenshot.return_value = b"image"
        controller.execute_python_command.return_value = {"returncode": 0, "error": "",
            "output": json.dumps({"guest_path": TASK["evaluator"]["result"]["path"], "exists": False})}
        original_open = Path.open
        def fail_evidence(path, *args, **kwargs):
            if path.name.endswith("-stat.json"):
                raise OSError("evidence disk full")
            return original_open(path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, "open", fail_evidence):
            report = export_phase(controller, TASK, temp, {}, "post-evaluator", contextlib.nullcontext,
                                  state_stability="stable")
            item = report["files"][0]
            self.assertEqual(item["status"], "error")
            self.assertIn("original download transport error", item["error"])
            self.assertIn("evidence disk full", item["stat_evidence_error"])
            self.assertNotIn("missing_evidence", item)
            self.assertEqual(report["screenshot"]["status"], "saved")

    def test_disk_failure_is_recorded_without_leaving_partial_file(self):
        controller = Mock()
        controller.get_file.return_value = b"output"
        controller.get_screenshot.return_value = b"image"
        original_open = Path.open
        def write_failure(path, *args, **kwargs):
            if path.name.endswith(".partial"):
                raise OSError("disk full")
            return original_open(path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, "open", write_failure):
            report = export_phase(controller, TASK, temp, {}, "pre-evaluator", contextlib.nullcontext,
                                  state_stability="unknown")
            self.assertEqual(report["files"][0]["status"], "error")
            self.assertEqual(report["screenshot"]["status"], "error")
            self.assertEqual(list(Path(temp).rglob("*.partial")), [])
            controller.execute_python_command.assert_not_called()

    def test_interrupted_phase_preserves_partial_manifest_and_propagates(self):
        for exception in (KeyboardInterrupt("cancel"), SystemExit("stop")):
            with self.subTest(exception=type(exception).__name__), tempfile.TemporaryDirectory() as temp:
                controller = Mock()
                controller.get_file.side_effect = [b"first-output", exception]
                task = {"evaluator": {"result": {"type": "vm_file", "multi": True,
                    "path": ["first", "second"], "dest": ["first.xlsx", "second.xlsx"]}}}
                with self.assertRaises(type(exception)) as raised:
                    export_phase(controller, task, temp, {}, "pre-evaluator", contextlib.nullcontext,
                                 state_stability="unknown")
                self.assertIs(raised.exception, exception)
                report = json.loads((Path(temp) / "artifacts/pre-evaluator/manifest.json").read_text())
                self.assertEqual(report, exception.artifact_export_report)
                self.assertEqual([x["status"] for x in report["files"]], ["saved", "interrupted"])
                self.assertIn("persisted_at", report["files"][0])
                self.assertEqual(Path(report["files"][0]["host_path"]).read_bytes(), b"first-output")
                self.assertEqual(report["screenshot"]["status"], "not_attempted")
                controller.get_screenshot.assert_not_called()


class RunnerExportTests(unittest.TestCase):
    def run_fixture(self, folder, *, score_error=None, cleanup_error=None,
                    screenshot_error=False, file_error=False, agent_status="done", export_interrupt=False):
        marker = folder.parent / "owner-cleaned"
        args = SimpleNamespace(guest_url="http://fixture", bridge_url="http://fixture", upstream_host=None,
            cleanup_command=json.dumps([sys.executable, "-c", "from pathlib import Path; "
                                        f"Path({str(marker)!r}).touch()"]), reset_command=None,
            setup_timeout=5, setup_settle=0, timeout=5, evaluator_timeout=5, cleanup_timeout=5,
            model="fixture", reasoning="high", max_responses=1, run_id="run")
        env = Mock()
        state = [b"before-save"]
        env.controller.get_file.side_effect = OSError("download failed") if file_error else lambda path: state[0]
        env.controller.get_screenshot.side_effect = [b"initial", OSError("capture failed"), b"post"] \
            if screenshot_error else lambda: b"image"
        if export_interrupt:
            env.controller.get_screenshot.side_effect = [b"initial", KeyboardInterrupt("during export")]
        def evaluate():
            state[0] = b"after-postconfig"
            if score_error:
                raise score_error
            return .75
        bridge = Mock()
        bridge.shell_runtime.return_value = {"version": "7", "edition": "Core", "executable": "pwsh.exe"}
        bridge.call.side_effect = [{"instructions": ""}, None, {"tools": []}]
        bridge.stop.side_effect = cleanup_error
        with patch.dict(os.environ, {"WAA_BRIDGE_TOKEN": "fixture", "OPENAI_BASE_URL": "http://model"}), \
             patch("bridge_client.BridgeClient", return_value=bridge), \
             patch.object(waa_compare, "make_env", return_value=env), \
             patch.object(waa_compare, "prepare_env"), \
             patch.object(waa_compare, "ResponsesClient"), \
             patch.object(waa_compare, "run_agent", return_value={"status": agent_status}), \
             patch.object(env, "evaluate", side_effect=evaluate) as metric:
            result = waa_compare.run_episode(args, "hcu", {"command": ["driver"]}, "fixture", TASK, folder)
        self.assertTrue(marker.exists())
        self.assertEqual(result["vm_cleanup_status"], "completed")
        self.assertEqual(json.loads((folder / "result.json").read_text())["evaluator_score"], result["evaluator_score"])
        for call in env.controller.get_file.call_args_list:
            self.assertEqual(call.args, ("C:\\work\\actual.xlsx",))
        return result, metric

    def test_screenshot_failure_does_not_skip_score_and_phases_are_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "episode"
            result, metric = self.run_fixture(folder, screenshot_error=True)
            reports = result["artifact_exports"]
            self.assertEqual(Path(reports[0]["files"][0]["host_path"]).read_bytes(), b"before-save")
            self.assertEqual(Path(reports[1]["files"][0]["host_path"]).read_bytes(), b"after-postconfig")
            self.assertEqual(reports[0]["screenshot"]["status"], "error")
            self.assertFalse((folder / "cache").exists())
            self.assertEqual(result["evaluator_score"], .75)
            metric.assert_called_once()

    def test_postconfig_or_metric_error_still_preserves_actual_files(self):
        with tempfile.TemporaryDirectory() as temp:
            result, _ = self.run_fixture(Path(temp) / "episode", score_error=RuntimeError("postconfig failure"))
            self.assertEqual(result["evaluator_status"], "error")
            self.assertIsNone(result["evaluator_score"])
            self.assertEqual([r["files"][0]["status"] for r in result["artifact_exports"]], ["saved", "saved"])

    def test_export_failure_preserves_existing_score_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            result, _ = self.run_fixture(Path(temp) / "episode", file_error=True)
            self.assertEqual(result["evaluator_score"], .75)
            self.assertEqual([r["files"][0]["status"] for r in result["artifact_exports"]], ["error", "error"])

    def test_cleanup_failure_and_interruption_preserve_without_scoring(self):
        for options in ({"cleanup_error": RuntimeError("still active")}, {"agent_status": "interrupted"}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temp:
                result, metric = self.run_fixture(Path(temp) / "episode", **options)
                self.assertEqual(len(result["artifact_exports"]), 1)
                self.assertEqual(result["artifact_exports"][0]["files"][0]["status"], "saved")
                metric.assert_not_called()
                if "cleanup_error" in options:
                    self.assertEqual(result["artifact_exports"][0]["state_stability"], "unstable_cleanup_failed")

    def test_infeasible_outcome_still_exports_declared_result(self):
        with tempfile.TemporaryDirectory() as temp:
            result, _ = self.run_fixture(Path(temp) / "episode", agent_status="infeasible")
            self.assertEqual(result["status"], "infeasible")
            self.assertEqual(result["artifact_exports"][0]["files"][0]["status"], "saved")

    def test_export_interruption_keeps_completed_file_evidence_and_skips_metric(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "episode"
            result, metric = self.run_fixture(folder, export_interrupt=True)
            self.assertEqual(result["status"], "interrupted")
            self.assertEqual(result["interrupted_phase"], "artifact_export_pre-evaluator")
            report = result["artifact_exports"][0]
            self.assertEqual(report["files"][0]["status"], "saved")
            self.assertEqual(report["screenshot"]["status"], "interrupted")
            metric.assert_not_called()

    def test_manifest_interruption_retains_full_report_in_result(self):
        original_write = Path.write_text
        def interrupt_manifest(path, *args, **kwargs):
            if path.name == "manifest.json":
                raise KeyboardInterrupt("during manifest write")
            return original_write(path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as temp, patch.object(Path, "write_text", interrupt_manifest):
            folder = Path(temp) / "episode"
            result, metric = self.run_fixture(folder)
            self.assertEqual(result["status"], "interrupted")
            report = json.loads((folder / "result.json").read_text())["artifact_exports"][0]
            self.assertEqual(report["files"][0]["status"], "saved")
            self.assertIn("persisted_at", report["files"][0])
            self.assertEqual(report["screenshot"]["status"], "saved")
            self.assertIn("KeyboardInterrupt", report["manifest_error"])
            metric.assert_not_called()


if __name__ == "__main__":
    unittest.main()
