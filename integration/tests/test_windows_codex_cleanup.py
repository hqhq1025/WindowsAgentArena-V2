import ctypes
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows_codex_cleanup import JobProcessTracker, classify_processes, _Windows


CODEX = r'C:\runtime\codex'
DRIVER = r'C:\runtime\driver'
SHELL = r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'


def process(pid, path, parent=None, visible=False, creation=None):
    return dict(pid=pid, path=path, creation_time=creation or pid * 100,
                parent_identity=parent, visible=visible)


class ClassificationTests(unittest.TestCase):
    def roles(self, records):
        return classify_processes(records, CODEX, DRIVER, [SHELL])

    def test_runtime_shell_and_command_descendants(self):
        records = [process(1, CODEX + r'\bin\codex.exe'),
                   process(2, SHELL, [1, 100]),
                   process(3, r'C:\Python\python.exe', [2, 200]),
                   process(4, DRIVER + r'\native\provider.exe', [1, 100])]
        self.assertEqual(list(self.roles(records).values()),
                         ['agent_runtime', 'agent_command', 'agent_command', 'agent_runtime'])

    def test_gui_and_descendants_survive_shell_ancestry(self):
        records = [process(1, CODEX + r'\codex.exe'), process(2, SHELL, [1, 100]),
                   process(3, r'C:\Apps\editor.exe', [2, 200], True),
                   process(4, r'C:\Apps\helper.exe', [3, 300])]
        roles = self.roles(records)
        self.assertEqual(roles[(3, 300)], 'task_application')
        self.assertEqual(roles[(4, 400)], 'task_application')
        self.assertEqual(roles[(2, 200)], 'agent_command')

    def test_names_prefixes_and_unproven_ancestry_are_unknown(self):
        records = [process(1, r'C:\other\codex.exe'),
                   process(2, CODEX + r'-old\codex.exe'),
                   process(3, SHELL), process(4, r'C:\Apps\worker.exe', [3, 999])]
        self.assertEqual(set(self.roles(records).values()), {'unknown'})

    def test_visible_runtime_cannot_claim_runtime_stopped(self):
        self.assertEqual(self.roles([process(1, CODEX + r'\codex.exe', visible=True)])[(1, 100)],
                         'protected_runtime')

    def test_pid_reuse_does_not_inherit_old_command_role(self):
        records = [process(1, CODEX + r'\codex.exe'), process(2, SHELL, [1, 100]),
                   process(2, r'C:\Apps\other.exe', creation=500),
                   process(3, r'C:\Apps\child.exe', [2, 500], creation=600)]
        self.assertEqual(self.roles(records)[(3, 600)], 'unknown')


