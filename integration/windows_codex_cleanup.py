"""Conservative cleanup of a Codex Job; task application processes survive.

The caller owns the Job handle and must keep it open through finish(). This
module never sets Job limits or terminates the Job. Poll snapshot() while the
agent runs: ancestry missed between polls is deliberately left unverified.
"""
import ctypes
import ntpath
import os
import threading
import time
from ctypes import wintypes


def _path(value):
    return ntpath.normcase(ntpath.normpath(value)) if value else ''


def _under(path, root):
    return bool(root) and path.startswith(root.rstrip('\\') + '\\')


def classify_processes(records, codex_runtime_root, driver_runtime_root, shell_paths,
                       console_path=None, supervisor_identity=None):
    """Return roles keyed by (PID, creation FILETIME), without using image names."""
    roots = [_path(codex_runtime_root), _path(driver_runtime_root)]
    shells = {_path(path) for path in shell_paths}
    console_path = _path(console_path)
    by_id = {(p['pid'], p['creation_time']): p for p in records}
    protected = {key for key, p in by_id.items() if p.get('visible')}
    # Keep historical GUI ancestry, including children whose GUI parent exited.
    changed = True
    while changed:
        changed = False
        for key, p in by_id.items():
            if tuple(p.get('parent_identity') or ()) in protected and key not in protected:
                protected.add(key)
                changed = True
    roles = {}
    for key, p in by_id.items():
        runtime = any(_under(_path(p.get('path')), root) for root in roots)
        roles[key] = ('protected_runtime' if runtime else 'task_application') if key in protected else (
            'agent_runtime' if runtime else 'unknown')
        if key == supervisor_identity:
            roles[key] = 'protected_runtime' if key in protected else 'agent_supervisor'
        elif key not in protected and console_path and _path(p.get('path')) == console_path:
            # Even an overly broad runtime root cannot authorize killing conhost.
            roles[key] = 'unknown'
    changed = True
    while changed:
        changed = False
        for key, p in by_id.items():
            parent_role = roles.get(tuple(p.get('parent_identity') or ()))
            if roles[key] != 'unknown':
                continue
            if console_path and _path(p.get('path')) == console_path:
                if parent_role in {'agent_runtime', 'agent_command', 'agent_supervisor'}:
                    roles[key] = 'console_companion'
                    changed = True
            elif parent_role == 'agent_command' or (
                parent_role == 'agent_runtime' and _path(p.get('path')) in shells
            ):
                roles[key] = 'agent_command'
                changed = True
    return roles


