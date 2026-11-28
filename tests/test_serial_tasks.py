import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from decklab_core.board_maintenance import maintenance_for
from decklab_core.board_ownership import ownership_for
from decklab_core.device import Device
from decklab_core.devices import Manager
from decklab_core.storage import Store
from decklab_core.task_actions import TaskActions


class SerialTaskTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.store = Store(Path(root.name) / 'registry.sqlite3')
        self.addCleanup(self.store.close)
        self.serial = 'FIXTURE-SERIAL'
        self.store.bind({'serial': self.serial, 'serial_source': 'device-tree',
                         'identity_status': 'confirmed', 'ok': True}, 'fixture-port', '/fixture/tty')
        self.manager = Manager(self.store)
        self.addCleanup(self.manager.close)
        board = self.manager.boards[self.serial]
        self.device = Device('fixture-port', '/fixture/tty', self.manager, board=board)
        board.attach(self.device)
        self.device.present, self.device.fd, self.device.newline = True, object(), '\r'
        self.device.console_tail = 'fixture:~# '
        self.actions = TaskActions(self.manager)
        self.ownership = ownership_for(self.manager)
        self.payload = {'kind': 'serial-command', 'serial': self.serial, 'connection_id': self.device.connection_id,
                        'request_id': 'uart-case-0001', 'timeout': 1,
                        'command': "printf 'stdout\\n'; printf 'stderr' >&2; exit 7",
                        'titanx': {'job_id': 'job-71', 'case_code': 'KERNEL_SAMPLE',
                                   'command_id': 'step:uart:1', 'round': 2}}
        self.writes = []
        self.drop_receipt = False

        def serial_write(text):
            self.assertIn('__DECKLAB_SERIAL_RC_', text)
            self.writes.append(text)
            if not self.drop_receipt:
                # Run only the locally generated frame and the fixed test
                # command. The fixture never opens an actual serial port.
                self.assertIn(self.payload['command'].split(';')[0], text.replace("'\"'\"'", "'"))
                received = subprocess.check_output(
                    ['/bin/sh', '-c', text.replace('\r', '\n')], stderr=subprocess.STDOUT, timeout=1)
                with self.device.cv:
                    self.device.buf.extend(text.encode() + received.replace(b'\n', b'\r\n'))
                    self.device.cv.notify_all()
            return True, ''

        override = patch.object(self.device, '_write', side_effect=serial_write)
        override.start()
        self.addCleanup(override.stop)

    def submit(self, proof=None, **changes):
        with self.ownership.scope('web', proof=proof):
            return self.actions.start(dict(self.payload, **changes), self.device, 'web')

    def test_titanx_case_with_seven_day_deadline_finishes_immediately(self):
        proof = self.ownership.claim(self.serial, 'web')['execution_lease']
        task = self.finish(self.submit(proof, timeout=604800))
        self.assertEqual(task['result']['serial_receipt']['exit_code'], 7)
        self.assertEqual(self.submit(proof, timeout=604801).status, 400)

    def finish(self, reply):
        self.assertEqual(reply.status, 202, reply.payload)
        task_id = reply.payload['task']['id']
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            with self.manager.tasks._lock:
                if task_id not in self.manager.tasks._active:
                    return self.manager.tasks.store.get(task_id)
            time.sleep(.02)
        self.fail('serial task did not finish')

    def test_requires_the_board_lease_and_replays_the_original_receipt(self):
        self.assertEqual(self.submit().status, 423)
        proof = self.ownership.claim(self.serial, 'web')['execution_lease']
        self.assertEqual(self.submit().status, 423)
        task = self.finish(self.submit(proof))
        self.assertEqual((task['state'], task['outcome']), ('completed', 'observed'), task)
        receipt = task['result']['serial_receipt']
        self.assertEqual((receipt['exit_code'], receipt['stdout']), (7, 'stdout\nstderr'))
        self.assertEqual(receipt['serial_window']['connection_id'], self.device.connection_id)
        self.assertGreater(receipt['end_off'], receipt['serial_window']['start_off'])
        self.assertEqual(self.submit(proof).payload['task']['id'], task['id'])
        self.assertEqual(self.submit(proof, command='true').status, 409)
        self.assertEqual(len(self.writes), 1)
        store = self.manager.tasks.store
        rows = store.titanx_case_receipts(self.serial, 'job-71', 'kernel_sample', 2, 'serial-command')
        self.assertEqual([r['id'] for r in rows], [task['id']])
        self.assertEqual(store.titanx_case_receipts(self.serial, 'job-71', 'kernel_sample', 1, 'serial-command'), [])
        self.assertEqual(maintenance_for(self.manager).holds(self.serial), [])

    def test_stdin_upload_and_case_evidence_use_the_reserved_serial_console(self):
        import hashlib
        data = b'echo serial-only-test\n' * 150
        spool = self.manager.artifacts
        row = spool.create('upload', 'titanx-case-script', len(data), 'web', self.serial)
        spool.append(row['id'], 0, data, 'web', self.serial)
        spool.seal(row['id'], 'web', self.serial)
        proof = self.ownership.claim(self.serial, 'web')['execution_lease']
        path = '/tmp/shell-cases/fixture.sh'
        self.payload['command'] = "file='" + path + "'\nsha256sum"
        def board_shell(command, timeout, **kwargs):
            result = subprocess.run(['/bin/sh', '-c', command], capture_output=True, text=True, timeout=3)
            return result.returncode == 0, result.stdout + result.stderr
        with patch.object(self.device, '_run_cmd_locked', side_effect=board_shell):
            task = self.finish(self.submit(proof, stdin_artifact=row['id']))
        self.assertEqual(task['state'], 'completed', task.get('error'))
        receipt = task['result']['serial_receipt']
        digest = hashlib.sha256(data).hexdigest()
        self.assertIn(digest, receipt['stdout'])
        self.assertEqual(receipt['exit_code'], 0)
        self.assertFalse(Path(receipt['stdin_path']).exists())
        upload = self.manager.tasks.store.case_script_upload(self.serial, 'web', path, 0, time.time())
        self.assertEqual(upload['upload']['kind'], 'serial-command')
        self.assertEqual(upload['script_preview'], data.decode())
        self.assertEqual(upload['stdin_sha256'], digest)

    def test_shared_probe_requires_the_same_board_reservation_and_actor(self):
        proof = self.ownership.claim(self.serial, 'web')['execution_lease']
        self.payload.pop('titanx')
        task = self.finish(self.submit(proof))
        receipt = task['result']['serial_receipt']
        store = self.manager.tasks.store
        scope = (receipt['reservation_id'], 'web', 0, time.time())
        def read(serial, scope):
            return store.titanx_case_receipts(serial, 'job', 'CASE', 2, 'serial-command', reservation=scope)
        self.assertEqual([r['id'] for r in read(self.serial, scope)], [task['id']])
        self.assertEqual(read('OTHER', scope), [])
        self.assertEqual(read(self.serial, ('another-lease', *scope[1:])), [])
        self.assertEqual(read(self.serial, (scope[0], 'other-actor', *scope[2:])), [])

    def test_terminal_control_bytes_are_rejected_before_writing(self):
        proof = self.ownership.claim(self.serial, 'web')['execution_lease']
        for control in ('\x03', '\x04', '\x1b', '\x7f', '\r'):
            with self.subTest(control=control):
                self.assertEqual(self.submit(proof, command='echo demo' + control).status, 400)
        self.assertFalse(self.writes)

    def test_interrupted_serial_task_recreates_the_durable_board_hold(self):
        store = self.manager.tasks.store
        task_id = store.create('serial-command', 'web', '', [
            {'serial': self.serial, 'connection_id': self.device.connection_id}])
        store.update(task_id, state='running', outcome='issued', result={
            'cleanup_confirmed': False, 'serial_receipt': {'state': 'running', 'command': 'true'}})
        store.recover()
        task = store.get(task_id)
        self.assertEqual((task['state'], task['outcome']), ('interrupted', 'unknown'))
        self.assertEqual(task['result']['serial_receipt']['command'], 'true')
        holds = maintenance_for(self.manager).holds(self.serial)
        self.assertEqual([(row['task_id'], row['state']) for row in holds], [(task_id, 'recovery')])
        self.assertFalse(self.writes)

    def test_missing_exit_retains_original_operation_for_human_recovery(self):
        self.drop_receipt = True
        proof = self.ownership.claim(self.serial, 'web')['execution_lease']
        task = self.finish(self.submit(proof))
        self.assertEqual((task['state'], task['outcome']), ('failed', 'unknown'), task)
        receipt = task['result']['serial_receipt']
        self.assertEqual((receipt['state'], receipt['exit_code']), ('unconfirmed', None))
        self.assertIs(task['result']['cleanup_confirmed'], False)
        maintenance = maintenance_for(self.manager)
        self.assertEqual([row['state'] for row in maintenance.holds(self.serial)], ['recovery'])
        self.assertEqual(self.submit(proof).payload['task']['id'], task['id'])
        self.assertEqual(len(self.writes), 1)
        result = maintenance.recover_operation(task['id'], 'web', task['updated'], True, 'fixture command stopped')
        self.assertEqual(result['outcome'], 'unknown')
        self.assertEqual(maintenance.holds(self.serial), [])