class FakeWindows:
    def __init__(self):
        self.live = {1: dict(path=CODEX + r'\codex.exe', creation_time=100),
                     2: dict(path=SHELL, creation_time=200),
                     3: dict(path=r'C:\Apps\editor.exe', creation_time=300),
                     4: dict(path=r'C:\Apps\helper.exe', creation_time=400)}
        self.parent_map = {1: 0, 2: 1, 3: 2, 4: 3}
        self.windows = {3}
        self.killed = []

    def pids(self, _job):
        return set(self.live)

    def parents(self):
        return self.parent_map

    def visible(self):
        return self.windows

    def identity(self, pid):
        return dict(self.live[pid])

    def terminate(self, _job, record):
        self.killed.append(record['pid'])
        del self.live[record['pid']]
        return 'terminated'

    def wait_supervisor(self, _job, record):
        self.live.pop(record['pid'], None)
        return 'supervisor_exited'


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeWindows()
        with patch('windows_codex_cleanup._Windows', return_value=self.api):
            self.tracker = JobProcessTracker(123, CODEX, DRIVER, [SHELL])

    def test_retains_gui_descendants_and_records_exited_runtime(self):
        self.tracker.snapshot()
        self.api.windows = set()  # Closing/hiding the window does not erase evidence.
        result = self.tracker.finish()
        self.assertEqual(result['status'], 'agent_runtime_stopped_with_task_apps_retained')
        self.assertEqual(self.api.killed, [1, 2])
        self.assertEqual({p['pid'] for p in result['remaining']}, {3, 4})
        self.assertEqual(len(result['processes']), 4)

    def test_unknown_survivor_retains_guest(self):
        self.api.live[5] = dict(path=r'C:\Unknown\service.exe', creation_time=500)
        self.api.parent_map[5] = 0
        result = self.tracker.finish()
        self.assertEqual(result['status'], 'unverified')
        self.assertTrue(result['retain_guest'])
        self.assertNotIn(5, self.api.killed)

    def test_enumeration_failure_prevents_cleanup(self):
        self.api.parents = lambda: (_ for _ in ()).throw(OSError('access denied'))
        result = self.tracker.finish()
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual(self.api.killed, [])

    def test_runtime_exit_keeps_previously_observed_shell_identity(self):
        self.tracker.snapshot()
        del self.api.live[1]
        result = self.tracker.finish()
        self.assertEqual(result['status'], 'agent_runtime_stopped_with_task_apps_retained')
        self.assertEqual(self.api.killed, [2])

    def supervisor_tracker(self):
        self.api.live[5] = dict(path=r'C:\Python\python.exe', creation_time=50)
        self.api.parent_map[5] = 0
        self.api.parent_map[1] = 5
        with patch('windows_codex_cleanup._Windows', return_value=self.api):
            return JobProcessTracker(123, CODEX, DRIVER, [SHELL], supervisor_pid=5)

    def test_supervisor_natural_exit_precedes_final_snapshot(self):
        tracker = self.supervisor_tracker()
        result = tracker.finish()
        self.assertEqual(result['status'], 'agent_runtime_stopped_with_task_apps_retained')
        self.assertEqual(result['supervisor']['wait_outcome'], 'supervisor_exited')
        self.assertEqual(self.api.killed, [1, 2])
        self.assertEqual(next(p for p in result['processes'] if p['pid'] == 5)['role'], 'agent_supervisor')

    def test_supervisor_wait_timeout_requires_guest_retention(self):
        tracker = self.supervisor_tracker()
        self.api.wait_supervisor = lambda *_args: 'supervisor_wait_timeout'
        result = tracker.finish()
        self.assertEqual(result['status'], 'unverified')
        self.assertTrue(result['retain_guest'])
        self.assertNotIn(5, self.api.killed)

    def test_reused_supervisor_pid_is_unknown_and_retained(self):
        tracker = self.supervisor_tracker()
        self.api.live[5] = dict(path=r'C:\Python\python.exe', creation_time=900)
        self.api.wait_supervisor = lambda *_args: 'supervisor_identity_changed'
        result = tracker.finish()
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual(next(p for p in result['remaining'] if p['pid'] == 5)['role'], 'unknown')
        self.assertNotIn(5, self.api.killed)

    def test_supervisor_binding_rejects_pid_outside_job(self):
        with patch('windows_codex_cleanup._Windows', return_value=self.api):
            with self.assertRaisesRegex(RuntimeError, 'selected Job'):
                JobProcessTracker(123, CODEX, DRIVER, [SHELL], supervisor_pid=555)

    def test_supervisor_exit_during_identity_read_is_rechecked_in_job(self):
        tracker = self.supervisor_tracker()
        original = self.api.identity
        def identity(pid):
            if pid == 5:
                del self.api.live[pid]
                raise OSError('Process exited during query')
            return original(pid)
        self.api.identity = identity
        state = tracker.snapshot()
        self.assertTrue(state['verified'], state)
        self.assertEqual(state['departed_during_snapshot'][0]['pid'], 5)
        self.assertEqual(tracker.finish()['status'], 'agent_runtime_stopped_with_task_apps_retained')

    def test_live_unreadable_member_remains_unverified(self):
        tracker = self.supervisor_tracker()
        original = self.api.identity
        def identity(pid):
            if pid == 5:
                raise OSError('Permission denied')
            return original(pid)
        self.api.identity = identity
        state = tracker.snapshot()
        self.assertFalse(state['verified'])
        self.assertEqual(state['departed_during_snapshot'], [])


