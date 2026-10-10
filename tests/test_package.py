"""Extract the deb and run it against an empty fake serial directory, never install it."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from seriald_core.storage import Store
from seriald_core import __version__
from tools.build_deb import build, collector_build_id


@unittest.skipUnless(shutil.which('dpkg-deb'), 'requires dpkg-deb')
class PackageTests(unittest.TestCase):
    def test_collector_build_id_ignores_control_but_tracks_collector(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'seriald_core').mkdir()
            (root / 'packaging').mkdir()
            for name in ('__init__.py', 'admin.py', 'control_server.py', 'version.py',
                         'collector_server.py'):
                (root / 'seriald_core' / name).write_text(name)
            unit = root / 'packaging/seriald-collector.service'
            unit.write_text('collector unit')
            original = collector_build_id(root)
            (root / 'seriald_core/control_server.py').write_text('new control')
            (root / 'seriald_core/version.py').write_text("__version__ = 'next'")
            self.assertEqual(collector_build_id(root), original)
            (root / 'seriald_core/collector_server.py').write_text('new collector')
            self.assertNotEqual(collector_build_id(root), original)
            changed_collector = collector_build_id(root)
            unit.write_text('new collector unit')
            self.assertNotEqual(collector_build_id(root), changed_collector)

    def test_extracted_package_runs_and_preserves_offline_registry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = build(root / 'dist', __version__)
            extracted = root / 'extract'
            subprocess.run(['dpkg-deb', '--extract', str(package), str(extracted)], check=True,
                           stdout=subprocess.DEVNULL)
            subprocess.run(['dpkg-deb', '--control', str(package), str(root / 'control')], check=True)
            for script in ('preinst', 'postinst', 'prerm', 'postrm'):
                subprocess.run(['sh', '-n', str(root / 'control' / script)], check=True)
            preinst = (root / 'control/preinst').read_text()
            postinst = (root / 'control/postinst').read_text()
            self.assertIn('dpkg --compare-versions "$2" lt 1.3.2', postinst)
            self.assertIn('chmod 0660', postinst)
            self.assertIn('[ -e "$restart_marker" ]', postinst)
            self.assertIn('restart seriald-collector.service', postinst)
            self.assertIn('collector restarted (collector code changed)', postinst)
            self.assertIn('collector kept running (collector code unchanged)', postinst)
            self.assertIn('collector started (was not running)', postinst)
            address_script = postinst.split("<<'PY'", 1)[1].split('\n', 1)[1].split('\nPY\n', 1)[0]
            address_output = io.StringIO()
            with mock.patch('pathlib.Path.read_text', return_value='SERIALD_PORT=9123\nSERIALD_BIND=0.0.0.0\n'), \
                    mock.patch('seriald_core.control_server.lan_hosts', return_value=['10.0.120.161']), \
                    redirect_stdout(address_output):
                exec(compile(address_script, 'postinst-access', 'exec'), {})
            self.assertIn('http://127.0.0.1:9123/', address_output.getvalue())
            self.assertIn('http://10.0.120.161:9123/', address_output.getvalue())
            installed_build_id = (extracted / 'usr/lib/seriald/.collector-build-id').read_text().strip()
            self.assertEqual(installed_build_id, collector_build_id())
            self.assertIn(installed_build_id, preinst)
            old_id, marker = root / 'old-build-id', root / 'restart-required'
            old_id.write_text(installed_build_id + '\n')
            preinst_env = dict(os.environ, SERIALD_INSTALLED_BUILD_ID_FILE=str(old_id),
                               SERIALD_RESTART_MARKER=str(marker))
            subprocess.run(['sh', str(root / 'control/preinst'), 'upgrade', '1.3.6'],
                           check=True, env=preinst_env)
            self.assertFalse(marker.exists(), 'unchanged collector must keep running')
            old_id.write_text('different-build\n')
            subprocess.run(['sh', str(root / 'control/preinst'), 'upgrade', '1.3.6'],
                           check=True, env=preinst_env)
            self.assertTrue(marker.exists(), 'changed collector must restart')
            code = extracted / 'usr/lib/seriald'
            self.assertTrue((extracted / 'usr/share/doc/seriald/DEVICE_IDENTITY_MATRIX.md').is_file())
            self.assertTrue((extracted / 'usr/share/doc/seriald/REFACTOR_PLAN.md').is_file())
            self.assertTrue((extracted / 'usr/lib/seriald/skills/seriald-board/SKILL.md').is_file())
            package_env = (extracted / 'etc/seriald/seriald.env').read_text()
            self.assertIn('SERIALD_BASE=/data/seriald-data', package_env)
            self.assertIn('SERIALD_BIND=0.0.0.0', package_env)
            collector_unit = (extracted / 'lib/systemd/system/seriald-collector.service').read_text()
            control_unit = (extracted / 'lib/systemd/system/seriald.service').read_text()
            self.assertIn('RuntimeDirectory=seriald', collector_unit)
            self.assertNotIn('RuntimeDirectory=seriald', control_unit)
            self.assertIn('UMask=0007', collector_unit)
            self.assertIn('UMask=0007', control_unit)
            state, ports = root / 'state', root / 'empty-ports'
            ports.mkdir()
            store = Store(str(state / 'registry.sqlite3'))
            store.bind({'serial': 'TEST-SERIAL', 'hostname': 'test-board', 'ips': []}, 'usb-old', '/dev/old')
            store.close()
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            collector_socket = state / 'collector.sock'
            env = dict(os.environ, PYTHONPATH=str(code), SERIALD_BASE=str(state),
                       SERIALD_STATIC=str(code), SERIALD_SOCKET=str(collector_socket),
                       SERIALD_BY_PATH=str(ports), SERIALD_PORT=str(port), SERIALD_BIND='127.0.0.1')
            collector = subprocess.Popen(['python3', '-m', 'seriald_core.collector_server'], env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            control = None

            def start_control():
                return subprocess.Popen(['python3', '-m', 'seriald_core.control_server'], env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            def stop(process):
                if process is None:
                    return
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

            try:
                for _ in range(50):
                    if collector_socket.exists():
                        break
                    if collector.poll() is not None:
                        self.fail(collector.stdout.read().decode())
                    time.sleep(.05)
                else:
                    self.fail('collector socket did not start')
                collector_pid = collector.pid
                duplicate = subprocess.run(
                    ['python3', '-m', 'seriald_core.collector_server'], env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5)
                self.assertNotEqual(duplicate.returncode, 0)
                self.assertIn('already active', duplicate.stdout)
                self.assertIsNone(collector.poll(), 'duplicate launch must not disturb the live collector')
                control = start_control()
                url = 'http://127.0.0.1:%d' % port
                for _ in range(50):
                    try:
                        with urlopen(url + '/api/devices', timeout=.5) as response:
                            devices = json.load(response)['devices']
                        break
                    except (URLError, TimeoutError):
                        if control.poll() is not None:
                            self.fail(control.stdout.read().decode())
                        time.sleep(.05)
                else:
                    self.fail('isolated service did not start')
                self.assertEqual([(d['key'], d['serial'], d['state']) for d in devices],
                                 [('test-board', 'TEST-SERIAL', 'offline')])
                with urlopen(url + '/skills/seriald-board/SKILL.md') as response:
                    self.assertIn('charset=utf-8', response.headers['Content-Type'])
                    self.assertIn(b'seriald-board', response.read())
                with urlopen(url + '/serialctl.py') as response:
                    self.assertIn(b'class Client', response.read())
                share = Request(url + '/api/agent-share',
                                data=json.dumps({'scope': 'device', 'serial': 'TEST-SERIAL'}).encode(),
                                headers={'Content-Type': 'application/json'})
                with urlopen(share) as response:
                    grant = json.load(response)
                scoped = Request(url + '/api/devices', headers={'Authorization': 'Bearer ' + grant['token']})
                with urlopen(scoped) as response:
                    self.assertEqual(json.load(response)['devices'][0]['serial'], 'TEST-SERIAL')
                with urlopen(url + '/api/health') as response:
                    first_health = json.load(response)
                self.assertEqual(first_health['role'], 'collector')
                self.assertEqual(first_health['version'], __version__)
                self.assertEqual(first_health['pid'], collector_pid)
                with urlopen(url + '/static/app.js') as response:
                    self.assertIn('no-cache', response.headers['Cache-Control'])
                    self.assertIn(b'SerialdTerminalPolicy.install', response.read())
                with urlopen(url + '/static/terminal-policy.js') as response:
                    self.assertIn('no-cache', response.headers['Cache-Control'])
                    self.assertIn(b'registerCsiHandler', response.read())
                with urlopen(url + '/static/icons.svg') as response:
                    self.assertIn('image/svg+xml', response.headers['Content-Type'])
                    self.assertIn('no-cache', response.headers['Cache-Control'])
                    sprite = response.read()
                    self.assertIn(b'id="i-terminal"', sprite)
                    self.assertIn(b'id="i-transfer"', sprite)
                for route in ('/registry.sqlite3', '/vendor/%2e%2e/seriald.py'):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(url + route)
                    self.assertEqual(error.exception.code, 404)
                cli = subprocess.run(['python3', str(code / 'serialctl.py'), '--url', url, 'devices'],
                                     capture_output=True, text=True, timeout=5)
                self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
                self.assertEqual(json.loads(cli.stdout)['devices'][0]['key'], 'test-board')
                stop(control)
                control.stdout.close()
                control = start_control()
                for _ in range(50):
                    try:
                        with urlopen(url + '/api/devices', timeout=.5) as response:
                            restarted = json.load(response)['devices']
                        break
                    except (URLError, TimeoutError):
                        time.sleep(.05)
                else:
                    self.fail('control plane did not restart')
                with urlopen(scoped) as response:
                    self.assertEqual(json.load(response)['devices'][0]['serial'], 'TEST-SERIAL')
                self.assertIsNone(collector.poll(), 'collector must survive control restart')
                self.assertEqual(collector.pid, collector_pid)
                self.assertEqual(restarted[0]['key'], 'test-board')
                with urlopen(url + '/api/health') as response:
                    second_health = json.load(response)
                self.assertEqual(second_health['pid'], first_health['pid'])
                self.assertEqual(second_health['started'], first_health['started'])
            finally:
                stop(control)
                stop(collector)
                if control and control.stdout:
                    control.stdout.close()
                collector.stdout.close()
            restored = Store(str(state / 'registry.sqlite3'))
            self.assertEqual(restored.get('TEST-SERIAL')['label'], 'test-board')
            restored.close()
