"""Run one real Codex CLI episode in its isolated Windows guest workspace."""
import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import time
import zipfile

from guest_mcp_bridge import Command, interactive_session
from windows_codex_cleanup import JobProcessTracker


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def run(root):
    spec = json.loads((root / 'launch.json').read_text(encoding='utf-8'))
    record = {'status': 'starting', 'agent_backend': 'codex_cli', 'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'model': spec['model'], 'reasoning': spec['reasoning'], 'timeout_seconds': spec['timeout'],
              'gateway_task_id': spec['gateway_task_id'], 'cli_version_expected': spec['cli_version']}
    command = None
    tracker = None
    started = time.monotonic()
    try:
        if os.name != 'nt':
            raise RuntimeError('Production Codex episodes require a Windows guest')
        record['desktop_session'] = interactive_session()
        version = subprocess.run([spec['executable'], '--version'], capture_output=True, text=True,
                                 timeout=15, check=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
        record['cli_version_actual'] = version
        if version != 'codex-cli ' + spec['cli_version']:
            raise RuntimeError('Codex executable version differs from the selected version')
        workspace, home = root / 'workspace', root / 'codex-home'
        workspace.mkdir()
        home.mkdir()
        (home / 'config.toml').write_text(spec['config_toml'], encoding='utf-8')
        (root / 'prompt.txt').write_text(spec['instruction'], encoding='utf-8')
        logs = root / 'logs'
        logs.mkdir()
        environment = {key: value for key, value in os.environ.items()
                       if not any(marker in key.upper() for marker in ('TOKEN', 'SECRET', 'API_KEY'))
                       and not key.upper().startswith(('CODEX_', 'OPENAI_'))}
        environment['CODEX_HOME'] = str(home)
        environment['PATH'] = str(Path(spec['powershell_executable']).parent) + os.pathsep + environment.get('PATH', '')
        selected_shell = Path(spec['powershell_executable']).resolve(strict=True)
        detected_shell = shutil.which('pwsh', path=environment['PATH'])
        if not detected_shell or Path(detected_shell).resolve() != selected_shell:
            raise RuntimeError('Codex shell detection does not resolve the selected PowerShell')
        shell_probe = subprocess.run([str(selected_shell), '-NoLogo', '-NoProfile', '-Command',
            '@{version=$PSVersionTable.PSVersion.ToString();path=(Get-Process -Id $PID).Path}|ConvertTo-Json -Compress'],
            capture_output=True, text=True, check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        record['powershell_runtime'] = json.loads(shell_probe.stdout)
        if Path(record['powershell_runtime']['path']).resolve() != selected_shell:
            raise RuntimeError('PowerShell probe ran a different executable')
        record['powershell_executable'] = spec['powershell_executable']
        argv = [spec['executable'], 'exec', '--strict-config', '--skip-git-repo-check',
                '--color', 'never', '--json', '--cd', str(workspace),
                '--output-last-message', str(root / 'last-message.txt'), '-']
        record['argv'] = argv
        command = Command(argv, env=environment, stdin_path=root / 'prompt.txt', output_dir=logs)
        tracker = JobProcessTracker(command.job.handle, spec['codex_runtime_root'], spec['runtime_root'],
            [spec['powershell_executable'], os.path.join(os.environ['SystemRoot'], 'System32',
             'WindowsPowerShell', 'v1.0', 'powershell.exe'), os.environ.get('COMSPEC', '')],
             supervisor_pid=command.process.pid)
        record.update(status='running', supervisor_pid=command.process.pid)
        save(root / 'status.json', record)
        timed_out = cancelled = False
        while command.process.poll() is None:
            tracker.snapshot()
            cancelled = (root / 'cancel').exists()
            timed_out = time.monotonic() - started >= spec['timeout']
            if cancelled or timed_out:
                break
            time.sleep(0.25)
        # Never use Command.cancel here: terminating the entire job could
        # destroy a task application needed by the evaluator.
        record['process_cleanup'] = tracker.finish()
        record['cleanup_status'] = record['process_cleanup']['status']
        record['retain_guest'] = record['process_cleanup']['retain_guest']
        try:
            command.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            record.update(cleanup_status='unverified', retain_guest=True,
                          cleanup_error='Codex supervisor did not exit after runtime cleanup')
        record.update(returncode=command.process.poll(), timed_out=timed_out, cancelled=cancelled)
        record['status'] = ('timeout' if timed_out else 'cancelled' if cancelled
                            else 'completed' if record['returncode'] == 0 else 'cli_error')
        if (root / 'last-message.txt').exists():
            record['final_message'] = (root / 'last-message.txt').read_text(encoding='utf-8')
    except BaseException as error:
        record.update(status='error', error=f'{type(error).__name__}: {error}')
        if tracker is not None:
            try:
                record['process_cleanup'] = tracker.finish()
                record['cleanup_status'] = record['process_cleanup']['status']
                record['retain_guest'] = record['process_cleanup']['retain_guest']
            except Exception as cleanup:
                record.update(cleanup_status='unverified', retain_guest=True,
                              cleanup_error=f'{type(cleanup).__name__}: {cleanup}')
        elif command is not None:
            record.update(cleanup_status='unverified', retain_guest=True,
                          cleanup_error='Process tracker unavailable after Codex startup')
    finally:
        if command is not None:
            command.dispose()
        record['elapsed_seconds'] = time.monotonic() - started
        record['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save(root / 'status.json', record)
    return record


def archive(root):
    status = json.loads((root / 'status.json').read_text(encoding='utf-8'))
    if status['status'] in {'starting', 'running'} or status.get('retain_guest'):
        raise RuntimeError('Cannot seal evidence while Codex is still running')
    selected = [root / 'status.json', root / 'prompt.txt', root / 'last-message.txt']
    for directory in (root / 'logs', root / 'codex-home' / 'sessions'):
        if directory.exists():
            selected.extend(path for path in directory.rglob('*') if path.is_file())
    selected = [path for path in selected if path.exists()]
    if sum(path.stat().st_size for path in selected) > 256 * 1024 * 1024:
        raise RuntimeError('Codex evidence exceeds the archive limit; retain the guest')
    target = root / 'codex-evidence.zip'
    with zipfile.ZipFile(target, 'x', zipfile.ZIP_DEFLATED) as output:
        for path in selected:
            output.write(path, path.relative_to(root))
    return {'archive': str(target), 'files': len(selected), 'bytes': target.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'status', 'cancel', 'archive'])
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'run':
        result = run(args.root)
    elif args.action == 'cancel':
        (args.root / 'cancel').touch(exist_ok=True)
        result = {'cancel_requested': True}
    elif args.action == 'archive':
        result = archive(args.root)
    else:
        path = args.root / 'status.json'
        result = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'status': 'starting'}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
