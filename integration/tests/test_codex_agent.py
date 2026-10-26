"""Host admission and real subprocess evidence tests; no models or VMs."""
import io
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import urllib.parse
import zipfile

import codex_agent
from guest_mcp_bridge import Command


class CodexAgentTests(unittest.TestCase):
    IDENTITY = '["waa","run / 1","hcu","任务/full-id"]'

    def test_guest_status_protocol_survives_non_unicode_windows_locale(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = {'status': 'completed', 'final_message': '中文、日本語、🙂'}
            (root / 'status.json').write_text(json.dumps(expected), encoding='utf-8')
            command = [sys.executable, str(Path(codex_agent.__file__).with_name('guest_codex.py')),
                       'status', '--root', str(root)]
            result = subprocess.run(command, capture_output=True, env={**os.environ,
                'PYTHONIOENCODING': 'cp1252', 'PYTHONUTF8': '0'}, check=True)
            self.assertEqual(json.loads(result.stdout.decode('ascii')), expected)

    def budget(self, **changes):
        state = {'enabled': True, 'limit': 100, 'unit': 'responses_completed_response',
                 'counted_statuses': ['completed'], 'request_limit': 400,
                 'consecutive_failure_limit': 4, 'relay_retries': 0,
                 'persistent': True, 'writable': True, 'task_id': self.IDENTITY,
                 'requests_admitted': 0, 'completed': 0, 'reserved': 0,
                 'unknown_reservations': 0, 'circuit_open': False}
        return dict(state, **changes)

    def test_budget_uses_full_canonical_identity_and_accepts_fresh_route(self):
        config = {'base_url': 'http://127.0.0.62:8568/v1'}
        state = self.budget()
        with patch.object(codex_agent, 'gateway_get', return_value=state) as get:
            self.assertIs(codex_agent.require_budget(config, self.IDENTITY, fresh=True), state)
        actual_config, route = get.call_args.args
        self.assertIs(actual_config, config)
        parsed = urllib.parse.urlsplit(route)
        self.assertEqual(parsed.path, '/_weavebench/task-budget')
        self.assertEqual(urllib.parse.parse_qs(parsed.query), {'task_id': [self.IDENTITY]})

    def test_budget_rejects_wrong_or_missing_task_identity(self):
        for identity in ('_notask', 'different-task', None):
            with self.subTest(identity=identity), patch.object(codex_agent, 'gateway_get', return_value=self.budget(task_id=identity)):
                with self.assertRaisesRegex(RuntimeError, 'canonical task identity'):
                    codex_agent.require_budget({}, self.IDENTITY)

    def test_budget_rejects_policy_drift_and_missing_required_fields(self):
        changed = {'enabled': False, 'limit': 99, 'unit': 'requests',
                   'counted_statuses': ['completed', 'incomplete'], 'request_limit': 401,
                   'consecutive_failure_limit': 5, 'relay_retries': 1,
                   'persistent': False, 'writable': False}
        for name, value in changed.items():
            for state in (self.budget(**{name: value}), {key: value for key, value in self.budget().items() if key != name}):
                with self.subTest(field=name, missing=name not in state), patch.object(codex_agent, 'gateway_get', return_value=state):
                    with self.assertRaisesRegex(RuntimeError, 'completed-response budget'):
                        codex_agent.require_budget({}, self.IDENTITY)

    def test_fresh_route_rejects_prior_requests_reservations_and_open_circuit(self):
        for name in ('requests_admitted', 'completed', 'reserved', 'unknown_reservations', 'circuit_open'):
            state = self.budget(**{name: True if name == 'circuit_open' else 1})
            with self.subTest(field=name), patch.object(codex_agent, 'gateway_get', return_value=state):
                with self.assertRaisesRegex(RuntimeError, 'already has gateway request history'):
                    codex_agent.require_budget({}, self.IDENTITY, fresh=True)
                self.assertIs(codex_agent.require_budget({}, self.IDENTITY, fresh=False), state)

    def test_guest_must_read_same_relay_instance_and_ledger(self):
        health = dict(version='r3', configuration_id='config', instance_id='instance', ledger_id='ledger')
        codex_agent.require_same_relay(health, dict(health), self.budget(), self.budget())
        for field in health:
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'relay differ'):
                codex_agent.require_same_relay(health, dict(health, **{field: 'other'}), self.budget(), self.budget())
        with self.assertRaisesRegex(RuntimeError, 'budget views differ'):
            codex_agent.require_same_relay(health, health, self.budget(), self.budget(task_id='other'))

    def test_config_toml_round_trip_preserves_paths_text_and_retry_policy(self):
        driver = {'command': [r'C:\Program Files\computer-use.exe', '--name', 'quoted "value"'],
                  'cwd': r'C:\Task Workspace', 'env': {'TRACE.VALUE': '中文\nline "two"\\'}}
        instructions = '原始说明\nPreserve "quotes", slash \\ and tabs\t.'
        base = 'http://127.0.0.62:8568/t/%5B%22waa%22%5D/v1'
        config = tomllib.loads(codex_agent.build_config('gpt-5.4', 'high', base, driver, instructions))
        self.assertEqual(config['developer_instructions'], instructions)
        self.assertEqual(config['model'], 'gpt-5.4')
        self.assertEqual(config['model_reasoning_effort'], 'high')
        self.assertFalse(config['allow_login_shell'])
        self.assertFalse(config['features']['unbounded_connection_retries'])
        self.assertFalse(config['features']['mcp_2026_07_28'])
        provider = config['model_providers'][config['model_provider']]
        self.assertEqual(provider['base_url'], base)
        self.assertEqual(provider['wire_api'], 'responses')
        self.assertEqual(provider['request_max_retries'], 0)
        self.assertEqual(provider['stream_max_retries'], 0)
        self.assertFalse(provider['supports_websockets'])
        self.assertFalse(provider['requires_openai_auth'])
        self.assertEqual(provider['capabilities']['remote_compaction'], 'unsupported')
        mcp = config['mcp_servers']['computer_use']
        self.assertEqual([mcp['command'], *mcp['args']], driver['command'])
        self.assertEqual(mcp['cwd'], driver['cwd'])
        self.assertEqual(mcp['env'], driver['env'])
        self.assertTrue(mcp['required'])

    def zip_bytes(self, members):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            for name, content in members:
                archive.writestr(name, content)
        return output.getvalue()

    def test_extract_preserves_files_and_refuses_existing_target(self):
        data = self.zip_bytes([('status.json', b'{"status":"completed"}'),
                               ('codex-home/sessions/a.jsonl', b'{"type":"session_meta"}\n')])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'evidence'
            codex_agent.extract_evidence(data, target)
            self.assertEqual((target / 'codex-home/sessions/a.jsonl').read_bytes(), b'{"type":"session_meta"}\n')
            with self.assertRaises(FileExistsError):
                codex_agent.extract_evidence(data, target)

    def test_extract_checks_all_members_before_writing_anything(self):
        for name in ('../escape', '/absolute', 'logs/../../escape', 'logs\\escape'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'evidence'
                data = self.zip_bytes([('valid.txt', b'valid first'), (name, b'unsafe')])
                with self.assertRaisesRegex(ValueError, 'Invalid Codex evidence member'):
                    codex_agent.extract_evidence(data, target)
                self.assertFalse(target.exists())
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_extract_checks_aggregate_expanded_size_before_creating_target(self):
        # Central-directory advertised sizes exercise the admission check without
        # allocating hundreds of MiB or extracting deliberately invalid payloads.
        data = bytearray(self.zip_bytes([('a', b'a'), ('b', b'b')]))
        cursor = 0
        for _ in range(2):
            cursor = data.index(b'PK\x01\x02', cursor)
            struct.pack_into('<I', data, cursor + 24, 128 * 1024 * 1024 + 1)
            cursor += 46
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'evidence'
            with self.assertRaisesRegex(ValueError, 'archive limit'):
                codex_agent.extract_evidence(bytes(data), target)
            self.assertFalse(target.exists())

    @unittest.skipIf(os.name == 'nt', 'Real POSIX supervisor check; Windows has a separate canary')
    def test_real_command_stdin_and_full_files_survive_summary_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / 'logs'
            logs.mkdir()
            prompt = '逐字输入\nquotes " and slash \\'.encode('utf-8')
            stdin = root / 'prompt.txt'
            stdin.write_bytes(prompt)
            size = 1024 * 1024 + 731
            script = ('import sys; data=sys.stdin.buffer.read(); '
                      'sys.stdout.buffer.write(data+b"\\n"+b"x"*' + str(size) + '+b"STDOUT_END"); '
                      'sys.stderr.buffer.write(b"y"*' + str(size) + '+b"STDERR_END")')
            command = Command([sys.executable, '-c', script], stdin_path=stdin, output_dir=logs)
            try:
                result = command.wait(10)
                self.assertEqual(result['returncode'], 0)
                self.assertFalse(result['timed_out'])
                self.assertFalse(result['cancelled'])
                self.assertTrue(result['output_truncated'])
                self.assertTrue(result['error_truncated'])
                self.assertTrue(result['output'].startswith(prompt.decode('utf-8') + '\n'))
                self.assertNotIn('STDOUT_END', result['output'])
                self.assertNotIn('STDERR_END', result['error'])
                self.assertEqual(len(result['output'].encode('utf-8')), 1024 * 1024)
                self.assertEqual(len(result['error']), 1024 * 1024)
            finally:
                if command.process.poll() is None:
                    command.cancel(force=True)
                command.dispose()
            self.assertEqual((logs / 'stdout.jsonl').read_bytes(), prompt + b'\n' + b'x' * size + b'STDOUT_END')
            self.assertEqual((logs / 'stderr.log').read_bytes(), b'y' * size + b'STDERR_END')


if __name__ == '__main__':
    unittest.main()
