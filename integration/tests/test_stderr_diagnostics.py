"""Operator diagnostics survive session disposal without altering MCP responses."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from guest_mcp_bridge import Bridge, BridgeFailure, StderrDiagnostics


SERVER = r'''
import json, os, sys, time
for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "stderr":
        os.write(2, ("startup " + os.environ["TEST_PASSWORD"] + " secret-token inherited-key\n").encode())
        os.write(2, b"x" * (2 * 1024 * 1024))
    if request["method"] == "stall":
        os.write(2, b"provider started before timeout\n")
        time.sleep(60)
    print(json.dumps({"jsonrpc":"2.0", "id":request["id"], "result":{"ok":True}}), flush=True)
os.write(2, b"driver stopped\n")
'''


class DiagnosticsTest(unittest.TestCase):
    def test_real_process_stop_preserves_bounded_redacted_log(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"TEST_ACCESS_KEY": "inherited-key"}):
            bridge = Bridge({"arms": {"hcu": {"command": [sys.executable, "-u", "-c", SERVER],
                                               "env": {"TEST_PASSWORD": "configured-value"}}}},
                            "secret-token", temp)
            session_id = bridge.dispatch("/session/start", {"arm": "hcu"})["session_id"]
            try:
                response = bridge.dispatch("/session/call", {"session_id": session_id, "method": "stderr"})
                self.assertEqual(response["response"]["result"], {"ok": True})
                self.assertNotIn("startup", json.dumps(response))
            finally:
                self.assertEqual(bridge.dispatch("/session/stop", {"session_id": session_id}), {"stopped": True})
            self.assertNotIn(session_id, bridge.sessions)
            directory = Path(temp) / session_id
            data = (directory / "stderr.log").read_bytes()
            state = json.loads((directory / "status.json").read_text())
            self.assertEqual(len(data), StderrDiagnostics.LIMIT)
            self.assertTrue(data.startswith(b"startup [REDACTED] [REDACTED] [REDACTED]\n"))
            for secret in (b"configured-value", b"secret-token", b"inherited-key"):
                self.assertNotIn(secret, data)
            self.assertTrue(state["truncated"])
            self.assertGreater(state["discarded_bytes"], 0)
            self.assertTrue(state["reader_done"])
            self.assertTrue(state["session_closed"])
            self.assertEqual(state["returncode"], 0)
            self.assertEqual(state["errors"], [])

    def test_timeout_then_stop_keeps_failure_diagnostic(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge = Bridge({"arms": {"hcu": {"command": [sys.executable, "-u", "-c", SERVER]}}}, "secret-token", temp)
            session_id = bridge.dispatch("/session/start", {"arm": "hcu"})["session_id"]
            with self.assertRaises(BridgeFailure):
                bridge.dispatch("/session/call", {"session_id": session_id, "method": "stall", "timeout": 0.2})
            bridge.dispatch("/session/stop", {"session_id": session_id})
            directory = Path(temp) / session_id
            self.assertEqual((directory / "stderr.log").read_bytes(), b"provider started before timeout\n")
            self.assertTrue(json.loads((directory / "status.json").read_text())["session_closed"])

    def test_redaction_covers_every_chunk_boundary_and_eof(self):
        for boundary in range(1, 20):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temp:
                writer = StderrDiagnostics(Path(temp) / "session", ["secret", "secret-long"])
                data = b"pre secret-long secret post secret"
                for start in range(0, len(data), boundary):
                    writer.write(data[start:start + boundary])
                    self.assertLessEqual(len(writer.pending), len(b"secret-long") - 1)
                writer.finish()
                self.assertEqual((writer.directory / "stderr.log").read_bytes(),
                                 b"pre [REDACTED] [REDACTED] post [REDACTED]")

    def test_write_failure_is_recorded_and_does_not_expose_exception_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            writer = StderrDiagnostics(Path(temp) / "session", ["secret"])
            with patch.object(writer.stream, "write", side_effect=OSError("secret")), patch("sys.stderr") as error:
                writer.write(b"ordinary diagnostic\n")
                writer.finish()
            state = json.loads((writer.directory / "status.json").read_text())
            self.assertIn("stderr:OSError", state["errors"])
            self.assertEqual(state["saved_bytes"], 0)
            self.assertEqual(state["discarded_bytes"], len(b"ordinary diagnostic\n"))
            self.assertNotIn("secret", str(error.mock_calls))

    def test_status_failure_does_not_stop_stream_drain(self):
        with tempfile.TemporaryDirectory() as temp:
            writer = StderrDiagnostics(Path(temp) / "session", [])
            with patch.object(Path, "replace", side_effect=OSError("sensitive path")), patch("sys.stderr"):
                writer.write(b"diagnostic")
                writer.finish()
            self.assertIn("status:OSError", writer.state["errors"])
            self.assertEqual((writer.directory / "stderr.log").read_bytes(), b"diagnostic")

    def test_start_failure_is_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge = Bridge({"arms": {"hcu": {"command": [str(Path(temp) / "missing-executable")]}}}, "secret-token", temp)
            with self.assertRaises(OSError):
                bridge.dispatch("/session/start", {"arm": "hcu"})
            self.assertEqual(bridge.sessions, {})
            states = list(Path(temp).glob("*/status.json"))
            self.assertEqual(len(states), 1)
            state = json.loads(states[0].read_text())
            self.assertIn("start:FileNotFoundError", state["errors"])
            self.assertTrue(state["reader_done"])

    def test_empty_session_logs_are_distinct_and_persist_after_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "diagnostics"
            command = [sys.executable, "-c", "import sys; sys.stdin.read()"]
            bridge = Bridge({"arms": {"hcu": {"command": command}}}, "secret-token", root)
            self.assertTrue(root.is_dir())
            sessions = []
            for _ in range(2):
                session_id = bridge.dispatch("/session/start", {"arm": "hcu"})["session_id"]
                sessions.append(session_id)
                bridge.dispatch("/session/stop", {"session_id": session_id})
            self.assertNotEqual(*sessions)
            for session_id in sessions:
                self.assertEqual((root / session_id / "stderr.log").read_bytes(), b"")
                state = json.loads((root / session_id / "status.json").read_text())
                self.assertEqual(state["received_bytes"], 0)
                self.assertEqual(state["errors"], [])
                self.assertTrue(state["session_closed"])


if __name__ == "__main__":
    unittest.main()