class TerminationGuardTests(unittest.TestCase):
    def test_reused_pid_foreign_job_and_new_visible_window_are_not_killed(self):
        for changed, member, visible, expected in [
            (True, True, False, 'identity_changed'),
            (False, False, False, 'not_in_job'),
            (False, True, True, 'became_visible'),
        ]:
            with self.subTest(expected=expected):
                class Kernel:
                    def OpenProcess(self, *_args):
                        return 123

                    def CloseHandle(self, *_args):
                        return True

                    def IsProcessInJob(self, _handle, _job, result):
                        result._obj.value = member
                        return True

                    def TerminateProcess(self, *_args):
                        raise AssertionError('Unsafe TerminateProcess call')

                api = object.__new__(_Windows)
                api.kernel = Kernel()
                api._identity = lambda _handle: dict(path=CODEX + r'\codex.exe',
                                                     creation_time=999 if changed else 100)
                api.pids = lambda _job: {1}
                api.visible = lambda: {1} if visible else set()
                self.assertEqual(api.terminate(1, process(1, CODEX + r'\codex.exe')), expected)


@unittest.skipUnless(os.name == 'nt', 'requires real Windows Job and process APIs')
class WindowsHelperTests(unittest.TestCase):
    def helper(self, recognized, visible=False, supervisor=False):
        from ctypes import wintypes
        api = _Windows()
        kernel = api.kernel
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        job = api._check(kernel.CreateJobObjectW(None, None))
        child = None
        try:
            script = 'import time; print("ready", flush=True); time.sleep(60)'
            if supervisor:
                script = 'import sys,time; print("ready", flush=True); sys.stdin.read(1); time.sleep(0.2)'
            if visible:
                script = '''import ctypes, time
from ctypes import wintypes
u = ctypes.WinDLL('user32', use_last_error=True)
u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
    wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
u.CreateWindowExW.restype = wintypes.HWND
window = u.CreateWindowExW(0, 'STATIC', 'Job cleanup test helper', 0x10CF0000,
    10, 10, 200, 100, None, None, None, None)
if not window: raise ctypes.WinError(ctypes.get_last_error())
print('ready', flush=True)
time.sleep(60)
'''
            child = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE,
                                     stdin=subprocess.PIPE if supervisor else subprocess.DEVNULL,
                                     text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            api._check(kernel.AssignProcessToJobObject(job, int(child._handle)))
            ready = threading.Event()
            output = []
            def read_ready():
                output.append(child.stdout.readline().strip())
                ready.set()
            reader = threading.Thread(target=read_ready, daemon=True)
            reader.start()
            self.assertTrue(ready.wait(5), 'Helper did not start within 5 seconds')
            self.assertEqual(output, ['ready'])
            runtime = str(Path(sys.executable).parent) if recognized else r'C:\not-this-runtime'
            tracker = JobProcessTracker(job, runtime, DRIVER, [SHELL],
                                        supervisor_pid=child.pid if supervisor else None)
            before = tracker.snapshot()
            self.assertTrue(before['verified'], before)
            self.assertEqual({p['pid'] for p in before['processes']}, {child.pid})
            if supervisor:
                child.stdin.write('1')
                child.stdin.close()
            result = tracker.finish()
            if supervisor:
                self.assertEqual(result['status'], 'agent_runtime_stopped', result)
                self.assertEqual(result['actions'], [])
                child.wait(timeout=5)
            elif visible:
                self.assertEqual(result['status'], 'agent_runtime_stopped_with_task_apps_retained', result)
                self.assertIsNone(child.poll())
            elif recognized:
                self.assertEqual(result['status'], 'agent_runtime_stopped', result)
                child.wait(timeout=5)
            else:
                self.assertEqual(result['status'], 'unverified', result)
                self.assertIsNone(child.poll())
        finally:
            if child is not None and child.poll() is None:
                child.terminate()  # Exact helper Popen handle owned by this test.
                child.wait(timeout=5)
            if child is not None and child.stdout is not None:
                child.stdout.close()
            if child is not None and child.stdin is not None:
                child.stdin.close()
            kernel.CloseHandle(job)

    def test_real_runtime_terminated(self):
        self.helper(True)

    def test_real_unknown_member_retained(self):
        self.helper(False)

    def test_real_visible_task_application_retained(self):
        self.helper(False, visible=True)

    def test_real_supervisor_natural_exit(self):
        self.helper(False, supervisor=True)


if __name__ == '__main__':
    unittest.main()
