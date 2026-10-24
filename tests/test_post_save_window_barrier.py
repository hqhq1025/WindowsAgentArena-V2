"""Do not read a saved-file result while document preparation remains blocked."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/win-arena-container/client'))
from desktop_env.controllers import setup
from desktop_env.envs.desktop_env import DesktopEnv
from test_setup_controller import response
from test_saved_file_postconfig import saved_file_target


class PostSaveWindowBarrierTests(unittest.TestCase):
    def environment(self):
        env = DesktopEnv.__new__(DesktopEnv)
        env.setup_controller = setup.SetupController('test-guest', '/tmp')
        activation = {'type': 'activate_window', 'parameters': {'window_name': 'Document.xlsx', 'strict': True}}
        env.evaluator = {
            'func': 'synthetic_file_metric', 'result': {'type': 'vm_file', 'path': 'document.xlsx'},
            'postconfig': [activation,
                           {'type': 'execute', 'parameters': {'command': ['save-current-document']}},
                           activation],
        }
        env.action_history = []
        env.result_getter = Mock(return_value='actual saved file')
        env.metric = Mock(return_value=0.25)
        env.metric_options = {}
        return env

    def test_save_blocked_by_modal_does_not_read_or_score_old_file(self):
        env = self.environment()
        responses = [response(text='Window activated successfully'),
                     response(body={'returncode': 0, 'output': '', 'error': ''}),
                     response(503, body={'status': 'error', 'error': 'Window did not become active and responsive'})]
        with patch.object(setup.requests, 'post', side_effect=responses) as post:
            with self.assertRaises(setup.requests.HTTPError):
                env.evaluate()
        self.assertEqual(post.call_count, 3)
        env.result_getter.assert_not_called()
        env.metric.assert_not_called()

    def test_responsive_document_reaches_original_metric_after_save(self):
        env = self.environment()
        events = []
        def send(url, **kwargs):
            route = url.rsplit('/', 1)[-1]
            events.append(route)
            if route == 'execute':
                return response(body={'returncode': 0, 'output': '', 'error': ''})
            return response(text='Window activated successfully')
        env.result_getter.side_effect = lambda *_: events.append('read_saved_file') or 'actual saved file'
        with patch.object(setup.requests, 'post', side_effect=send):
            self.assertEqual(env.evaluate(), 0.25)
        self.assertEqual(events, ['activate_window', 'execute', 'activate_window', 'read_saved_file'])
        env.metric.assert_called_once_with('actual saved file')

    def test_unknown_after_save_transport_failure_stops_scoring(self):
        env = self.environment()
        responses = [response(text='Window activated successfully'),
                     response(body={'returncode': 0, 'output': '', 'error': ''}),
                     setup.requests.Timeout('unknown final window state')]
        with patch.object(setup.requests, 'post', side_effect=responses):
            with self.assertRaises(setup.requests.Timeout):
                env.evaluate()
        env.result_getter.assert_not_called()
        env.metric.assert_not_called()

    def test_all_eligible_selected_tasks_end_with_same_window_barrier(self):
        examples = ROOT / 'src/win-arena-container/client/evaluation_examples_windows'
        selected = json.loads((examples / 'test_all.json').read_text())
        checked = 0
        for domain, task_ids in selected.items():
            for task_id in task_ids:
                task = json.loads((examples / 'examples' / domain / f'{task_id}.json').read_text())
                if not saved_file_target(task):
                    continue
                steps = task['evaluator']['postconfig']
                initial = next(step for step in steps if step['type'] == 'activate_window')
                with self.subTest(task_id=task_id):
                    self.assertEqual(steps[-1], {'type': 'activate_window', 'parameters': {
                        'window_name': initial['parameters']['window_name'], 'strict': True}})
                    self.assertNotIn('open_file_if_missing', steps[-1]['parameters'])
                    self.assertTrue(any(step['type'] == 'execute' for step in steps[:-1]))
                checked += 1
        self.assertEqual(checked, 38)


if __name__ == '__main__':
    unittest.main()
