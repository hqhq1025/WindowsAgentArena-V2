"""Host control for a real guest Codex process; contains no model request loop."""
import io
import json
import ntpath
from pathlib import Path
import time
import urllib.parse
import urllib.request
import zipfile

from gateway_config import validate_model_url

COMPLETION_CONTRACT = '''At the end, briefly describe the observed result. Put exactly one of these markers on the last line:
WAA_STATUS: done
WAA_STATUS: infeasible
WAA_STATUS: failed
Use done only after checking the requested observable result. Use infeasible only when the original request cannot be achieved in the provided environment; explain the reason. Use failed when you did not complete the task, including when time or tool limits prevent completion. These are your declarations, not independent verification.'''


def toml_string(value):
    return json.dumps(value, ensure_ascii=False)


def load_codex_config(path):
    config = json.loads(Path(path).read_text())
    required = {'configuration_id', 'executable', 'version', 'powershell_executable',
                'guest_gateway_base_url', 'instructions', 'runtime_roots', 'codex_runtime_root'}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError('Codex config must explicitly bind executable, version, shell, guest gateway, instructions, and runtime roots')
    for name in ('executable', 'powershell_executable', 'codex_runtime_root'):
        if not isinstance(config[name], str) or not ntpath.isabs(config[name]):
            raise ValueError(name + ' must be an absolute guest path')
    validate_model_url(config['guest_gateway_base_url'])
    if not config['guest_gateway_base_url'].rstrip('/').endswith('/v1'):
        raise ValueError('Guest gateway must be a /v1 base URL')
    if not config['configuration_id'] or not config['version']:
        raise ValueError('Codex version and configuration identity must be nonempty')
    for name in ('instructions', 'runtime_roots'):
        if not isinstance(config[name], dict) or set(config[name]) != {'hcu', 'cua'}:
            raise ValueError(name + ' must explicitly bind both hcu and cua')
    for arm in ('hcu', 'cua'):
        if not isinstance(config['instructions'][arm], list) or any(
                not isinstance(p, str) or not Path(p).is_file() for p in config['instructions'][arm]):
            raise ValueError('Instructions must be an explicit list of existing files')
        if not isinstance(config['runtime_roots'][arm], str) or not ntpath.isabs(config['runtime_roots'][arm]):
            raise ValueError('Driver runtime roots must be absolute guest paths')
    return config


def build_config(model, reasoning, model_url, driver, instructions):
    lines = [f'model = {toml_string(model)}', 'model_provider = "weavebench"',
             f'model_reasoning_effort = {toml_string(reasoning)}', 'approval_policy = "never"',
             'sandbox_mode = "danger-full-access"', 'web_search = "disabled"', 'allow_login_shell = false',
             'project_doc_max_bytes = 0', f'developer_instructions = {toml_string(instructions)}',
             '[features]', 'apps = false', 'plugins = false', 'unbounded_connection_retries = false',
             'mcp_2026_07_28 = false',
             '[model_providers.weavebench]', 'name = "WeaveBench"',
             f'base_url = {toml_string(model_url)}', 'wire_api = "responses"',
             'requires_openai_auth = false', 'supports_websockets = false',
             'request_max_retries = 0', 'stream_max_retries = 0', 'stream_idle_timeout_ms = 270000',
             '[model_providers.weavebench.capabilities]', 'remote_compaction = "unsupported"',
             '[mcp_servers.computer_use]', f'command = {toml_string(driver["command"][0])}',
             f'args = {toml_string(driver["command"][1:])}', 'required = true',
             'startup_timeout_sec = 60', 'tool_timeout_sec = 180']
    if driver.get('cwd'):
        lines.append(f'cwd = {toml_string(driver["cwd"])}')
    if driver.get('env'):
        lines.append('[mcp_servers.computer_use.env]')
        lines.extend(f'{toml_string(key)} = {toml_string(value)}' for key, value in sorted(driver['env'].items()))
    return '\n'.join(lines) + '\n'


def gateway_get(config, route):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(config['base_url'].removesuffix('/v1') + route, timeout=10) as response:
        return json.load(response)


