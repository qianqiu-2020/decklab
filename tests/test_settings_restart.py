"""Isolated settings activation: fake devices, private sockets, no USB discovery."""
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace

from seriald_core.settings_restart import configure_collector, configure_control
from seriald_core.web_settings import WebSettings, SettingsError

ROOT = Path(__file__).resolve().parents[1]


class RestartAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.settings = WebSettings(Path(self.folder.name) / 'web-settings.json', {'SERIALD_BASE': self.folder.name})
        self.tasks = SimpleNamespace(_lock=threading.RLock(), _active={}, _closing=False)
        self.manager = SimpleNamespace(tasks=self.tasks, all_devices=lambda: [], stopping=threading.Event())
        self.server = SimpleNamespace(manager=self.manager, shutdown=mock.Mock())
        configure_collector(self.server, self.settings)
        self.revision = self.settings.snapshot()['revision']

    def test_busy_tasks_and_parallel_requests_leave_collector_running(self):
        for busy in ('task', 'post'):
            self.tasks._active = {'active': object()} if busy == 'task' else {}
            self.server.configuration_posts = 2 if busy == 'post' else 1
            with self.assertRaises(SettingsError):
                self.server.activate_settings(self.revision)
            self.assertFalse(self.server.restart_requested)
            self.assertFalse(self.tasks._closing)
            self.server.shutdown.assert_not_called()

    def test_console_reservation_transfer_and_transaction_refuse_restart(self):
        transactions = mock.Mock()
        device = SimpleNamespace(transactions=transactions, file_transfer=SimpleNamespace(active=False))
        self.manager.all_devices = lambda: [device]
        for kind in ('reservation', 'transaction', 'transfer'):
            transactions.snapshot.return_value = {'kind': 'command'} if kind == 'transaction' else None
            transactions.ownership.return_value = {'actor': 'web'} if kind == 'reservation' else None
            device.file_transfer.active = kind == 'transfer'
            with self.assertRaises(SettingsError):
                self.server.activate_settings(self.revision)
            self.assertFalse(self.server.restart_requested)

    def test_accepted_activation_blocks_new_tasks_before_shutdown(self):
        with mock.patch('seriald_core.settings_restart.threading.Thread') as thread:
            self.server.activate_settings(self.revision)
        self.assertTrue(self.server.restart_requested)
        self.assertTrue(self.tasks._closing)
        self.assertTrue(self.manager.stopping.is_set())
        thread.return_value.start.assert_called_once()

    def test_stale_revision_is_refused(self):
        with self.assertRaises(SettingsError):
            self.server.activate_settings('stale')
        self.assertFalse(self.server.restart_requested)

    def test_readiness_retries_reads_but_never_replays_a_lost_restart_request(self):
        calls, attempts = [], []
        def connection_factory(target):
            attempts.append(target)
            if len(attempts) < 3:
                raise ConnectionRefusedError()
            connection = SimpleNamespace(close=lambda: None)
            def request(method, path, **kwargs):
                calls.append((method, path))
                connection.method = method
            def response():
                if connection.method == 'POST':
                    raise ConnectionResetError()
                return SimpleNamespace(status=200, read=lambda size: b'{"restarting":false}')
            connection.request, connection.getresponse = request, response
            return connection
        configure_control(self.server, self.settings, str(self.settings.path.parent / 'seriald.sock'), connection_factory)
        saved = self.settings.save({'version': 1, 'environment': {'BAUD': '9600'}}, self.revision)
        with self.assertRaises(SettingsError) as context:
            self.server.activate_settings(saved['revision'])
        self.assertEqual(context.exception.status, 503)
        self.assertEqual(calls, [('GET', '/api/settings-health'), ('POST', '/api/settings-activate')])
        self.assertEqual(len(attempts), 4)
        self.assertFalse(self.server.restart_requested)


@unittest.skipUnless(os.name == 'posix', 'private Unix socket activation')
class RestartProcessTests(unittest.TestCase):
    def test_real_processes_reexec_without_discovering_host_serial_ports(self):
        with tempfile.TemporaryDirectory(prefix='seriald-settings-') as folder:
            base = Path(folder)
            (base / 'no-serial-devices').mkdir()
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            environment = dict(os.environ, SERIALD_BASE=folder, SERIALD_MCP_ENABLED='0',
                               SERIALD_BY_PATH=str(base / 'no-serial-devices'), SERIALD_AUTH_TOKEN='')
            processes = []
            logs = []
            try:
                for name, arguments in [('collector_server', []), ('control_server', [str(port)])]:
                    log = (base / (name + '.log')).open('w+')
                    logs.append(log)
                    command = ([sys.executable, '-c', 'import time; time.sleep(0.4); '
                                'from seriald_core.collector_server import main; main()']
                               if name == 'collector_server' else
                               [sys.executable, '-m', 'seriald_core.' + name] + arguments)
                    process = subprocess.Popen(command,
                                               cwd=ROOT, env=environment, stdout=log, stderr=log)
                    processes.append(process)

                def exchange(method, payload=None):
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                    try:
                        connection.request(method, '/api/settings', body=json.dumps(payload) if payload else None,
                                           headers={'Content-Type': 'application/json'})
                        response = connection.getresponse()
                        return response.status, json.loads(response.read())
                    finally:
                        connection.close()

                def wait_ready(revision=None):
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if any(process.poll() is not None for process in processes):
                            self.fail('isolated process exited: ' + '\n'.join(path.read_text() for path in base.glob('*.log')))
                        try:
                            status, result = exchange('GET')
                            if status == 200 and (revision is None or result['active_revision'] == revision):
                                return result
                        except (OSError, ValueError, http.client.HTTPException):
                            pass
                        time.sleep(0.05)
                    self.fail('isolated activation timed out: ' + '\n'.join(path.read_text() for path in base.glob('*.log')))

                before = wait_ready()
                status, result = exchange('POST', {'action': 'save', 'revision': before['revision'],
                    'configuration': {'version': 1, 'environment': {'BAUD': '9600'}}})
                self.assertEqual(status, 200, result)
                revision = result['revision']
                status, result = exchange('POST', {'action': 'activate', 'revision': revision})
                self.assertEqual(status, 202, result)
                after = wait_ready(revision)
                self.assertEqual(after['configuration']['environment']['BAUD'], '9600')
                self.assertEqual(after['configuration']['environment']['PORT'], str(port))
                self.assertTrue(all(process.poll() is None for process in processes))
                # Same PIDs remain: processes re-execute only after closing their
                # existing state, rather than spawning a competing collector.
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                try:
                    connection.request('GET', '/api/devices')
                    response = connection.getresponse()
                    result = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertEqual(result['devices'], [])
                finally:
                    connection.close()
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                for log in logs:
                    log.close()
