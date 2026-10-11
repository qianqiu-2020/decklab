"""Collector handlers use explicit per-server state; importing API opens nothing."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from seriald_core import web
from seriald_core.devices import Manager
from seriald_core.httpd import BoundedThreadingTCPServer
from seriald_core.storage import Store


class CollectorInjectionTests(unittest.TestCase):
    def test_import_does_not_create_manager_read_profiles_or_create_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ, SERIALD_BASE=directory + '/unused',
                               SERIALD_BAUD='invalid', SERIALD_PROFILES=directory + '/missing.json',
                               SERIALD_HARDWARE=directory + '/missing-hardware.json')
            root = Path(__file__).resolve().parents[1]
            result = subprocess.run([sys.executable, '-c',
                'from seriald_core import web; assert web.MGR is None'],
                cwd=root, env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((Path(directory) / 'unused').exists())

    def test_independent_collectors_do_not_share_directory_or_mutations(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(mock.patch.object(web, 'LOG_ROOT', directory))
            poison = mock.Mock()
            stack.enter_context(mock.patch.object(web, 'MGR', poison))
            servers, workers, urls = [], [], []
            for name in ('alpha', 'beta'):
                store = Store(':memory:')
                stack.callback(store.close)
                store.bind({'serial': name, 'serial_source': 'device-tree'}, 'fake-' + name, '', name)
                manager = Manager(store)
                stack.callback(manager.close)
                server = stack.enter_context(BoundedThreadingTCPServer(('127.0.0.1', 0), web.Handler))
                server.manager = manager
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                servers.append(server)
                workers.append(worker)
                urls.append('http://127.0.0.1:%d' % server.server_address[1])
            try:
                for url, name in zip(urls, ('alpha', 'beta')):
                    with urlopen(url + '/api/devices', timeout=3) as result:
                        self.assertEqual([row['key'] for row in json.load(result)['devices']], [name])
                body = json.dumps({'key': 'alpha', 'serial': 'alpha', 'label': 'renamed'}).encode()
                with urlopen(Request(urls[0] + '/api/label', body,
                             {'Content-Type': 'application/json'}), timeout=3) as result:
                    self.assertTrue(json.load(result)['ok'])
                with urlopen(urls[1] + '/api/devices', timeout=3) as result:
                    self.assertEqual(json.load(result)['devices'][0]['key'], 'beta')
                with self.assertRaises(HTTPError) as error:
                    urlopen(urls[1] + '/api/operation?key=renamed', timeout=3)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
                self.assertEqual(poison.mock_calls, [])
            finally:
                for server in servers:
                    server.shutdown()
                for worker in workers:
                    worker.join(timeout=3)

    def test_unconfigured_server_returns_unavailable_instead_of_initializing(self):
        with mock.patch.object(web, 'MGR', None), \
                BoundedThreadingTCPServer(('127.0.0.1', 0), web.Handler) as server:
            server.manager = None
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with self.assertRaises(HTTPError) as error:
                    urlopen('http://127.0.0.1:%d/api/devices' % server.server_address[1], timeout=3)
                self.assertEqual(error.exception.code, 503)
                self.assertEqual(json.load(error.exception)['code'], 'unavailable')
                error.exception.close()
            finally:
                server.shutdown()
                worker.join(timeout=3)
