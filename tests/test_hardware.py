"""Configured helper processes use only local fixtures, never real hardware."""
import json
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from seriald_core.hardware import HardwareBook
from seriald_core.tasks import TaskCancelled, TaskError
from seriald_core.demo import DemoManager
from seriald_core.storage import Store
from seriald_core.task_actions import TaskActions


class HardwareConfigTests(unittest.TestCase):
    def test_disabled_default_and_invalid_configuration(self):
        with self.assertRaises(TaskError):
            HardwareBook().command('SN', 'reset', 'web')
        for values in ({'argv': ['relative']}, {'argv': [sys.executable], 'timeout': True},
                       {'argv': [sys.executable], 'timeout': 0},
                       {'argv': [sys.executable], 'allow_agent': 1},
                       {'argv': [sys.executable, '{serial.__class__}']},
                       {'argv': [sys.executable, '{serial!r}']},
                       {'argv': [sys.executable, '{unknown}']},
                       {'argv': [sys.executable, '\x00']},
                       {'argv': [sys.executable], 'shell': True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                HardwareBook({'boards': {'SN': {'reset': values}}})
        for document in ({'ports': {}}, {'boards': {'': {}}}, {'boards': {'SN': {'unknown': {}}}}):
            with self.assertRaises(ValueError):
                HardwareBook(document)

    def test_agent_policy_and_literal_argument_expansion(self):
        book = HardwareBook({'boards': {'SN': {'reset': {'argv': [sys.executable, '{serial}', '{device}', '{port}']}}}})
        with self.assertRaises(TaskError) as failure:
            book.command('SN', 'reset', 'agent')
        self.assertEqual(failure.exception.status, 403)
        device = mock.Mock(serial='SN', dev='tty; touch /tmp/unwanted', by_path='usb$(id)')
        command = book.boards['SN']['reset']
        self.assertEqual(command.expand(device), [sys.executable, 'SN', device.dev, device.by_path])

    def test_environment_file_limits_and_duplicate_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'hardware.json'
            with mock.patch.dict(os.environ, SERIALD_HARDWARE=str(path)):
                for raw in ('{"boards":{},"boards":{}}', ' ' * 65537):
                    path.write_text(raw)
                    with self.assertRaises(ValueError):
                        HardwareBook.from_env()
                path.write_text('{"boards":{}}', encoding='utf-8-sig')
                self.assertEqual(HardwareBook.from_env().boards, {})


@unittest.skipUnless(os.name in ('posix', 'nt'), 'requires a supported process-tree adapter')
class HardwareTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for module in ('device', 'devices'):
            patch = mock.patch('seriald_core.' + module + '.LOG_ROOT', str(self.root / 'logs'))
            patch.start(); self.addCleanup(patch.stop)
        self.store = Store(str(self.root / 'registry.sqlite3'))
        self.addCleanup(self.store.close)
        self.manager = DemoManager(self.store)
        self.addCleanup(self.manager.close)
        self.device = self.manager.resolve('demo-k1')
        self.actions = TaskActions(self.manager)

    def configure(self, code, allow_agent=False, timeout=60):
        helper = self.root / 'helper.py'
        helper.write_text(code)
        self.manager.hardware = HardwareBook({'boards': {self.device.serial: {'reset': {
            'argv': [sys.executable, str(helper), '{serial}', '{device}', '{port}'],
            'allow_agent': allow_agent, 'timeout': timeout}}}})

    def start(self, actor='web', **extra):
        return self.actions.start(dict(kind='hardware-reset', connection_id=self.device.connection_id, **extra),
                                  self.device, actor)

    def finish(self, response):
        self.assertEqual(response.status, 202, response.payload)
        task_id = response.payload['task']['id']
        with self.manager.tasks._lock:
            active = self.manager.tasks._active.get(task_id)
        if active:
            active[1].join(3)
            self.assertFalse(active[1].is_alive(), 'fixture helper did not stop')
        return self.manager.tasks.store.get(task_id)

    def ready(self):
        marker = self.root / 'ready'
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists())

    def test_configured_helper_audits_without_persisting_command_or_output(self):
        output = self.root / 'args.json'
        self.configure('import json,sys\nfrom pathlib import Path\nPath(%r).write_text(json.dumps(sys.argv[1:]))\n'
                       'print("a secret helper output")\n' % str(output), allow_agent=True)
        self.device.dev = 'literal; do not execute'
        task = self.finish(self.start('agent'))
        self.assertEqual(task['state'], 'completed', task)
        self.assertEqual(json.loads(output.read_text()), [self.device.serial, self.device.dev, self.device.by_path])
        self.assertEqual(task['result']['action'], 'reset')
        self.assertNotIn('secret', json.dumps(task))
        self.assertNotIn('helper.py', json.dumps(task))
        self.assertEqual([row['action'] for row in self.store.events(self.device.serial)][:2],
                         ['hardware-complete', 'hardware-start'])
        self.assertFalse(self.device.stop.is_set())
        self.assertIsNone(self.device.transactions.snapshot())

    def test_hardware_discovery_is_actor_scoped_and_never_executes(self):
        from seriald_core import web
        self.configure('raise AssertionError("must not execute")')
        for actor, expected in [('web', ['reset']), ('agent', [])]:
            handler = web.Handler.__new__(web.Handler)
            handler.path = '/api/hardware?key=demo-k1&serial=DEMO-001'
            handler.headers = {'X-Seriald-Actor': actor}
            handler.wfile = io.BytesIO()
            handler.send_response = mock.Mock(); handler.send_header = mock.Mock(); handler.end_headers = mock.Mock()
            with mock.patch.object(web, 'MGR', self.manager):
                handler.do_GET()
            self.assertEqual(json.loads(handler.wfile.getvalue())['actions'], expected)
            self.assertEqual(handler.send_response.call_args.args, (200,))
        self.assertEqual(self.manager.tasks.store.list(), [])

    def test_unconfigured_agent_or_client_command_is_rejected_without_execution(self):
        self.assertEqual(self.start().status, 404)
        self.configure('raise AssertionError("must not execute")')
        self.assertEqual(self.start('agent').status, 403)
        self.assertEqual(self.start(argv=[sys.executable]).status, 400)
        self.assertEqual(self.manager.tasks.store.list(), [])

    def test_nonzero_and_timeout_fail_release_the_lease_and_keep_capture(self):
        for code, timeout in [('raise SystemExit(7)', 60), ('import time; time.sleep(60)', 1)]:
            self.configure(code, timeout=timeout)
            task = self.finish(self.start())
            self.assertEqual(task['state'], 'failed', task)
            self.assertIn('partial', task['error'])
            self.assertIsNone(self.device.transactions.snapshot())
            self.assertFalse(self.device.stop.is_set())

    def test_cancelled_helper_cleans_up_without_disabling_another_board(self):
        self.configure('import time\nfrom pathlib import Path\nPath(%r).touch()\ntime.sleep(60)' % str(self.root / 'ready'))
        response = self.start(); self.ready()
        self.manager.tasks.cancel(response.payload['task']['id'], 'web')
        task = self.finish(response)
        self.assertEqual(task['state'], 'cancelled', task)
        self.assertIsNone(self.device.transactions.snapshot())
        self.assertFalse(self.manager.resolve('demo-k3').stop.is_set())

    def test_connection_change_stops_running_helper(self):
        self.configure('import time\nfrom pathlib import Path\nPath(%r).touch()\ntime.sleep(60)' % str(self.root / 'ready'))
        response = self.start(); self.ready()
        self.device.connection_id = 'replacement'
        task = self.finish(response)
        self.assertEqual(task['state'], 'failed', task)
        self.assertRegex(task['error'], 'changed|变化')

    def test_identified_offline_board_can_run_fixed_power_command(self):
        offline = self.manager.resolve('demo-offline')
        offline.stop.set()
        self.manager.hardware = HardwareBook({'boards': {offline.serial: {'power-on': {
            'argv': [sys.executable, '-c', 'pass']}}}})
        response = self.actions.start({'kind': 'hardware-power-on',
            'connection_id': offline.connection_id}, offline, 'web')
        task = self.finish(response)
        self.assertEqual(task['state'], 'completed', task)

    def test_unknown_target_and_missing_attachment_are_rejected(self):
        self.configure('pass')
        self.device.dev = ''
        self.assertEqual(self.start().status, 409)
        self.device.record = None
        self.assertEqual(self.start().status, 409)

    def test_console_operation_cancel_stops_hardware_task(self):
        self.configure('import time\nfrom pathlib import Path\nPath(%r).touch()\ntime.sleep(60)' % str(self.root / 'ready'))
        response = self.start(); self.ready()
        lease = self.device.transactions.snapshot()
        self.device.transactions.cancel(lease['id'], 'web')
        self.assertEqual(self.finish(response)['state'], 'cancelled')

    def test_other_actor_input_ownership_blocks_configured_helper(self):
        self.configure('raise AssertionError("must not execute")', allow_agent=True)
        self.device.transactions.claim('other-agent')
        response = self.start('agent')
        self.assertEqual(response.status, 423)
        self.assertEqual(self.manager.tasks.store.list(), [])

    def test_active_file_transfer_is_preserved_and_prevents_helper_execution(self):
        marker = self.root / 'must-not-exist'
        self.configure('from pathlib import Path\nPath(%r).touch()' % str(marker))
        transfer = self.device.file_transfer.start('upload', '/tmp/reserved', self.device.connection_id, size=0)
        try:
            task = self.finish(self.start())
            self.assertEqual(task['state'], 'failed')
            self.assertTrue(self.device.file_transfer.active)
            self.assertFalse(marker.exists())
        finally:
            self.device.file_transfer.abort(transfer['id'], self.device.connection_id)

    @unittest.skipUnless(sys.platform == 'linux', 'process-state assertion uses Linux /proc')
    def test_timeout_terminates_descendants_in_the_owned_process_group(self):
        pid_file = self.root / 'child.pid'
        code = ('import subprocess,sys,time\nfrom pathlib import Path\n'
                'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\n'
                'Path(%r).write_text(str(child.pid))\ntime.sleep(60)' % str(pid_file))
        self.configure(code, timeout=1)
        task = self.finish(self.start())
        self.assertEqual(task['state'], 'failed', task)
        state = Path('/proc') / pid_file.read_text() / 'stat'
        deadline = time.monotonic() + 1
        while state.exists() and state.read_text().split()[2] != 'Z' and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(not state.exists() or state.read_text().split()[2] == 'Z')
