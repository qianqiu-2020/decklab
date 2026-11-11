"""Protected control/collector integration on fake attachments; never opens a tty."""
from contextlib import contextmanager
import json
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from seriald_core import control_server, device as device_module, web
from seriald_core.agent_grants import AgentGrantStore
from seriald_core.devices import Manager
from seriald_core.httpd import BoundedThreadingTCPServer
from seriald_core.profiles import ProfileBook
from seriald_core.storage import Store


def api_request(url, route, token, payload=None, query=None):
    if query:
        route += '?' + urlencode(query)
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(url + route, data=data, headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as response:
        with response:
            return response.code, json.load(response)


@contextmanager
def protected_connection():
    """A real authenticated API with manual/unknown OS and in-memory registry."""
    with tempfile.TemporaryDirectory() as directory, mock.patch.object(device_module, 'LOG_ROOT', directory):
        store, grants = Store(':memory:'), AgentGrantStore(':memory:')
        manager = Manager(store, profiles=ProfileBook({
            'default': 'raw', 'profiles': {'raw': {'probe': 'manual'}}}))
        device = device_module.Device('fake-usb-port', 'FAKE-COM7', manager)
        device.fd, device.present = 98765, True
        manager.devices[device.by_path] = device
        class Control(control_server.ControlHandler):
            auth_token = 'fixture-owner-token'
            grant_store = grants
            trust_loopback = False
            agent_tokens = {}
        with BoundedThreadingTCPServer(('127.0.0.1', 0), web.Handler) as collector, \
                BoundedThreadingTCPServer(('127.0.0.1', 0), Control) as control, \
                mock.patch.object(device, 'send', return_value=(True, '')) as send, \
                mock.patch.object(device, 'run_probe', side_effect=AssertionError('unexpected OS probe')) as probe:
            collector.manager = manager
            Control.collector_socket = collector.server_address
            workers = [threading.Thread(target=server.serve_forever, daemon=True)
                       for server in (collector, control)]
            for worker in workers:
                worker.start()
            fixture = SimpleNamespace(url='http://127.0.0.1:%d' % control.server_address[1],
                owner_token=Control.auth_token, grant_store=grants, manager=manager,
                device=device, send=send, probe=probe)
            try:
                status, issued = api_request(fixture.url, '/api/agent-share', fixture.owner_token,
                    {'scope': 'connection', 'key': device.key, 'connection_id': device.connection_id})
                if status != 200:
                    raise AssertionError('could not issue connection grant: ' + repr(issued))
                fixture.token, fixture.grant = issued['token'], issued['grant']
                yield fixture
            finally:
                device.fd, device.present = None, False
                with device.cv:
                    device.cv.notify_all()
                for server in (collector, control):
                    server.shutdown()
                for worker in workers:
                    worker.join(timeout=3)
                manager.close()
                grants.close()
                store.close()


class ConnectionAccessTests(unittest.TestCase):
    def test_unidentified_raw_access_and_grant_revocation_use_real_protected_api(self):
        with protected_connection() as fixture:
            device, token = fixture.device, fixture.token
            status, listed = api_request(fixture.url, '/api/devices', token)
            self.assertEqual(status, 200)
            self.assertEqual([item['key'] for item in listed['devices']], [device.key])
            self.assertEqual(listed['devices'][0]['serial'], '')
            device.append(b'unsupported-os> ')
            status, terminal = api_request(fixture.url, '/api/session', token,
                query={'key': device.key, 'sid': 'serial', 'off': 0})
            self.assertEqual(status, 200)
            self.assertIn('unsupported-os>', terminal['data'])
            status, _ = api_request(fixture.url, '/api/session-send', token,
                {'key': device.key, 'sid': 'serial', 'data': 'help\r'})
            self.assertEqual(status, 200)
            fixture.send.assert_called_once_with('help\r')
            fixture.probe.assert_not_called()
            fixture.grant_store.revoke(fixture.grant['id'])
            self.assertEqual(api_request(fixture.url, '/api/devices', token)[0], 401)

    def test_same_connection_remains_discoverable_after_identification_but_replacement_is_denied(self):
        with protected_connection() as fixture:
            device, token = fixture.device, fixture.token
            # Simulate a separately completed identity bind without exercising a shell.
            record = fixture.manager.store.bind({'serial': 'SN-FIXTURE', 'serial_source': 'device-tree'},
                device.by_path, device.dev, 'identified-label')
            device.record = record
            device.board.attach(device)
            fixture.manager.boards[device.serial] = device.board
            status, listed = api_request(fixture.url, '/api/devices', token)
            self.assertEqual(status, 200)
            self.assertEqual([item['key'] for item in listed['devices']], ['identified-label'])
            old_connection = device.connection_id
            device.connection_id = 'replacement-connection'
            status, listed = api_request(fixture.url, '/api/devices', token)
            self.assertEqual(status, 200)
            self.assertEqual(listed['devices'], [])
            status, _ = api_request(fixture.url, '/api/session-send', token,
                {'key': device.key, 'connection_id': old_connection, 'sid': 'serial', 'data': 'help\r'})
            self.assertEqual(status, 404)
            fixture.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