class _Windows:
    """Small, explicitly typed Win32 boundary, also replaceable in unit tests."""
    def __init__(self):
        if os.name != 'nt':
            raise OSError('Windows Job cleanup requires Windows')
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        K = self.kernel
        signatures = {
            'QueryInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            'CreateToolhelp32Snapshot': ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            'Process32FirstW': ([wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
            'Process32NextW': ([wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
            'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
            'QueryFullProcessImageNameW': ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                           ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            'GetProcessTimes': ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            'IsProcessInJob': ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            'TerminateProcess': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            'GetSystemWindowsDirectoryW': ([wintypes.LPWSTR, wintypes.UINT], wintypes.UINT),
        }
        for name, (args, result) in signatures.items():
            getattr(K, name).argtypes, getattr(K, name).restype = args, result
        directory = ctypes.create_unicode_buffer(32768)
        length = self._check(K.GetSystemWindowsDirectoryW(directory, len(directory)))
        if length >= len(directory):
            raise RuntimeError('System Windows directory exceeds buffer')
        self.console_path = _path(ntpath.join(directory.value, 'System32', 'conhost.exe'))
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.user.EnumWindows.restype = wintypes.BOOL
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.restype = wintypes.BOOL
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD

    @staticmethod
    def _check(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())
        return value

    def pids(self, job):
        # JOBOBJECT_BASIC_PROCESS_ID_LIST has two DWORDs then ULONG_PTR[].
        capacity = 64
        while capacity <= 65536:
            class Pids(ctypes.Structure):
                _fields_ = [('assigned', wintypes.DWORD), ('count', wintypes.DWORD),
                            ('pids', ctypes.c_size_t * capacity)]
            data = Pids()
            ok = self.kernel.QueryInformationJobObject(job, 3, ctypes.byref(data), ctypes.sizeof(data), None)
            if ok and data.count <= capacity and data.assigned <= capacity:
                return set(data.pids[:data.count])
            if not ok and ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                self._check(ok)
            capacity *= 2
        raise RuntimeError('Job process enumeration exceeds bounded capacity')

    def parents(self):
        class Entry(ctypes.Structure):
            _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                        ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                        ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                        ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                        ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
        handle = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = Entry()
            entry.dwSize = ctypes.sizeof(entry)
            self._check(self.kernel.Process32FirstW(handle, ctypes.byref(entry)))
            result = {}
            while True:
                result[entry.th32ProcessID] = entry.th32ParentProcessID
                if not self.kernel.Process32NextW(handle, ctypes.byref(entry)):
                    if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                        raise ctypes.WinError(ctypes.get_last_error())
                    return result
        finally:
            self.kernel.CloseHandle(handle)

    def visible(self):
        result = set()
        errors = []
        @self.callback_type
        def visit(hwnd, _):
            if self.user.IsWindowVisible(hwnd):
                pid = wintypes.DWORD()
                if self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) and pid.value:
                    result.add(pid.value)
                else:
                    errors.append('Visible window process identity unavailable')
            return True
        self._check(self.user.EnumWindows(visit, 0))
        if errors:
            raise RuntimeError(errors[0])
        return result

    def _identity(self, handle):
        size = wintypes.DWORD(32768)
        path = ctypes.create_unicode_buffer(size.value)
        self._check(self.kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)))
        stamps = [wintypes.FILETIME() for _ in range(4)]
        self._check(self.kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in stamps)))
        return {'path': path.value, 'creation_time': (stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime}

    def identity(self, pid):
        handle = self._check(self.kernel.OpenProcess(0x1000, False, pid))
        try:
            return self._identity(handle)
        finally:
            self.kernel.CloseHandle(handle)

    def terminate(self, job, record):
        handle = self._check(self.kernel.OpenProcess(0x1000 | 0x0001 | 0x00100000, False, record['pid']))
        try:
            current = self._identity(handle)
            if current['creation_time'] != record['creation_time'] or _path(current['path']) != _path(record['path']):
                return 'identity_changed'
            member = wintypes.BOOL()
            self._check(self.kernel.IsProcessInJob(handle, job, ctypes.byref(member)))
            if not member.value or record['pid'] not in self.pids(job):
                return 'not_in_job'
            if record['pid'] in self.visible():
                return 'became_visible'
            self._check(self.kernel.TerminateProcess(handle, 1))
            waited = self.kernel.WaitForSingleObject(handle, 2000)
            return 'terminated' if waited == 0 else 'termination_unverified'
        finally:
            self.kernel.CloseHandle(handle)

    def wait_supervisor(self, job, record):
        return self._wait_owned(job, record, 3000, 'supervisor')

    def wait_companion(self, job, record, timeout_ms):
        return self._wait_owned(job, record, timeout_ms, 'console_companion')

    def _wait_owned(self, job, record, timeout_ms, role):
        """Wait on an exact process identity; never request termination rights."""
        if record['pid'] not in self.pids(job):
            return role + '_not_in_job'
        handle = self.kernel.OpenProcess(0x1000 | 0x00100000, False, record['pid'])
        if not handle:
            error = ctypes.get_last_error()
            if record['pid'] not in self.pids(job):
                return role + '_not_in_job'
            raise ctypes.WinError(error)
        try:
            current = self._identity(handle)
            if current['creation_time'] != record['creation_time'] or _path(current['path']) != _path(record['path']):
                return role + '_identity_changed'
            member = wintypes.BOOL()
            self._check(self.kernel.IsProcessInJob(handle, job, ctypes.byref(member)))
            if not member.value:
                return role + '_not_in_job'
            waited = self.kernel.WaitForSingleObject(handle, timeout_ms)
            if waited == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            return role + '_exited' if waited == 0 else role + '_wait_timeout'
        finally:
            self.kernel.CloseHandle(handle)


class JobProcessTracker:
    def __init__(self, job_handle, codex_runtime_root, driver_runtime_root, shell_paths, supervisor_pid=None):
        self.job_handle = job_handle
        self.codex_runtime_root = codex_runtime_root
        self.driver_runtime_root = driver_runtime_root
        self.shell_paths = tuple(shell_paths)
        self._api = _Windows()
        self._records = {}
        self._lock = threading.RLock()
        self._snapshot_number = 0
        self._supervisor = None
        if supervisor_pid is not None:
            if supervisor_pid not in self._api.pids(job_handle):
                raise RuntimeError('Supervisor does not belong to the selected Job')
            self._supervisor = dict(self._api.identity(supervisor_pid), pid=supervisor_pid,
                                    role='agent_supervisor')

    def snapshot(self):
        with self._lock:
            self._snapshot_number += 1
            now = time.time_ns() // 100 + 116444736000000000
            errors, current, process_errors = [], {}, {}
            departed = []
            try:
                owned = self._api.pids(self.job_handle)
                parents, visible = self._api.parents(), self._api.visible()
            except Exception as error:
                return {'verified': False, 'errors': [str(error)], 'processes': [], 'snapshot': self._snapshot_number}
            for pid in sorted(owned):
                try:
                    identity = self._api.identity(pid)
                    key = (pid, identity['creation_time'])
                    entry = self._records.setdefault(key, dict(identity, pid=pid, parent_pid=parents.get(pid),
                                                               first_seen=now, parent_identity=None, visible=False))
                    entry.update(last_seen=now, visible=entry['visible'] or pid in visible)
                    current[pid] = entry
                    if pid not in parents:
                        process_errors[pid] = f'PID {pid}: missing parent snapshot'
                except Exception as error:
                    process_errors[pid] = f'PID {pid}: {error}'
            if process_errors:
                try:
                    still_owned = self._api.pids(self.job_handle)
                    for pid, error in process_errors.items():
                        if pid in still_owned:
                            errors.append(error)
                        else:
                            # Normal supervisor exit can race an identity read.
                            # Only a fresh Job enumeration proves that it left.
                            departed.append({'pid': pid, 'observation_error': error})
                            current.pop(pid, None)
                except Exception as error:
                    errors.extend(process_errors.values())
                    errors.append(str(error))
            for entry in current.values():
                if entry['parent_identity'] is not None:
                    continue
                parent = current.get(entry['parent_pid'])
                candidates = [parent] if parent else [p for p in self._records.values()
                    if p['pid'] == entry['parent_pid'] and p['last_seen'] >= entry['creation_time']]
                candidates = [p for p in candidates if p and p['creation_time'] < entry['creation_time']]
                if len(candidates) == 1:
                    entry['parent_identity'] = [candidates[0]['pid'], candidates[0]['creation_time']]
            roles = classify_processes(self._records.values(), self.codex_runtime_root,
                                       self.driver_runtime_root, self.shell_paths, self._api.console_path,
                                       (self._supervisor['pid'], self._supervisor['creation_time'])
                                       if self._supervisor else None)
            for key, entry in self._records.items():
                entry['role'] = roles[key]
            return {'verified': not errors, 'errors': errors, 'snapshot': self._snapshot_number,
                    'departed_during_snapshot': departed,
                    'processes': [dict(entry) for entry in current.values()]}

    def finish(self):
        """Stop verified runtime/command processes; retain GUI and unknown members."""
        with self._lock:
            actions, errors = [], []
            # Bound rescans: a still-spawning process never leads to an endless cleanup.
            for _ in range(3):
                state = self.snapshot()
                if not state['verified']:
                    errors.extend(state['errors'])
                    break
                targets = [p for p in state['processes'] if p['role'] in {'agent_runtime', 'agent_command'}]
                if not targets:
                    break
                # Parents first stops command production; task app children are retained.
                targets.sort(key=lambda p: p['creation_time'])
                for target in targets:
                    fresh = self.snapshot()
                    if not fresh['verified']:
                        errors.extend(fresh['errors'])
                        break
                    match = next((p for p in fresh['processes'] if p['pid'] == target['pid']
                                  and p['creation_time'] == target['creation_time']), None)
                    if not match or match['role'] not in {'agent_runtime', 'agent_command'}:
                        continue
                    try:
                        outcome = self._api.terminate(self.job_handle, match)
                    except Exception as error:
                        outcome = 'error'
                        errors.append(f"PID {match['pid']}: {error}")
                    actions.append({'pid': match['pid'], 'creation_time': match['creation_time'], 'outcome': outcome})
                if errors:
                    break
            supervisor = dict(self._supervisor) if self._supervisor else None
            if supervisor is not None:
                try:
                    supervisor['wait_outcome'] = self._api.wait_supervisor(self.job_handle, supervisor)
                except Exception as error:
                    supervisor['wait_outcome'] = 'error'
                    errors.append(f"Supervisor PID {supervisor['pid']}: {error}")
            console_waits = []
            companions = self.snapshot()
            errors.extend(companions['errors'])
            deadline = time.monotonic() + 3
            for companion in companions['processes']:
                if companion['role'] != 'console_companion':
                    continue
                try:
                    outcome = self._api.wait_companion(self.job_handle, companion,
                                                       max(0, int((deadline - time.monotonic()) * 1000)))
                except Exception as error:
                    outcome = 'error'
                    errors.append(f"Console PID {companion['pid']}: {error}")
                console_waits.append({'pid': companion['pid'], 'creation_time': companion['creation_time'],
                                      'outcome': outcome})
            final = self.snapshot()
            errors.extend(final['errors'])
            retained = final['processes']
            verified = final['verified'] and not errors and all(p['role'] == 'task_application' for p in retained)
            return {'status': ('agent_runtime_stopped_with_task_apps_retained' if retained else 'agent_runtime_stopped')
                    if verified else 'unverified', 'retain_guest': not verified,
                    'processes': [dict(p) for p in self._records.values()], 'remaining': retained,
                    'supervisor': supervisor, 'console_waits': console_waits,
                    'actions': actions, 'errors': errors, 'snapshots': self._snapshot_number}
