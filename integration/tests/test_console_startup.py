"""Check every bridge-owned console process without changing GUI launch policy."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from guest_mcp_bridge import Command, Session


CONSOLE_PROBE = r'''
import ctypes,json
api=ctypes.WinDLL('kernel32')
api.GetConsoleWindow.restype=ctypes.c_void_p
console=api.GetConsoleWindow()
print(json.dumps({'console':console}),flush=True)
'''


class ConsoleStartupTests(unittest.TestCase):
    def test_command_retains_output_environment_and_process_group(self):
        env = dict(os.environ, WAA_CONSOLE_TEST='preserved')
        command = Command([sys.executable, '-c',
            "import os,sys; print(os.environ['WAA_CONSOLE_TEST']); print('stderr',file=sys.stderr)"], env=env)
        try:
            result = command.wait(10)
            self.assertEqual(result['returncode'], 0)
            self.assertEqual(result['output'].strip(), 'preserved')
            self.assertEqual(result['error'].strip(), 'stderr')
            if os.name != 'nt':
                # The real subprocess call also rejects nonzero Windows flags.
                self.assertIsNone(command.job)
        finally:
            command.dispose()

    @unittest.skipUnless(os.name == 'nt', 'requires real Windows console semantics')
    def test_supervisor_and_command_have_no_console(self):
        actual_popen = subprocess.Popen
        def inspect_supervisor(argv, **kwargs):
            # The observation executes inside the actual supervisor before it
            # launches its command, and goes through the existing output pipe.
            argv = list(argv)
            argv[2] = CONSOLE_PROBE + '\n' + argv[2]
            return actual_popen(argv, **kwargs)

        with patch('guest_mcp_bridge.subprocess.Popen', side_effect=inspect_supervisor):
            command = Command([sys.executable, '-c', CONSOLE_PROBE])
        try:
            result = command.wait(10)
            self.assertEqual(result['returncode'], 0, result)
            states = [json.loads(line) for line in result['output'].splitlines()]
            self.assertEqual(states, [{'console': None}, {'console': None}])
        finally:
            command.dispose()

    @unittest.skipUnless(os.name == 'nt', 'requires real Windows console semantics')
    def test_mcp_has_no_console_and_keeps_stdio(self):
        code = r'''
import ctypes,json,sys
api=ctypes.WinDLL('kernel32')
api.GetConsoleWindow.restype=ctypes.c_void_p
for line in sys.stdin:
    request=json.loads(line)
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],
                      'result':{'console':api.GetConsoleWindow()}}),flush=True)
'''
        session = Session({'command': [sys.executable, '-u', '-c', code]})
        try:
            state = session.call('console_probe', None, 10)
            self.assertEqual(state['response']['result'], {'console': None})
        finally:
            session.close()


if __name__ == '__main__':
    unittest.main()
