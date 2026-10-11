"""Demo must exercise production APIs without hardware or command execution."""
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.request import Request, urlopen

from seriald_core.demo import DemoManager
from seriald_core.storage import Store
from seriald_core.file_transfer import BATCH_SIZE
import serialctl


ROOT = Path(__file__).resolve().parents[1]


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        for module in ('seriald_core.device', 'seriald_core.devices'):
            patcher = mock.patch(module + '.LOG_ROOT', self.temporary.name)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.manager = DemoManager(self.store)
        self.addCleanup(self.manager.close)
        self.device = self.manager.resolve('demo-k1')

    def test_demo_never_discovers_opens_hardware_or_executes_host_commands(self):
        with mock.patch('seriald_core.devices.os.scandir', side_effect=AssertionError('discovery')), \
                mock.patch('seriald_core.devices.open_serial', side_effect=AssertionError('hardware')), \
                mock.patch('subprocess.Popen', side_effect=AssertionError('command execution')):
            self.assertEqual(self.manager.scan(), {})
            self.assertFalse(self.manager._matches(self.device))
            session, error = self.device.ssh_connect(actor='demo-agent')
            self.assertEqual(error, '')
            self.assertIs(self.device.ssh_connect(actor='demo-agent')[0], session)
            self.assertTrue(session.write('uname -a\r'))
            self.assertIn(b'6.18.0-demo', session.read(0)[1])
            self.assertTrue(session.write('touch /tmp/not-a-host-file\r'))
            self.assertIn(b'simulated shell', session.read(0)[1])
            session.close()
            self.assertFalse(session.write('echo closed\r'))

    def test_agent_command_frame_and_boot_archives(self):
        frame = serialctl.frame_command('echo hello-agent', 'abc123')
        self.assertTrue(self.device.send(frame)[0])
        raw = self.device.read(0)[1].decode().replace('\r\n', '\n')
        self.assertIn('__SD_AGENT_B_abc123__\nhello-agent\n', raw)
        self.assertIn('__SD_AGENT_E_abc123__:0\n', raw)
        boots = self.device.boot_epochs()
        self.assertEqual(len(boots), 2)
        self.assertTrue(any(boot['cross_day'] for boot in boots))

    def test_upload_download_batch_checksum_and_abort(self):
        transfer = self.device.file_transfer
        connection = self.device.connection_id
        # Compressible and random data exercise gzip and line splitting.
        payload = b'demo\n' * 3000 + os.urandom(12000)
        state = transfer.start('upload', '/tmp/test file.bin', connection, size=len(payload))
        offset = 0
        while offset < len(payload):
            part = payload[offset:offset + BATCH_SIZE]
            result = transfer.batch(state['id'], connection, offset,
                                    base64.b64encode(part).decode())
            offset = result['offset']
        done = transfer.finish(state['id'], connection)
        self.assertEqual(done['sha256'], hashlib.sha256(payload).hexdigest())
        self.assertEqual(self.device.files['/tmp/test file.bin'], payload)
        state = transfer.start('download', '/tmp/test file.bin', connection)
        output = bytearray()
        while len(output) < len(payload):
            result = transfer.chunk(state['id'], connection, len(output))
            output.extend(base64.b64decode(result['data_b64']))
        transfer.finish(state['id'], connection)
        self.assertEqual(output, payload)
        state = transfer.start('upload', '/tmp/abort.bin', connection, size=5)
        temp = transfer.state['temp']
        transfer.abort(state['id'], connection)
        self.assertNotIn(temp, self.device.files)


class DemoHttpTests(unittest.TestCase):
    def test_launcher_http_cli_and_production_path_isolation(self):
        with tempfile.TemporaryDirectory() as forbidden:
            env = dict(os.environ, SERIALD_BASE=forbidden,
                       SERIALD_PROFILES='/does/not/exist', SERIALD_AUTH_TOKEN='production-token')
            process = subprocess.Popen([sys.executable, str(ROOT / 'seriald_demo.py'), '--port', '0'],
                                       cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                lines = queue.Queue()
                threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
                line = lines.get(timeout=10)
                match = re.search(r'http://127.0.0.1:\d+/', line)
                self.assertIsNotNone(match, line)
                url = match.group(0).rstrip('/')

                def request(route, data=None):
                    encoded = json.dumps(data).encode() if data is not None else None
                    with urlopen(Request(url + route, data=encoded,
                                         headers={'Content-Type': 'application/json'}), timeout=5) as response:
                        return json.load(response)

                devices = request('/api/devices')['devices']
                self.assertTrue(request('/api/capabilities')['features']['operation_cancel'])
                self.assertEqual(len(devices), 3)
                self.assertEqual(sum(d['state'] == 'online' for d in devices), 2)
                with urlopen(url + '/', timeout=5) as response:
                    self.assertIn(b'DeckLab', response.read())
                session = request('/api/ssh-connect', {'key': 'demo-k1', 'actor': 'demo-agent'})
                self.assertTrue(session['ok'])
                logs = request('/api/logs?key=demo-k1')
                self.assertEqual(len(logs['boots']), 2)
                cli = subprocess.run([sys.executable, str(ROOT / 'serialctl.py'), '--url', url, 'devices'],
                                     capture_output=True, text=True, timeout=10)
                self.assertEqual(cli.returncode, 0, cli.stderr)
                self.assertIn('demo-k1', cli.stdout)
                def run_cli(device, *arguments):
                    result = subprocess.run([sys.executable, str(ROOT / 'serialctl.py'),
                                             '--url', url, '--device', device, *arguments],
                                            capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                    return result.stdout

                self.assertIn('hello-agent', run_cli('demo-k1', 'exec', '--command', 'echo hello-agent'))
                run_cli('demo-k1', 'file-copy', '--remote', '/root/log.txt',
                        '--to-device', 'demo-k3', '--to-path', '/tmp/copied.log')
                with tempfile.TemporaryDirectory() as download:
                    destination = Path(download) / 'download.log'
                    run_cli('demo-k3', 'file-get', '--remote', '/tmp/copied.log',
                            '--local', str(destination))
                    self.assertEqual(destination.read_bytes(),
                                     (b'[    0.000000] Linux version 6.18.0-demo\n'
                                      b'[    1.234000] mmc0: initialized\n') * 512)
                self.assertEqual(list(Path(forbidden).iterdir()), [])
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()
                process.stderr.close()


if __name__ == '__main__':
    unittest.main()
