"""Saved-file preparation checks without a Windows guest or benchmark answers."""
import ast
import json
import ntpath
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/win-arena-container/client'))
from desktop_env.controllers import setup
from test_setup_controller import response


def saved_file_target(task):
    """Audit the existing save-only, default-application preparation contract."""
    evaluator = task.get('evaluator', {})
    steps = evaluator.get('postconfig', [])
    activations = [step for step in steps if step['type'] == 'activate_window']
    if len(activations) == 2 and steps[-1] == activations[-1]:
        expected = {'window_name': activations[0]['parameters']['window_name'], 'strict': True}
        if activations[-1]['parameters'] == expected:
            activations = activations[:1]
    if len(activations) != 1:
        return None
    parameters = activations[0]['parameters']
    if not parameters.get('strict') or parameters.get('by_class'):
        return None
    results = evaluator.get('result', [])
    results = results if isinstance(results, list) else [results]
    if not results or any(item.get('type') != 'vm_file' for item in results):
        return None
    paths = {item.get('path') for item in results}
    if len(paths) != 1 or None in paths:
        return None
    path = paths.pop()
    opened = [step['parameters']['path'] for step in task.get('config', []) if step['type'] == 'open']
    if opened != [path] or ntpath.basename(path) not in parameters['window_name']:
        return None
    save_count = 0
    for step in steps:
        if step['type'] in ('activate_window', 'sleep'):
            continue
        if step['type'] != 'execute':
            return None
        command = step['parameters'].get('command', [])
        if len(command) != 3 or command[:2] != ['python', '-c']:
            return None
        for node in ast.parse(command[2]).body:
            if isinstance(node, ast.Import) and all(
                name.name in ('pyautogui', 'time') and name.asname is None for name in node.names
            ):
                continue
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                return None
            call = node.value
            if not isinstance(call.func, ast.Attribute) or not isinstance(call.func.value, ast.Name) or call.keywords:
                return None
            name = (call.func.value.id, call.func.attr)
            if name == ('pyautogui', 'hotkey') and [ast.literal_eval(arg) for arg in call.args] == ['ctrl', 's']:
                save_count += 1
            elif not (name == ('time', 'sleep') and len(call.args) == 1
                      and isinstance(call.args[0], ast.Constant)
                      and type(call.args[0].value) in (int, float) and call.args[0].value >= 0):
                return None
    return path if save_count == 1 else None


