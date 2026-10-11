"""Run collector-owned copies on fake shell/serial boards, never hardware."""
import hashlib
import tempfile
import threading
import unittest
from unittest import mock

from seriald_core.demo import DemoManager
from seriald_core.storage import Store
from seriald_core.task_actions import TaskActions
from seriald_core.tasks import TaskError


class CopyTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name + '/registry.sqlite3')
        self.addCleanup(self.store.close)
        with mock.patch('seriald_core.device.LOG_ROOT', self.tmp.name + '/logs'):
            self.manager = DemoManager(self.store)
        self.addCleanup(self.manager.close)
        self.source = self.manager.resolve('demo-k1')
        self.destination = self.manager.resolve('demo-k3')
        self.actions = TaskActions(self.manager)
        self.source.files['/source'] = bytes(range(256)) * 400

    def start(self, **changes):
        payload = dict(kind='file-copy', connection_id=self.source.connection_id,
                       to_key=self.destination.key, to_connection_id=self.destination.connection_id,
                       to_serial=self.destination.serial or '', path='/source', to_path='/destination',
                       client_id='page', overwrite=False)
        payload.update(changes)
        return self.actions.start(payload, self.source, 'web')

    def result(self, response):
        self.assertEqual(response.status, 202, response.payload)
        task_id = response.payload['task']['id']
        with self.manager.tasks._lock:
            entry = self.manager.tasks._active.get(task_id)
        if entry:
            entry[1].join(3)
            self.assertFalse(entry[1].is_alive(), 'copy did not stop')
        return self.manager.tasks.store.get(task_id)

    def test_copy_result_persists_and_multi_board_scope_cannot_leak(self):
        task = self.result(self.start())
        self.assertEqual(task['state'], 'completed', task)
        self.assertEqual(self.destination.files['/destination'], self.source.files['/source'])
        self.assertEqual(task['result']['sha256'], hashlib.sha256(self.source.files['/source']).hexdigest())
        self.assertEqual(task['progress']['done'], len(self.source.files['/source']))
        self.assertEqual(len(task['targets']), 2)
        self.assertEqual(self.actions.query(task['id'], serial=self.source.serial).status, 404)
        self.assertEqual([row['id'] for row in self.actions.query(target_serial=self.source.serial).payload['tasks']], [task['id']])
        self.assertEqual(self.actions.query(serial=self.source.serial, target_serial=self.source.serial).payload['tasks'], [])
        self.assertFalse(self.source.file_transfer.active)
        self.assertFalse(self.destination.file_transfer.active)
        self.assertGreater(task['result']['rate_diagnostics']['effective_kib_s'], 0)

    def test_unidentified_destination_is_pinned_to_its_connection_without_a_serial(self):
        self.destination.detach_identity()
        task = self.result(self.start())
        self.assertEqual(task['state'], 'completed', task)
        self.assertEqual(self.destination.files['/destination'], self.source.files['/source'])
        self.assertTrue(any(target['serial'] == '' for target in task['targets']))

    def test_empty_file_and_overwrite_policy(self):
        self.source.files['/source'] = b''
        self.destination.files['/destination'] = b'original'
        task = self.result(self.start())
        self.assertEqual(task['state'], 'failed')
        self.assertEqual(self.destination.files['/destination'], b'original')
        self.assertFalse(self.source.file_transfer.active)
        task = self.result(self.start(overwrite=True))
        self.assertEqual(task['state'], 'completed', task)
        self.assertEqual(self.destination.files['/destination'], b'')

    def test_bad_destination_and_paths_rejected_before_creating_task(self):
        for changes in ({'to_key': self.source.key}, {'to_connection_id': 'stale'},
                        {'to_serial': 'other'}, {'path': 'relative'}, {'to_path': '/tmp/\nfile'}):
            self.assertGreaterEqual(self.start(**changes).status, 400)
        self.assertEqual(self.manager.tasks.store.list(), [])

    def test_destination_input_owner_blocks_task_without_reserving_source(self):
        with self.destination.transactions.actor_scope('web', 'other-page'):
            self.destination.transactions.claim('web')
        response = self.start()
        self.assertEqual(response.status, 423)
        self.assertFalse(self.source.file_transfer.active)
        self.assertEqual(self.manager.tasks.store.list(), [])

    def test_cancel_wakes_reader_cleans_both_ports_and_keeps_other_device_available(self):
        entered = threading.Event()
        original = self.source._run_cmd_locked
        def waiting(command, timeout, ensure_shell=True):
            if 'SD_DATA=' in command:
                entered.set()
                lease = self.source.transactions._local.lease
                with self.source.cv:
                    self.source.cv.wait_for(lease.is_set, timeout=2)
                lease.check()
            return original(command, timeout, ensure_shell)
        with mock.patch.object(self.source, '_run_cmd_locked', side_effect=waiting):
            response = self.start()
            self.assertTrue(entered.wait(2))
            self.manager.tasks.cancel(response.payload['task']['id'], 'web')
            task = self.result(response)
        self.assertEqual(task['state'], 'cancelled', task)
        self.assertNotIn('/destination', self.destination.files)
        self.assertFalse(any('.seriald-' in path for path in self.destination.files))
        self.assertFalse(self.source.file_transfer.active)
        self.assertFalse(self.destination.file_transfer.active)
        self.assertIsNone(self.manager.resolve('demo-offline').transactions.snapshot())

    def test_replaced_source_connection_is_not_probed_or_cleaned_and_local_reservations_retire(self):
        original = self.source.file_transfer.chunk
        def replace(*arguments, **kwargs):
            result = original(*arguments, **kwargs)
            self.source.connection_id = 'replacement-connection'
            return result
        with mock.patch.object(self.source.file_transfer, 'chunk', side_effect=replace), \
             mock.patch.object(self.source, '_run_cmd_locked', wraps=self.source._run_cmd_locked) as command:
            task = self.result(self.start())
        self.assertEqual(task['state'], 'failed')
        self.assertFalse(self.source.file_transfer.active)
        self.assertFalse(self.destination.file_transfer.active)
        self.assertFalse(any(call.args[0].startswith('rm -f') for call in command.call_args_list))
        self.assertNotIn('/destination', self.destination.files)

    def test_prefetch_and_write_overlap_and_prefetch_is_joined_after_write_failure(self):
        reading, writing = threading.Event(), threading.Event()
        read_original = self.source._run_cmd_locked
        write_original = self.destination._run_cmd_locked
        def read(command, timeout, ensure_shell=True):
            if 'SD_DATA=' in command and self.source.file_transfer.state['offset'] >= 30720:
                reading.set()
                self.assertTrue(writing.wait(2), 'reader and writer did not overlap')
            return read_original(command, timeout, ensure_shell)
        def write(command, timeout, ensure_shell=True):
            if 'base64 -d' in command:
                writing.set()
                self.assertTrue(reading.wait(2), 'no prefetch during write')
                return False, 'injected write failure'
            return write_original(command, timeout, ensure_shell)
        with mock.patch.object(self.source, '_run_cmd_locked', side_effect=read), \
             mock.patch.object(self.destination, '_run_cmd_locked', side_effect=write):
            task = self.result(self.start())
        self.assertEqual(task['state'], 'failed')
        self.assertFalse(self.source.file_transfer.active)
        self.assertFalse(self.destination.file_transfer.active)
        self.assertNotIn('/destination', self.destination.files)
