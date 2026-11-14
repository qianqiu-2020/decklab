"""MCP uses the existing HTTP boundary, tested without collectors or real ports."""
import asyncio
import base64
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

import seriald_mcp
from serialctl import ClientError
from seriald_mcp import Adapter, Settings

SDK = seriald_mcp.MCPServer is not None
ROOT = Path(__file__).resolve().parents[1]
TOKEN_A = 'seriald-ag-' + 'A' * 32
TOKEN_B = 'seriald-ag-' + 'B' * 32


class FakeAPI(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_api()

    def do_POST(self):
        self.handle_api()

    def handle_api(self):
        token = self.headers.get('Authorization', '').removeprefix('Bearer ')
        path = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(path.query).items()}
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length']))) if self.command == 'POST' else query
        self.server.calls.append((token, path.path, payload))
        if token not in self.server.active:
            return self.reply(401, {'error': 'invalid grant', 'code': 'unauthorized'})
        key = 'raw-A' if token == TOKEN_A else 'board-B'
        cid = self.server.connection_a if token == TOKEN_A else 'connection-B'
        if path.path == '/api/devices':
            return self.reply(200, {'devices': [{'key': key, 'label': key, 'serial': None if token == TOKEN_A else 'SNB',
                                                'connection_id': cid, 'online': True}]})
        if payload.get('key') != key:
            return self.reply(403, {'error': 'scope mismatch', 'code': 'forbidden'})
        if payload.get('connection_id') and payload['connection_id'] != cid:
            return self.reply(409, {'error': 'connection changed', 'code': 'stale'})
        if path.path == '/api/session':
            data = b'BusyBox ready\r\n# '
            return self.reply(200, {'key': key, 'sid': payload['sid'], 'off': len(data),
                                   'data_b64': base64.b64encode(data).decode(), 'alive': True})
        if path.path == '/api/session-send':
            return self.reply(200, {'ok': True, 'key': key, 'written': len(payload['data'])})
        if path.path == '/api/boot-log':
            data = 'boot line\nsecond\n'.encode()
            return self.reply(200, {'id': payload['id'], 'data': data.decode(),
                                   'data_b64': base64.b64encode(data).decode(), 'size': len(data)})
        if path.path == '/api/task-start':
            return self.reply(200, {'task': {'id': 'task1', 'state': 'running', 'kind': payload['kind']}})
        if path.path in ('/api/task', '/api/task-cancel'):
            return self.reply(200, {'task': {'id': payload['id'], 'state': 'cancelled' if self.command == 'POST' else 'running'}})
        self.reply(200, {'ok': True, 'key': key, 'tasks': [], 'files': [], 'boots': []})

    def reply(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class APICase(unittest.TestCase):
    def setUp(self):
        self.api = ThreadingHTTPServer(('127.0.0.1', 0), FakeAPI)
        self.api.active = {TOKEN_A, TOKEN_B}
        self.api.calls = []
        self.api.connection_a = 'connection-A'
        self.worker = threading.Thread(target=self.api.serve_forever, daemon=True)
        self.worker.start()
        self.url = 'http://127.0.0.1:%d' % self.api.server_port
        self.adapter = Adapter(Settings(self.url, TOKEN_A))

    def tearDown(self):
        self.api.shutdown()
        self.api.server_close()
        self.worker.join(timeout=2)


class AdapterTests(APICase):
    def test_unknown_os_read_send_and_exact_connection(self):
        selected = self.adapter.devices()['devices'][0]
        self.assertIsNone(selected['serial'])
        read = self.adapter.read(selected['key'], limit=5)
        self.assertEqual(read['data'], 'y\r\n# ')
        self.assertNotIn('data_b64', read)
        self.assertGreater(read['truncated_bytes'], 0)
        with self.assertRaisesRegex(ClientError, 'require connection_id'):
            self.adapter.send('raw-A', 'help')
        self.adapter.send('raw-A', 'help', enter=True, connection_id='connection-A')
        sent = self.api.calls[-1]
        self.assertEqual(sent[1], '/api/session-send')
        self.assertEqual(sent[2]['data'], 'help\r')
        self.assertEqual(sent[2]['connection_id'], 'connection-A')
        self.api.connection_a = 'replacement'
        with self.assertRaisesRegex(ClientError, 'connection changed'):
            self.adapter.send('raw-A', 'danger', connection_id='connection-A')
        self.assertEqual(sum(p == '/api/session-send' for _, p, _ in self.api.calls), 1)

    def test_separate_clients_and_tokens_never_share_selection(self):
        a = self.adapter.client(TOKEN_A, 'raw-A')
        b = self.adapter.client(TOKEN_B, 'board-B')
        self.assertIsNot(a, b)
        self.assertEqual((a.key, a.token), ('raw-A', TOKEN_A))
        self.assertEqual((b.key, b.token), ('board-B', TOKEN_B))
        self.assertNotIn(TOKEN_A, repr(self.adapter.settings))
        with self.assertRaises(ClientError):
            self.adapter.client(TOKEN_A, 'board-B')

    def test_command_preserves_uncertain_result_and_serializes_writes(self):
        client = self.adapter.client(TOKEN_B, 'board-B')
        with self.adapter.writing(client):
            with self.assertRaisesRegex(ClientError, 'Another MCP write'):
                self.adapter.send('board-B', 'hello', token=TOKEN_B)
        expected = {'ok': False, 'sent': True, 'completed': False, 'may_still_be_running': True}
        with mock.patch('seriald_mcp.execute', return_value=expected) as command:
            self.assertEqual(self.adapter.command('board-B', 'echo hi', token=TOKEN_B), expected)
            self.assertEqual(command.call_args.args[0].token, TOKEN_B)
        self.assertFalse(any(route == '/api/identify' for _, route, _ in self.api.calls))

    def test_boot_pagination_tasks_and_revoke(self):
        page = self.adapter.boot('raw-A', 'boot1', offset=5, limit=4)
        self.assertEqual((page['data'], page['off'], page['more']), ('line', 9, True))
        self.assertNotIn('data_b64', page)
        result = self.adapter.start_task('raw-A', 'identify', connection_id='connection-A')
        self.assertEqual(result['task']['id'], 'task1')
        self.assertEqual(self.adapter.task('raw-A', 'task1', cancel=True)['task']['state'], 'cancelled')
        self.api.active.remove(TOKEN_A)
        with self.assertRaises(ClientError) as caught:
            self.adapter.devices()
        self.assertEqual(caught.exception.status, 401)

    def test_hardware_discovery_and_typed_control_tasks_use_authenticated_api(self):
        self.adapter.hardware('board-B', token=TOKEN_B)
        self.assertEqual(self.api.calls[-1][1], '/api/hardware')
        for kind in ('hardware-reset', 'hardware-power-on', 'hardware-power-off', 'hardware-enter-download'):
            self.assertEqual(self.adapter.start_task('board-B', kind, token=TOKEN_B)['task']['kind'], kind)
            self.assertEqual(self.api.calls[-1][0], TOKEN_B)
        with self.assertRaises(ClientError):
            self.adapter.start_task('board-B', 'hardware-flash', token=TOKEN_B)

    def test_bounds_and_no_serial_close(self):
        for kwargs in ({'offset': -1}, {'offset': True}, {'limit': 65537}):
            with self.assertRaises(ClientError):
                self.adapter.read('raw-A', **kwargs)
        with self.assertRaises(ClientError):
            self.adapter.command('raw-A', 'true', timeout=float('nan'))
        with self.assertRaises(ClientError):
            self.adapter.session_close('raw-A', 'serial')
        with self.assertRaises(ClientError):
            self.adapter.start_task('raw-A', 'unknown', connection_id='connection-A')


class StartupFailureTests(unittest.TestCase):
    def test_missing_transitive_dependency_is_not_hidden_by_sdk_import_guard(self):
        output = io.StringIO()
        with mock.patch.object(seriald_mcp, 'MCPServer', None), \
                mock.patch.object(seriald_mcp, 'SDK_IMPORT_ERROR', 'No module named missing_native'), \
                redirect_stderr(output):
            self.assertEqual(seriald_mcp.main([]), 2)
        self.assertIn('No module named missing_native', output.getvalue())


@unittest.skipUnless(SDK, 'optional mcp==2.3.0 SDK is not installed')
class MCPTransportTests(APICase):
    def test_official_stdio_client_legacy_initialize_and_current_protocol(self):
        from mcp import Client as MCPClient, StdioServerParameters

        async def check(mode):
            params = StdioServerParameters(command=sys.executable, args=[str(ROOT / 'seriald_mcp.py')],
                env=dict(os.environ, SERIALD_URL=self.url, SERIALD_TOKEN=TOKEN_A))
            async with MCPClient(params, mode=mode) as client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools.tools}
                self.assertTrue({'list_devices', 'read_terminal', 'send_input', 'execute_command', 'start_task',
                                 'get_task', 'cancel_task', 'read_boot', 'hardware_actions'}.issubset(names))
                capabilities = await client.call_tool('hardware_actions', {'device': 'raw-A'})
                self.assertTrue(capabilities.structured_content['ok'])
                raw = await client.call_tool('list_devices', {})
                self.assertEqual(raw.structured_content['devices'][0]['key'], 'raw-A')
                read = await client.call_tool('read_terminal', {'device': 'raw-A'})
                self.assertIn('BusyBox', read.structured_content['data'])
                result = await client.call_tool('send_input', {'device': 'raw-A', 'data': 'help',
                    'connection_id': 'connection-A', 'enter': True})
                self.assertTrue(result.structured_content['ok'])
                started = await client.call_tool('start_task', {'device': 'raw-A', 'kind': 'identify',
                    'connection_id': 'connection-A'})
                self.assertEqual(started.structured_content['task']['id'], 'task1')
                tasks = await client.call_tool('list_tasks', {'device': 'raw-A'})
                self.assertEqual(tasks.structured_content['tasks'], [])
                task = await client.call_tool('get_task', {'device': 'raw-A', 'task_id': 'task1'})
                self.assertEqual(task.structured_content['task']['state'], 'running')
                cancelled = await client.call_tool('cancel_task', {'device': 'raw-A', 'task_id': 'task1'})
                self.assertEqual(cancelled.structured_content['task']['state'], 'cancelled')
                self.api.connection_a = 'new-generation'
                stale = await client.call_tool('send_input', {'device': 'raw-A', 'data': 'bad',
                    'connection_id': 'connection-A'})
                self.assertTrue(stale.is_error)
                self.api.connection_a = 'connection-A'
                self.api.active.remove(TOKEN_A)
                revoked = await client.call_tool('list_devices', {})
                self.assertTrue(revoked.is_error)
                self.api.active.add(TOKEN_A)
        asyncio.run(check('legacy'))
        asyncio.run(check('auto'))

    def test_http_auth_concurrent_scopes_and_revocation(self):
        from mcp import Client as MCPClient
        from mcp.client.streamable_http import streamable_http_client
        import httpx2
        with socket.socket() as handle:
            handle.bind(('127.0.0.1', 0))
            port = handle.getsockname()[1]
        endpoint = 'http://127.0.0.1:%d/mcp' % port
        # A process-wide token must never turn missing caller credentials into access.
        env = dict(os.environ, SERIALD_URL=self.url, SERIALD_TOKEN=TOKEN_B)
        with subprocess.Popen([sys.executable, str(ROOT / 'seriald_mcp.py'), '--transport', 'streamable-http',
                               '--port', str(port)], env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE) as process:
            try:
                deadline = time.monotonic() + 15
                while True:
                    if process.poll() is not None:
                        self.fail('HTTP adapter exited: ' + process.stderr.read().decode())
                    try:
                        urlopen(endpoint, timeout=.3)
                    except HTTPError as error:
                        self.assertEqual(error.code, 401)
                        error.close()
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail('HTTP adapter did not become ready')
                        time.sleep(.05)

                async def scoped(token, expected, mode):
                    async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + token}) as http:
                        transport = streamable_http_client(endpoint, http_client=http)
                        async with MCPClient(transport, mode=mode) as client:
                            tools = await client.list_tools()
                            self.assertIn('send_input', {tool.name for tool in tools.tools})
                            result = await client.call_tool('list_devices', {})
                            self.assertFalse(result.is_error, repr(result))
                            self.assertEqual(result.structured_content['devices'][0]['key'], expected)
                            return client

                async def concurrent():
                    for mode in ('legacy', 'auto'):
                        await asyncio.gather(scoped(TOKEN_A, 'raw-A', mode), scoped(TOKEN_B, 'board-B', mode))
                    self.api.active.remove(TOKEN_A)
                    async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + TOKEN_A}) as http:
                        response = await http.post(endpoint, json={'jsonrpc': '2.0', 'id': 1,
                                                                  'method': 'tools/list'})
                        self.assertEqual(response.status_code, 401)
                asyncio.run(concurrent())
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                process.stderr.close()


if __name__ == '__main__':
    unittest.main()
