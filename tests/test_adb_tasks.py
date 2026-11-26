import hashlib
import os
import shlex
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from decklab_core.board_maintenance import maintenance_for
from decklab_core.device import Device
from decklab_core.devices import Manager
from decklab_core.storage import Store
from decklab_core.task_actions import TaskActions


@unittest.skipUnless(os.name == 'posix' and Path('/proc/sys/kernel/random/boot_id').exists(), 'uses a local shell fixture')
class AdbTasksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'registry.sqlite3')
        self.addCleanup(self.store.close)
        self.store.bind({'serial': 'FIXTURE-BOARD', 'serial_source': 'device-tree',
                         'identity_status': 'confirmed', 'ok': True}, 'fixture-port', '/fixture/tty')
        self.manager = Manager(self.store)
        self.addCleanup(self.manager.close)
        board = self.manager.boards['FIXTURE-BOARD']
        self.device = Device('fixture-port', '/fixture/tty', self.manager, board=board)
        board.attach(self.device)
        self.device.present = True
        self.actions = TaskActions(self.manager)
        self.mode = self.root / 'mode'
        executable = self.root / 'adb'
        executable.write_text("""#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path(__file__).parent
args = sys.argv[1:]
assert os.environ['ADB_SERVER_SOCKET'] == 'tcp:127.0.0.1:5037'
if args == ['devices', '-l']:
    print('List of devices attached')
    print('duplicated device usb:2-1 transport_id:7')
    print('duplicated device usb:2-2 transport_id:9')
elif args[2:3] == ['shell'] and '-T' not in args:
    board = 'FIXTURE-BOARD' if args[1] == '9' else 'OTHER-BOARD'
    print('serial=' + board)
    print('boot_id=' + Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    print('uid=1000')
else:
    assert args[:4] == ['-t', '9', 'shell', '-T']
    with (root/'commands').open('a') as record:
        record.write(args[1] + '\\n')
    mode = (root/'mode').read_text()
    if mode in ('drop', 'timeout'):
        print('partial stdout', flush=True)
        print('USB closed', file=sys.stderr, flush=True)
        if mode == 'timeout':
            time.sleep(20)
        raise SystemExit(1)
    env = dict(os.environ, TEST_BOARD='OTHER-BOARD' if mode == 'replaced' else 'FIXTURE-BOARD')
    raise SystemExit(subprocess.run(['/bin/sh', '-c', args[4]], env=env).returncode)
""")
        executable.chmod(0o700)
        for override in (patch('decklab_core.adb_tasks.shutil.which', return_value=str(executable)),
                         patch('decklab_core.adb_tasks.READ_IDENTITY', 'serial=$TEST_BOARD\n')):
            override.start()
            self.addCleanup(override.stop)
        self.payload = {'kind': 'adb-command', 'serial': self.device.serial,
                        'connection_id': self.device.connection_id, 'request_id': 'fixture-request-1',
                        'command': "printf '中文\\n'; printf 'stderr' >&2; exit 7"}

    def finish(self, mode='success', **payload):
        self.mode.write_text(mode)
        started = self.actions.start(dict(self.payload, **payload), self.device, 'web')
        self.assertEqual(started.status, 202, started.payload)
        task_id = started.payload['task']['id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.manager.tasks._lock:
                if task_id not in self.manager.tasks._active:
                    return self.actions.query(task_id, serial=self.device.serial).payload['task']
            time.sleep(.02)
        self.fail('fixture task did not finish')

    def test_sealed_input_is_streamed_to_guarded_shell_and_replay_is_idempotent(self):
        data = (b'#!/bin/sh\nprintf "hello"\n' * 4000)[:61440]
        spool = self.manager.artifacts
        serial = self.device.serial
        ready = spool.create('upload', 'case.sh', len(data), 'web', serial)
        spool.append(ready['id'], 0, data, 'web', serial)
        spool.seal(ready['id'], 'web', serial)
        destination = self.root / 'script-on-board'
        payload = {'stdin_artifact': ready['id'], 'command': 'cat > ' + shlex.quote(str(destination))}
        task = self.finish(**payload)
        self.assertEqual(task['outcome'], 'observed')
        self.assertEqual(task['result']['adb_receipt']['stdin_sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(destination.read_bytes(), data)
        self.assertEqual(spool.get(ready['id'], 'web', serial)['state'], 'consumed')
        replay = self.actions.start(dict(self.payload, **payload), self.device, 'web')
        self.assertEqual(replay.payload['task']['id'], task['id'])
        self.assertEqual((self.root / 'commands').read_text().splitlines(), ['9'])

    def test_interactive_task_registers_closes_and_deduplicates_session(self):
        import socket
        from decklab_core.actions import DeviceActions
        from decklab_core.adb_session import AdbSession

        local, peer = socket.socketpair()
        self.addCleanup(peer.close)
        def open_fixture(session):
            self.assertEqual(session.endpoint, '9')
            session.initial = b'fixture shell ready'
            return local
        self.payload.pop('command')
        self.payload.pop('timeout', None)
        with patch.object(self.device, 'stable_dir', return_value=str(self.root)), patch.object(
                AdbSession, '_open_transport', open_fixture):
            task = self.finish(kind='adb-connect')
            self.assertEqual(task['state'], 'completed', task)
            sid = task['result']['sid']
            self.assertEqual(self.finish(kind='adb-connect')['result']['sid'], sid)
            self.assertEqual([s['kind'] for s in self.device.list_sessions()], ['adb'])
            action = DeviceActions(self.manager)
            self.assertEqual(action.execute('session-send', {'sid': sid, 'data': 'pwd\n'}, self.device).status, 200)
            self.assertEqual(peer.recv(64), b'\x00\x04\x00\x00\x00pwd\n')
            self.assertEqual(action.execute('session-close', {'sid': sid}, self.device).status, 200)
            self.assertEqual(self.device.list_sessions(), [])

    def test_records_remote_exit_and_serial_cursor_on_exact_usb_board(self):
        task = self.finish()
        receipt = task['result']['adb_receipt']
        self.assertEqual((task['state'], task['outcome']), ('completed', 'observed'))
        self.assertEqual((receipt['exit_code'], receipt['stdout'], receipt['stderr']), (7, '中文\n', 'stderr'))
        self.assertEqual(receipt['command'], self.payload['command'])
        self.assertEqual(receipt['transport_id'], '9')
        self.assertEqual(receipt['usb_port'], '2-2')
        self.assertEqual(receipt['serial_window'], {'connection_id': self.device.connection_id, 'start_off': 0})
        self.assertGreaterEqual(receipt['finished_at'], receipt['started_at'])
        self.assertEqual(self.actions.query(task['id'], 'other').status, 404)
        self.assertFalse(maintenance_for(self.manager).holds(self.device.serial))
        again = self.actions.start(self.payload, self.device, 'web').payload['task']
        self.assertEqual(again['id'], task['id'])
        self.assertEqual(self.actions.start(dict(self.payload, command='exit 0'), self.device, 'web').status, 409)
        self.assertEqual((self.root / 'commands').read_text().splitlines(), ['9'])

    def test_lost_receipt_retains_partial_output_and_requires_recovery_without_replay(self):
        task = self.finish('drop')
        receipt = task['result']['adb_receipt']
        self.assertEqual((task['state'], task['outcome']), ('failed', 'unknown'))
        self.assertEqual((receipt['state'], receipt['exit_code'], receipt['adb_exit_code']), ('disconnected', None, 1))
        self.assertEqual(receipt['stdout'], 'partial stdout\n')
        self.assertEqual(receipt['stderr'], 'USB closed\n')
        coordinator = maintenance_for(self.manager)
        self.assertEqual([row['state'] for row in coordinator.holds(self.device.serial)], ['recovery'])
        self.assertEqual(self.actions.start(dict(self.payload, request_id='fixture-request-2'), self.device, 'web').status, 423)
        recovered = coordinator.recover_operation(task['id'], 'web', task['updated'], True, 'serial fixture confirmed stop')
        self.assertEqual(recovered['outcome'], 'unknown')
        self.assertFalse(coordinator.holds(self.device.serial))
        self.assertEqual(self.actions.start(self.payload, self.device, 'web').payload['task']['id'], task['id'])
        self.assertEqual((self.root / 'commands').read_text().splitlines(), ['9'])

    def test_guard_rejects_replacement_before_running_and_timeout_keeps_partial_output(self):
        task = self.finish('replaced', command='printf must-not-run')
        self.assertEqual((task['state'], task['outcome']), ('failed', 'not-issued'))
        self.assertEqual(task['result']['adb_receipt']['stdout'], '')
        self.assertEqual(task['result']['adb_receipt']['state'], 'rejected')
        self.assertFalse(maintenance_for(self.manager).holds(self.device.serial))
        task = self.finish('timeout', request_id='timeout-attempt', timeout=1)
        receipt = task['result']['adb_receipt']
        self.assertEqual((receipt['state'], receipt['stdout'], receipt['exit_code']), ('timed-out', 'partial stdout\n', None))
        self.assertEqual(task['outcome'], 'unknown')
