"""Capture failure must survive cleanup and prevent successful evidence sealing."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import remote_vm
import vm

CAPTURE_CONTAINER = vm.capture_container


class CleanupCaptureGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binding = {'episode_id': 'owned', 'container': 'waa-owned',
                        'storage': str(self.root / 'storage'), 'audio_capture': {'started': True}}
        self.export = {'status': 'exported', 'export_complete': True}
        self.patches = [patch.object(vm, 'capture_driver_diagnostics', return_value=self.export),
                        patch.object(vm, 'capture_container', return_value={'complete': True})]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_audio_error_preserves_export_and_container_stop_but_blocks_sealing(self):
        with patch.object(vm, 'stop_binding', return_value={'status': 'stopped',
                              'audio_capture_error': 'RuntimeError: stopcapture failed'}) as stop:
            cleanup = vm.capture_and_stop(self.binding, self.root)
        stop.assert_called_once_with(self.binding)
        self.assertEqual(cleanup['status'], 'stopped')
        self.assertEqual(cleanup['audio_capture_error'], 'RuntimeError: stopcapture failed')
        self.assertTrue(cleanup['driver_diagnostics_export']['export_complete'])
        self.assertTrue(cleanup['storage_reclamation_blocked'])
        self.assertTrue(self.binding['storage_reclamation_blocked'])
        with self.assertRaisesRegex(RuntimeError, 'Cleanup evidence incomplete'):
            vm.require_cleanup_evidence(self.binding)

    def test_repeat_cleanup_cannot_erase_previous_audio_error(self):
        self.binding['cleanup'] = {'status': 'stopped', 'audio_capture_error': 'original stopcapture failure'}
        self.binding['audio_capture']['started'] = False
        with patch.object(vm, 'stop_binding', return_value={'status': 'already_absent'}):
            cleanup = vm.capture_and_stop(self.binding, self.root)
        self.assertEqual(cleanup['audio_capture_error'], 'original stopcapture failure')
        self.assertTrue(self.binding['storage_reclamation_blocked'])

    def test_absent_container_with_unconfirmed_recording_is_incomplete(self):
        with patch.object(vm, 'stop_binding', return_value={'status': 'already_absent'}):
            cleanup = vm.capture_and_stop(self.binding, self.root)
        self.assertEqual(cleanup['audio_capture_error'], 'Audio capture lacks confirmed monitor stop')
        self.assertTrue(cleanup['storage_reclamation_blocked'])

    def test_successful_audio_and_disabled_audio_leave_export_gate_unchanged(self):
        for state in ({'started': False, 'monitor_stopped': True}, {}):
            with self.subTest(audio=state):
                binding = {**self.binding, 'audio_capture': state}
                with patch.object(vm, 'stop_binding', return_value={'status': 'stopped'}):
                    cleanup = vm.capture_and_stop(binding, self.root)
                self.assertFalse(cleanup['storage_reclamation_blocked'])
                vm.require_cleanup_evidence(binding)

    def test_successful_audio_does_not_clear_failed_driver_export(self):
        self.binding['audio_capture'].update(started=False, monitor_stopped=True)
        with patch.object(vm, 'capture_driver_diagnostics', return_value={'status': 'failed', 'export_complete': False}), \
             patch.object(vm, 'stop_binding', return_value={'status': 'stopped'}):
            cleanup = vm.capture_and_stop(self.binding, self.root)
        self.assertFalse(cleanup['driver_diagnostics_export']['export_complete'])
        self.assertTrue(cleanup['storage_reclamation_blocked'])

    def test_local_cli_saves_audio_failure_then_raises(self):
        owner = self.root / 'episode.json'
        owner.write_text(json.dumps(self.binding))
        with patch('sys.argv', ['vm.py', 'stop', '--episode', str(owner)]), \
             patch.object(vm, 'stop_binding', return_value={'status': 'stopped', 'audio_capture_error': 'QMP failed'}) as stop:
            with self.assertRaisesRegex(RuntimeError, 'Cleanup evidence incomplete'):
                vm.main()
        stop.assert_called_once()
        saved = json.loads(owner.read_text())
        self.assertEqual(saved['cleanup']['audio_capture_error'], 'QMP failed')
        self.assertTrue(saved['cleanup']['storage_reclamation_blocked'])
        self.assertTrue(saved['driver_diagnostics_export']['export_complete'])

    def test_remote_cancel_saves_audio_failure_then_raises(self):
        record = self.root / 'worker.json'
        progress = record.with_suffix('.vm.json')
        progress.write_text(json.dumps(self.binding))
        with patch.object(vm, 'stop_binding', return_value={'status': 'stopped', 'audio_capture_error': 'QMP failed'}) as stop:
            with self.assertRaisesRegex(RuntimeError, 'Cleanup evidence incomplete'):
                remote_vm.cancel_worker(argparse.Namespace(worker_record=record))
        stop.assert_called_once()
        saved = json.loads(progress.read_text())
        self.assertEqual(saved['cleanup']['audio_capture_error'], 'QMP failed')
        self.assertTrue(saved['cleanup']['storage_reclamation_blocked'])

    def test_docker_capture_nonzero_blocks_and_uses_owned_full_id(self):
        self.binding['audio_capture']['started'] = False
        owned = {'Id': 'a' * 64, 'Config': {'Labels': {'waa.owner': 'waa-v2', 'waa.episode': 'owned'}}}
        with patch.object(vm, 'capture_container', side_effect=CAPTURE_CONTAINER), \
             patch.object(vm, 'inspect_container', return_value=owned), \
             patch.object(vm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'Docker read failed')) as read, \
             patch.object(vm, 'stop_binding', return_value={'status': 'stopped'}) as stop:
            cleanup = vm.capture_and_stop(self.binding, self.root)
        stop.assert_called_once()
        self.assertTrue(cleanup['storage_reclamation_blocked'])
        self.assertIn('capture_error', cleanup)
        self.assertFalse(cleanup['container_capture_attempt']['complete'])
        for call in read.call_args_list:
            self.assertEqual(call.args[0][-1], owned['Id'])

    def test_repeat_absent_reuses_only_owned_complete_container_snapshot(self):
        self.binding['audio_capture']['started'] = False
        owned = {'Id': 'a' * 64, 'Config': {'Labels': {'waa.owner': 'waa-v2', 'waa.episode': 'owned'}}}
        with patch.object(vm, 'capture_container', side_effect=CAPTURE_CONTAINER), \
             patch.object(vm, 'inspect_container', return_value=owned), \
             patch.object(vm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'evidence', '')), \
             patch.object(vm, 'stop_binding', return_value={'status': 'stopped'}):
            vm.capture_and_stop(self.binding, self.root)
        previous = self.binding['container_capture']
        self.assertTrue(vm.reusable_container_capture(previous, self.binding, self.root))
        with patch.object(vm, 'capture_container', side_effect=CAPTURE_CONTAINER), \
             patch.object(vm, 'inspect_container', return_value=None), \
             patch.object(vm, 'stop_binding', return_value={'status': 'already_absent'}):
            cleanup = vm.capture_and_stop(self.binding, self.root)
        self.assertTrue(cleanup['container_capture_reused'])
        self.assertFalse(cleanup['storage_reclamation_blocked'])
        self.assertIn('capture_attempt_error', cleanup)
        self.assertEqual(self.binding['container_capture'], previous)
        self.assertFalse(cleanup['container_capture_attempt']['complete'])
        self.assertFalse(vm.reusable_container_capture(previous, {**self.binding, 'episode_id': 'other'}, self.root))
        self.assertFalse(vm.reusable_container_capture(previous, self.binding, self.root / 'other'))
        (Path(previous['capture_directory']) / 'container.log').unlink()
        self.assertFalse(vm.reusable_container_capture(previous, self.binding, self.root))

    def test_absence_cannot_clear_first_capture_failure(self):
        self.binding['audio_capture']['started'] = False
        self.binding['cleanup'] = {'status': 'stopped', 'capture_error': 'initial disk full'}
        with patch.object(vm, 'capture_container', side_effect=CAPTURE_CONTAINER), \
             patch.object(vm, 'inspect_container', return_value=None), \
             patch.object(vm, 'stop_binding', return_value={'status': 'already_absent'}):
            cleanup = vm.capture_and_stop(self.binding, self.root)
        self.assertTrue(cleanup['storage_reclamation_blocked'])
        self.assertEqual(cleanup['previous_capture_error'], 'initial disk full')
        self.assertIn('capture_error', cleanup)

    def test_new_success_can_resolve_capture_failure_without_erasing_its_record(self):
        self.binding['audio_capture']['started'] = False
        self.binding['cleanup'] = {'capture_error': 'initial transient capture error'}
        with patch.object(vm, 'stop_binding', return_value={'status': 'stopped'}):
            cleanup = vm.capture_and_stop(self.binding, self.root)
        self.assertFalse(cleanup['storage_reclamation_blocked'])
        self.assertNotIn('capture_error', cleanup)
        self.assertEqual(cleanup['previous_capture_error'], 'initial transient capture error')


if __name__ == '__main__':
    unittest.main()