def require_budget(config, identity, *, fresh=False):
    state = gateway_get(config, '/_weavebench/task-budget?' + urllib.parse.urlencode({'task_id': identity}))
    expected = {'enabled': True, 'limit': 100, 'unit': 'responses_completed_response',
                'counted_statuses': ['completed'], 'request_limit': 400,
                'consecutive_failure_limit': 4, 'relay_retries': 0, 'persistent': True, 'writable': True}
    if any(state.get(key) != value for key, value in expected.items()):
        raise RuntimeError('Gateway does not provide the selected completed-response budget')
    if state.get('task_id') != identity:
        raise RuntimeError('Gateway returned a different canonical task identity')
    if fresh and (state['requests_admitted'] or state['completed'] or state['reserved'] or
                  state.get('unknown_reservations') or state['circuit_open']):
        raise RuntimeError('New Codex episode already has gateway request history')
    return state


def require_same_relay(host_health, guest_health, host_budget, guest_budget):
    for field in ('version', 'configuration_id', 'instance_id', 'ledger_id'):
        if not host_health.get(field) or host_health[field] != guest_health.get(field):
            raise RuntimeError('Guest and host relay differ: ' + field)
    if host_budget != guest_budget:
        raise RuntimeError('Guest and host canonical budget views differ')


def extract_evidence(data, target):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(item.file_size for item in archive.infolist()) > 256 * 1024 * 1024:
            raise ValueError('Expanded Codex evidence exceeds the archive limit')
        for item in archive.infolist():
            path = Path(item.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in item.filename:
                raise ValueError('Invalid Codex evidence member')
        target.mkdir()
        archive.extractall(target)


def run_guest_codex(args, arm, driver, task, controller, folder):
    from vm import execute, upload
    from waa_compare import gateway_episode_route, write_json

    config = args.codex
    base, identity = gateway_episode_route(config['guest_gateway_base_url'], args.run_id, arm, task['_selection_id'])
    before = require_budget(args.gateway, identity, fresh=True)
    host_health = gateway_get(args.gateway, '/_weavebench/relay')
    root = 'C:/waa-codex-episodes/' + args.run_id + '-' + arm + '-' + task['_selection_id']
    instructions = '\n\n'.join([*(Path(path).read_text() for path in config['instructions'][arm]), COMPLETION_CONTRACT])
    spec = {'executable': config['executable'], 'cli_version': config['version'],
            'powershell_executable': config['powershell_executable'], 'instruction': task['instruction'],
            'model': args.model, 'reasoning': args.reasoning, 'timeout': args.timeout,
            'gateway_task_id': identity, 'runtime_root': config['runtime_roots'][arm],
            'codex_runtime_root': config['codex_runtime_root'],
            'config_toml': build_config(args.model, args.reasoning, base, driver, instructions)}
    result = {'agent_backend': 'codex_cli', 'guest_root': root, 'gateway_task_id': identity,
              'cli_version': config['version'], 'model_base_url': base, 'budget_before': before}
    write_json(folder / 'codex-launch.json', spec)
    write_json(folder / 'codex-budget-before.json', before)
    url = controller.http_server

    def invoke(action):
        code = ('import subprocess,sys,json; p=subprocess.run([sys.executable,' + repr(root + '/guest_codex.py') + ','
                + repr(action) + ',"--root",' + repr(root) + '],capture_output=True,text=True,timeout=45,'
                'creationflags=subprocess.CREATE_NO_WINDOW); '
                'print(json.dumps({"returncode":p.returncode,"stdout":p.stdout,"stderr":p.stderr}))')
        envelope = json.loads(execute(url, code)['output'])
        with (folder / 'codex-control.jsonl').open('a') as stream:
            stream.write(json.dumps({'action': action, **envelope}) + '\n')
        if envelope['returncode']:
            raise RuntimeError('Guest Codex ' + action + ' failed: ' + envelope['stderr'])
        return json.loads(envelope['stdout'])

    started = False
    try:
        relay_root = config['guest_gateway_base_url'].removesuffix('/v1')
        guest_network = execute(url, 'import urllib.request,json; '
            'o=urllib.request.build_opener(urllib.request.ProxyHandler({})); '
            'urls=' + repr([relay_root + '/_weavebench/relay', relay_root + '/_weavebench/task-budget?' +
                          urllib.parse.urlencode({'task_id': identity})]) + '; '
            'print(json.dumps([json.load(o.open(u,timeout=10)) for u in urls]))')
        guest_health, guest_budget = json.loads(guest_network['output'])
        require_same_relay(host_health, guest_health, before, guest_budget)
        write_json(folder / 'codex-relay-binding.json', {'host': host_health, 'guest': guest_health,
                                                       'task_id': identity, 'budget': guest_budget})
        # Check every ancestor explicitly. Fresh CODEX_HOME alone does not stop
        # Codex from reading project instructions in a parent directory.
        preflight = execute(url, 'import pathlib,json; p=pathlib.Path(' + repr(root) + '); '
            'assert not p.exists(), "Episode guest directory exists"; '
            'bad=[str(q/name) for q in [p,*p.parents] for name in ("AGENTS.md","AGENTS.override.md") if (q/name).exists()]; '
            'assert not bad, "Parent project instructions exist: "+repr(bad); '
            'p.mkdir(parents=True); print(json.dumps({"parents_checked":True,"fresh_root":str(p)}))')
        write_json(folder / 'codex-guest-preflight.json', json.loads(preflight['output']))
        for name in ('guest_codex.py', 'guest_mcp_bridge.py', 'windows_codex_cleanup.py'):
            upload(url, Path(__file__).with_name(name), root + '/' + name)
        upload(url, folder / 'codex-launch.json', root + '/launch.json')
        code = ('import subprocess,sys,pathlib,json; p=pathlib.Path(' + repr(root) + '); '
            'log=(p/"launcher.log").open("xb"); '
            'child=subprocess.Popen([sys.executable,str(p/"guest_codex.py"),"run","--root",str(p)],'
            'stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW); '
            'print(json.dumps({"launcher_pid":child.pid}))')
        result.update(json.loads(execute(url, code)['output']))
        started = True
        deadline = time.monotonic() + args.timeout + 60
        while time.monotonic() < deadline:
            state = invoke('status')
            if state['status'] not in ('starting', 'running'):
                break
            time.sleep(5)
        else:
            invoke('cancel')
            raise TimeoutError('Guest Codex supervisor did not publish its terminal status')
        result.update(state)
        marker = state.get('final_message', '').strip().splitlines()
        declaration = marker[-1].removeprefix('WAA_STATUS: ') if marker else None
        result['completion_declaration'] = declaration if declaration in {'done', 'infeasible', 'failed'} else None
        after = require_budget(args.gateway, identity)
        result.update(budget_after=after, successful_model_responses=after['completed'],
                      model_requests=after['requests_admitted'])
        write_json(folder / 'codex-budget-after.json', after)
        if state.get('retain_guest') or state.get('cleanup_status') not in {
                'agent_runtime_stopped', 'agent_runtime_stopped_with_task_apps_retained'}:
            result.update(retain_guest=True, mcp_cleanup_error=state.get('cleanup_error', 'Codex process cleanup unverified'))
            write_json(folder / 'codex-result.json', result)
            return result
        sealed = invoke('archive')
        data = controller.get_file(sealed['archive'])
        if data is None or len(data) != sealed['bytes']:
            raise RuntimeError('Codex archive download incomplete')
        (folder / 'codex-evidence.zip').write_bytes(data)
        extract_evidence(data, folder / 'codex')
        from codex_metrics import extract_metrics
        result['codex_metrics'] = extract_metrics(folder / 'codex', gateway_budget=after)
        write_json(folder / 'codex-metrics.json', result['codex_metrics'])
        if state['status'] == 'completed' and after['completed'] < 1:
            result.update(status='codex_infrastructure_error', error_kind='codex_infrastructure', retain_guest=True,
                          error='Codex exited successfully without a completed model response')
        if after['reserved'] or after['unknown_reservations']:
            result.update(status='gateway_unresolved', error_kind='routing', retain_guest=True)
        if after['completed'] == 100 and state['status'] == 'cli_error':
            result['status'] = 'response_budget'
        if after['circuit_open'] and state['status'] not in {'cancelled', 'interrupted'}:
            result.update(status='gateway_circuit_open', error_kind='routing')
        if state.get('cleanup_status') not in {'agent_runtime_stopped', 'agent_runtime_stopped_with_task_apps_retained'}:
            result['mcp_cleanup_error'] = state.get('cleanup_error', 'Codex process cleanup failed')
            result['retain_guest'] = True
    except BaseException as error:
        if started:
            try:
                invoke('cancel')
            except Exception as cancel_error:
                result['cancel_error'] = f'{type(cancel_error).__name__}: {cancel_error}'
        result.update(status='codex_infrastructure_error', error_kind='codex_infrastructure', retain_guest=True,
                      error=f'{type(error).__name__}: {error}')
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            result['status'] = 'interrupted'
    write_json(folder / 'codex-result.json', result)
    return result
