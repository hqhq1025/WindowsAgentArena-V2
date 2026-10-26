"""Read Codex 0.164 exec/rollout evidence without interpreting task success.

Formats: codex-rs/{exec/src/exec_events.rs,protocol/src/{models,items,protocol}.rs}.
Rollout calls are authoritative; exec item IDs are not model call IDs. Usage is
the last cumulative total per thread, never the sum of CLI turn.completed totals.
"""
import json
import math
from pathlib import Path
import re


USAGE_FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                'output_tokens', 'reasoning_output_tokens', 'total_tokens')
SHELL_NAMES = {'shell', 'shell_command', 'exec_command', 'write_stdin', 'local_shell'}
TOOL_ITEMS = {'command_execution', 'mcp_tool_call', 'file_change', 'collab_tool_call',
              'web_search', 'CommandExecution', 'McpToolCall', 'FileChange',
              'CollabAgentToolCall', 'DynamicToolCall', 'ImageView', 'WebSearch'}


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def duration(value):
    if not isinstance(value, dict):
        return None
    seconds, nanos = number(value.get('secs')), number(value.get('nanos'))
    return seconds + nanos / 1e9 if seconds is not None and nanos is not None else None


def category(name, kind=None):
    leaf = name.split('__')[-1].split('.')[-1]
    if leaf in {'AX', 'GUI'}:
        return leaf
    if leaf in SHELL_NAMES or kind in {'local_shell_call', 'command_execution', 'CommandExecution'}:
        return 'native_shell'
    return 'other'


def output_evidence(output, call):
    """Use typed results or Codex's prefix, never error words in command output."""
    if isinstance(output, list):
        if output and isinstance(output[0], dict) and output[0].get('type') in {'input_text', 'text'}:
            output_evidence(output[0].get('text'), call)
        return
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except (ValueError, TypeError):
            # Prefix ends before arbitrary program output, which may itself
            # contain fake 'Wall time' or exit-code lines.
            prefix = output.split('\nOutput:', 1)[0].split('\nFinal output:', 1)[0]
            if re.match(r'^(?:(?:Chunk ID: [^\n]+|Exit code: -?\d+)\n)?Wall time: ', prefix):
                match = re.search(r'^Wall time: ([0-9.]+) seconds$', prefix, re.M)
                if match:
                    call['duration_seconds'] = number(float(match[1]))
                    call['duration_source'] = 'codex_output_prefix'
                if call['category'] == 'native_shell':
                    match = re.search(r'^(?:Process exited with code |Exit code: )(-?\d+)$', prefix, re.M)
                    if match:
                        call['error'] = int(match[1]) != 0
            return
    if isinstance(output, dict):
        if isinstance(output.get('isError'), bool):
            call['error'] = output['isError']
        metadata = output.get('metadata')
        if call['category'] == 'native_shell' and isinstance(metadata, dict):
            if isinstance(metadata.get('exit_code'), int):
                call['error'] = metadata['exit_code'] != 0
            elapsed = number(metadata.get('duration_seconds'))
            if elapsed is not None:
                call.update(duration_seconds=elapsed, duration_source='codex_output_metadata')


def finish(call, item):
    if item.get('status') in {'failed', 'declined'} or item.get('error'):
        call['error'] = True
    elif item.get('status') == 'completed':
        call['error'] = False
    exit_code = item.get('exit_code', item.get('exitCode'))
    if isinstance(exit_code, int):
        call['error'] = exit_code != 0
    result = item.get('result')
    if isinstance(result, dict):
        if 'Err' in result:
            call['error'] = True
        else:
            output_evidence(result.get('Ok', result), call)
    elapsed = duration(item.get('duration'))
    if elapsed is not None:
        call.update(duration_seconds=elapsed, duration_source='native_duration')


