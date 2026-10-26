"""Fixtures follow the release-tagged Codex 0.164 serde event definitions."""
import json
from pathlib import Path
import tempfile
import unittest

from codex_metrics import extract_metrics


def record(kind, payload):
    return {'timestamp': '2026-10-26T12:00:00.000Z', 'type': kind, 'payload': payload}


def usage(input_tokens=100, **changes):
    result = {'input_tokens': input_tokens, 'cached_input_tokens': 20, 'output_tokens': 30,
              'reasoning_output_tokens': 10, 'cache_write_input_tokens': 0, 'total_tokens': input_tokens + 30}
    result.update(changes)
    return result


class CodexMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, path, events):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(''.join(json.dumps(event) + '\n' for event in events), encoding='utf-8')

    def rollout(self, events, filename='rollout.jsonl'):
        self.write('codex-home/sessions/2026/10/26/' + filename,
                   [record('session_meta', {'id': 'thread-1'})] + events)

    def test_rollout_calls_are_deduplicated_against_native_and_stdout_mirrors(self):
        call = record('response_item', {'type': 'function_call', 'name': 'mcp__computer_use__AX',
                    'arguments': '{}', 'call_id': 'call-1'})
        end = record('event_msg', {'type': 'mcp_tool_call_end', 'call_id': 'call-1',
            'turn_id': 'turn-1', 'invocation': {'server': 'computer_use', 'tool': 'AX', 'arguments': {}},
            'duration': {'secs': 2, 'nanos': 500000000},
            'result': {'Ok': {'content': [], 'isError': True}}})
        self.rollout([call, end, call, end])
        self.write('logs/stdout.jsonl', [
            {'type': kind, 'item': {'id': 'item_0', 'type': 'mcp_tool_call', 'server': 'computer_use',
              'tool': 'AX', 'arguments': {}, 'status': state, 'result': None,
              'error': {'message': 'failed'} if state == 'failed' else None}}
            for kind, state in [('item.started', 'in_progress'), ('item.completed', 'failed')]])
        result = extract_metrics(self.root)
        self.assertEqual(result['tool_count'], 1)
        self.assertEqual(result['tool_counts']['AX'], 1)
        self.assertEqual(result['tool_error_count'], 1)
        self.assertEqual(result['tool_duration_seconds_sum'], 2.5)

    def test_usage_uses_last_cumulative_total_and_gateway_not_cli_turns(self):
        self.rollout([record('event_msg', {'type': 'token_count', 'info': {
            'total_token_usage': usage(n), 'last_token_usage': usage(5), 'model_context_window': None}})
            for n in (100, 170)])
        self.write('logs/stdout.jsonl', [{'type': 'turn.completed', 'usage': usage(n)} for n in (100, 170)])
        self.write('status.json', [{'status': 'completed', 'returncode': 0, 'elapsed_seconds': 9.1,
                                    'gateway_task_id': '["waa","run","hcu","task"]'}])
        result = extract_metrics(self.root, {'completed': 7, 'requests_admitted': 8})
        self.assertEqual(result['usage']['input_tokens'], 170)
        self.assertEqual(result['successful_model_responses'], 7)
        self.assertEqual(result['cli_completed_turns'], 2)
        self.assertIsNone(result['task_success'])
        self.assertEqual(result['cli_exit_code'], 0)

    def test_absent_usage_and_zero_cli_default_stay_unknown(self):
        self.write('logs/stdout.jsonl', [{'type': 'turn.completed', 'usage': {field: 0 for field in usage()}}])
        result = extract_metrics(self.root)
        self.assertTrue(all(value is None for value in result['usage'].values()))
        self.assertIsNone(result['successful_model_responses'])
        self.rollout([record('event_msg', {'type': 'token_count', 'info': None})])
        self.assertIsNone(extract_metrics(self.root)['usage']['input_tokens'])

    def test_missing_usage_field_is_null_not_zero(self):
        partial = usage()
        del partial['reasoning_output_tokens']
        self.rollout([record('event_msg', {'type': 'token_count', 'info': {'total_token_usage': partial}})])
        result = extract_metrics(self.root)
        self.assertEqual(result['usage']['output_tokens'], 30)
        self.assertIsNone(result['usage']['reasoning_output_tokens'])

    def test_native_shell_header_and_paginated_mcp_duration(self):
        self.rollout([
            record('response_item', {'type': 'function_call', 'call_id': 'shell-1', 'name': 'functions.exec_command', 'arguments': '{}'}),
            record('response_item', {'type': 'function_call_output', 'call_id': 'shell-1',
                'output': 'Chunk ID: 123\nWall time: 1.2000 seconds\nProcess exited with code 2\nFinal output:\nWall time: 999 seconds'}),
            record('response_item', {'type': 'function_call', 'call_id': 'gui-1', 'name': 'mcp__computer_use__GUI', 'arguments': '{}'}),
            record('event_msg', {'type': 'item_completed', 'thread_id': 'thread-1', 'turn_id': 'turn-1',
                'completed_at_ms': 1000, 'item': {'type': 'McpToolCall', 'id': 'gui-1',
                'server': 'computer_use', 'tool': 'GUI', 'arguments': {}, 'status': 'completed',
                'result': {'content': [], 'isError': False}, 'duration': {'secs': 0, 'nanos': 500000000}}}),
            record('response_item', {'type': 'custom_tool_call', 'call_id': 'patch-1', 'name': 'apply_patch', 'input': 'patch'}),
        ])
        result = extract_metrics(self.root)
        self.assertEqual(result['tool_counts'], {'AX': 0, 'GUI': 1, 'native_shell': 1, 'other': 1})
        self.assertEqual(result['tool_duration_seconds_sum'], 1.7)
        self.assertEqual(result['tool_error_count'], 1)
        self.assertEqual(result['tool_error_unknown_count'], 1)

    def test_stdout_fallback_deduplicates_lifecycle_but_has_no_invented_duration(self):
        self.write('logs/stdout.jsonl', [
            {'type': kind, 'item': {'id': 'item_0', 'type': 'command_execution', 'command': 'exit 1',
                'aggregated_output': '', 'exit_code': code, 'status': state}}
            for kind, state, code in [('item.started', 'in_progress', None), ('item.completed', 'failed', 1)]]
            + [{'type': 'turn.failed', 'error': {'message': 'upstream stopped'}}])
        result = extract_metrics(self.root)
        self.assertEqual(result['tool_count'], 1)
        self.assertEqual(result['tool_error_count'], 1)
        self.assertEqual(result['tool_count_source'], 'stdout')
        self.assertIsNone(result['tool_duration_seconds_sum'])
        self.assertEqual(result['cli_terminal_event'], 'turn.failed')

    def test_shell_output_content_array_retains_codex_header_metrics(self):
        self.rollout([
            record('response_item', {'type': 'function_call', 'call_id': 'c1', 'name': 'shell_command', 'arguments': '{}'}),
            record('response_item', {'type': 'function_call_output', 'call_id': 'c1', 'output': [
                {'type': 'input_text', 'text': 'Exit code: 0\nWall time: 0.4 seconds\nOutput:\nExit code: 9'}]}),
        ])
        result = extract_metrics(self.root)
        self.assertEqual(result['tool_duration_seconds_sum'], 0.4)
        self.assertEqual(result['tool_error_count'], 0)
        self.assertEqual(result['tool_error_unknown_count'], 0)

    def test_truncated_record_preserves_metrics_and_warns(self):
        self.rollout([record('response_item', {'type': 'function_call', 'name': 'unknown', 'call_id': 'c1', 'arguments': '{}'})])
        path = self.root / 'codex-home/sessions/2026/10/26/rollout.jsonl'
        with path.open('a') as output:
            output.write('{"type":')
        result = extract_metrics(self.root)
        self.assertEqual(result['tool_count'], 1)
        self.assertEqual(len(result['warnings']), 1)

    def test_gateway_route_mismatch_is_rejected(self):
        self.write('status.json', [{'gateway_task_id': 'route-1'}])
        with self.assertRaisesRegex(ValueError, 'different task route'):
            extract_metrics(self.root, {'task_id': 'route-2', 'completed': 1})


if __name__ == '__main__':
    unittest.main()