class SavedFilePostconfigTests(unittest.TestCase):
    def setUp(self):
        self.controller = setup.SetupController('test-guest', '/tmp')
        self.params = {'window_name': 'Document.xlsx', 'strict': True,
                       'open_file_if_missing': r'C:\Users\Docker\Downloads\Document.xlsx'}
        self.missing = response(404, text='Window Document.xlsx not found (empty results)')
        self.ready = response(text='Window activated successfully')
        self.steps = [
            {'type': 'activate_window', 'parameters': self.params},
            {'type': 'execute', 'parameters': {'command': ['save-current-document']}},
        ]

    def test_closed_saved_document_reopens_and_activates_before_save(self):
        events = []
        window = [False]
        def send(url, **kwargs):
            events.append(url.rsplit('/', 1)[-1])
            if url.endswith('/activate_window'):
                return self.ready if window[0] else self.missing
            self.assertEqual(json.loads(kwargs['data']), {'path': self.params['open_file_if_missing']})
            window[0] = True
            return response(text='File opened successfully')
        with patch.object(setup.requests, 'post', side_effect=send), \
             patch.object(self.controller, '_execute_setup', side_effect=lambda **_: events.append('save')):
            self.controller.setup(self.steps)
        self.assertEqual(events, ['activate_window', 'open_file', 'activate_window', 'save'])

    def test_open_document_is_saved_without_reopening(self):
        with patch.object(setup.requests, 'post', return_value=self.ready) as post, \
             patch.object(self.controller, '_execute_setup') as save:
            self.controller.setup(self.steps)
        self.assertEqual(post.call_count, 1)
        save.assert_called_once()

    def test_missing_file_or_open_failure_stops_before_save(self):
        for error in [response(404, text='File not found'), response(500, text='Open failed'),
                      response(body={'status': 'error'}), setup.requests.Timeout('unknown request outcome')]:
            with self.subTest(error=error), \
                 patch.object(setup.requests, 'post', side_effect=[self.missing, error]) as post, \
                 patch.object(self.controller, '_execute_setup') as save:
                with self.assertRaises((setup.requests.RequestException, RuntimeError)):
                    self.controller.setup(self.steps)
                self.assertEqual(post.call_count, 2)
                save.assert_not_called()

    def test_unknown_activation_errors_do_not_open_or_save(self):
        errors = [response(404, text='proxy not found'), response(503, text='activation uncertain'),
                  response(409, text='window replaced'), response(body={'status': 'error'}),
                  setup.requests.Timeout('unknown request outcome')]
        for error in errors:
            with self.subTest(error=error), patch.object(setup.requests, 'post', side_effect=[error]) as post, \
                 patch.object(self.controller, '_execute_setup') as save:
                with self.assertRaises((setup.requests.RequestException, RuntimeError)):
                    self.controller.setup(self.steps)
                self.assertEqual(post.call_count, 1)
                save.assert_not_called()

    def test_wrong_title_reopens_once_and_deadline_stops_save(self):
        clock = [0.0]
        opened = []
        def send(url, **kwargs):
            self.assertLessEqual(kwargs['timeout'], 30 - clock[0])
            if url.endswith('/open_file'):
                opened.append(json.loads(kwargs['data'])['path'])
                return response(text='File opened successfully')
            return self.missing
        def sleep(seconds):
            clock[0] += seconds
        with patch.object(setup.requests, 'post', side_effect=send), \
             patch.object(setup.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(setup.time, 'sleep', side_effect=sleep), \
             patch.object(self.controller, '_execute_setup') as save:
            with self.assertRaisesRegex(RuntimeError, 'readiness deadline exceeded'):
                self.controller.setup(self.steps)
        self.assertEqual(opened, [self.params['open_file_if_missing']])
        self.assertEqual(clock[0], 30)
        save.assert_not_called()

    def test_open_time_is_inside_the_activation_deadline(self):
        clock = [0.0]
        def send(url, **kwargs):
            if url.endswith('/open_file'):
                clock[0] = 31.0
                return response(text='File opened successfully')
            return self.missing
        with patch.object(setup.requests, 'post', side_effect=send) as post, \
             patch.object(setup.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(self.controller, '_execute_setup') as save:
            with self.assertRaisesRegex(RuntimeError, 'readiness deadline exceeded'):
                self.controller.setup(self.steps)
        self.assertEqual(post.call_count, 2)
        save.assert_not_called()

    def test_opt_in_requires_path_and_strict_title_activation(self):
        for change in [{'open_file_if_missing': ''}, {'open_file_if_missing': 1},
                       {'strict': False}, {'by_class': True}]:
            with self.subTest(change=change), patch.object(setup.requests, 'post') as post:
                with self.assertRaises(ValueError):
                    self.controller._activate_window_setup(**(self.params | change))
                post.assert_not_called()

    def test_all_selected_configs_bind_only_eligible_saved_file_preparation(self):
        examples = ROOT / 'src/win-arena-container/client/evaluation_examples_windows'
        selected = json.loads((examples / 'test_all.json').read_text())
        self.assertEqual(sum(map(len, selected.values())), 141)
        changed = 0
        for domain, task_ids in selected.items():
            for task_id in task_ids:
                task = json.loads((examples / 'examples' / domain / f'{task_id}.json').read_text())
                target = saved_file_target(task)
                parameters = [step['parameters'] for step in task.get('evaluator', {}).get('postconfig', [])
                              if step['type'] == 'activate_window']
                supplied = [p['open_file_if_missing'] for p in parameters if 'open_file_if_missing' in p]
                with self.subTest(domain=domain, task_id=task_id):
                    self.assertEqual(supplied, [target] if target else [])
                changed += bool(target)
        self.assertEqual(changed, 38)


if __name__ == '__main__':
    unittest.main()