def extract_metrics(evidence_dir, gateway_budget=None):
    """Return JSON-safe metrics; gateway_budget must describe this episode's route.

    Missing evidence stays unknown. CLI completion and exit status never produce
    a task PASS. Call durations are a sum of measured intervals, not wall time.
    """
    root = Path(evidence_dir)
    warnings, calls, stdout_calls, thread_usage = [], {}, {}, {}
    rollout_files = sorted((root / 'codex-home' / 'sessions').rglob('*.jsonl'))
    status_path = root / 'status.json'
    status = json.loads(status_path.read_text(encoding='utf-8')) if status_path.exists() else {}

    def records(path):
        if not path.exists():
            return
        with path.open(encoding='utf-8') as source:
            for line_no, line in enumerate(source, 1):
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError('not an object')
                    yield value
                except ValueError:
                    warnings.append(f'{path.relative_to(root)}:{line_no}: invalid JSON record')

    def get_call(table, key, name, kind=None):
        return table.setdefault(key, {'call_id': key[1], 'thread_id': key[0], 'name': name,
            'category': category(name, kind), 'error': None, 'duration_seconds': None, 'duration_source': None})

    for path in rollout_files:
        thread = str(path.relative_to(root))
        for event in records(path):
            payload = event.get('payload')
            if not isinstance(payload, dict):
                continue
            kind = payload.get('type')
            if event.get('type') == 'session_meta':
                thread = payload.get('id', thread)
            elif event.get('type') == 'response_item':
                call_id = payload.get('call_id')
                if kind in {'function_call', 'custom_tool_call', 'local_shell_call', 'tool_search_call', 'web_search_call'}:
                    call_id = call_id or payload.get('id')
                    if not call_id:
                        warnings.append('Rollout tool call without call_id or id')
                        continue
                    get_call(calls, (thread, call_id), payload.get('name', kind), kind)
                elif kind in {'function_call_output', 'custom_tool_call_output'} and (thread, call_id) in calls:
                    output_evidence(payload.get('output'), calls[(thread, call_id)])
            elif event.get('type') == 'event_msg':
                if kind == 'token_count':
                    info = payload.get('info')
                    if isinstance(info, dict) and isinstance(info.get('total_token_usage'), dict):
                        thread_usage[thread] = info['total_token_usage']
                elif kind in {'mcp_tool_call_begin', 'mcp_tool_call_end', 'exec_command_begin', 'exec_command_end'}:
                    call_id = payload.get('call_id')
                    if not call_id:
                        continue
                    invocation = payload.get('invocation', {})
                    name = invocation.get('tool', 'exec_command')
                    call = get_call(calls, (thread, call_id), name)
                    if kind.endswith('_end'):
                        finish(call, payload)
                elif kind == 'item_completed':
                    item = payload.get('item', {})
                    if item.get('type') in TOOL_ITEMS and item.get('id'):
                        call = get_call(calls, (thread, item['id']), item.get('tool', item['type']), item['type'])
                        finish(call, item)

    cli_terminal, cli_turns, cli_errors, stdout_usage = None, 0, [], {}
    thread = 'stdout'
    stdout_path = root / 'logs' / 'stdout.jsonl'
    for event in records(stdout_path):
        kind = event.get('type')
        if kind == 'thread.started':
            thread = event.get('thread_id', thread)
        elif kind in {'turn.completed', 'turn.failed'}:
            cli_terminal = kind
            if kind == 'turn.completed':
                cli_turns += 1
                stdout_usage[thread] = event.get('usage', {})
            else:
                cli_errors.append(event.get('error'))
        elif kind == 'error':
            cli_errors.append(event.get('message'))
        elif kind in {'item.started', 'item.updated', 'item.completed'}:
            item = event.get('item', {})
            if item.get('type') in TOOL_ITEMS and item.get('id'):
                call = get_call(stdout_calls, (thread, item['id']), item.get('tool', item['type']), item['type'])
                if kind == 'item.completed':
                    finish(call, item)
            elif kind == 'item.completed' and item.get('type') == 'error':
                cli_errors.append(item.get('message'))

    if rollout_files:
        tool_source = 'rollout'
        if len(stdout_calls) > len(calls):
            warnings.append('stdout has more tool items than rollout; counts use rollout only and may be incomplete')
    else:
        calls, tool_source = stdout_calls, 'stdout' if stdout_path.exists() else None
    usage_source = 'rollout_cumulative' if thread_usage else None
    if not thread_usage and not rollout_files and stdout_usage:
        # The CLI emits zero defaults when no token notification arrived.
        # All-zero stdout alone therefore cannot establish measured zero usage.
        thread_usage = {key: value for key, value in stdout_usage.items()
                        if any(number(value.get(field)) not in (None, 0) for field in USAGE_FIELDS)}
        if thread_usage:
            usage_source = 'stdout_cumulative'
    usage = {}
    for field in USAGE_FIELDS:
        values = [number(value.get(field)) for value in thread_usage.values()]
        usage[field] = sum(values) if values and all(value is not None for value in values) else None
    counts = {name: sum(call['category'] == name for call in calls.values())
              for name in ('AX', 'GUI', 'native_shell', 'other')}
    measured = [call['duration_seconds'] for call in calls.values() if call['duration_seconds'] is not None]
    completed = requests = None
    if gateway_budget is not None:
        identity = status.get('gateway_task_id')
        if identity and gateway_budget.get('task_id') and identity != gateway_budget['task_id']:
            raise ValueError('Gateway budget belongs to a different task route')
        completed = gateway_budget.get('completed')
        requests = gateway_budget.get('requests_admitted')
    return {'schema_revision': 'codex-native-metrics-v1',
        'cli_status': status.get('status'), 'cli_exit_code': status.get('returncode'),
        'cli_terminal_event': cli_terminal, 'cli_completed_turns': cli_turns,
        'task_success': None, 'successful_model_responses': completed, 'model_requests': requests,
        'model_response_count_source': 'gateway_budget' if gateway_budget is not None else None,
        'usage': usage, 'usage_source': usage_source, 'tool_count': len(calls),
        'tool_counts': counts, 'tool_count_source': tool_source,
        'tool_error_count': sum(call['error'] is True for call in calls.values()),
        'tool_error_unknown_count': sum(call['error'] is None for call in calls.values()),
        'tool_duration_seconds_sum': sum(measured) if measured else None,
        'tool_duration_measured_count': len(measured), 'tool_calls': list(calls.values()),
        'elapsed_seconds': number(status.get('elapsed_seconds')), 'cli_errors': cli_errors,
        'rollout_files': [str(path.relative_to(root)) for path in rollout_files], 'warnings': warnings}
