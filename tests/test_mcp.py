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

import decklab_mcp
from decklabctl import ClientError
from decklab_mcp import Adapter, Settings

SDK = decklab_mcp.MCPServer is not None
ROOT = Path(__file__).resolve().parents[1]
TOKEN_A = 'decklab-ag-' + 'A' * 32
TOKEN_B = 'decklab-ag-' + 'B' * 32


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
        if path.path == '/api/capabilities':
            from decklab_core.actions import capabilities
            return self.reply(200, capabilities().payload)
        if path.path == '/api/task' and payload.get('request_id'):
            return self.reply(200, {'task': {'id': 'original-task', 'request_id': payload['request_id'],
                                           'state': 'running', 'outcome': 'issued'}})
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
            return self.reply(200, {'task': {'id': 'task1', 'state': 'running', 'kind': payload['kind'],
                'request_id': payload.get('request_id'), 'outcome': 'issued'}})
        if path.path in ('/api/task', '/api/task-cancel'):
            return self.reply(200, {'task': {'id': payload.get('id', 'original-task'),
                'request_id': payload.get('request_id'),
                'state': 'cancelled' if self.command == 'POST' else 'running'}})
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
    def test_capabilities_never_selects_or_touches_a_device(self):
        result = self.adapter.capabilities()
        self.assertEqual(result['contract']['name'], 'decklab-execution')
        self.assertEqual(result['scope']['authorization'], 'not-a-grant')
        self.assertEqual([(path, payload) for _, path, payload in self.api.calls],
                         [('/api/capabilities', {})])

    def test_execution_lease_only_reaches_its_invocation_and_is_not_returned(self):
        proof = {'id': 'lease-A', 'generation': 1, 'token': 'private-proof-A'}
        with self.adapter.execution_scope(proof):
            result = self.adapter.send('raw-A', 'help', connection_id='connection-A')
        self.assertEqual(self.api.calls[-1][2]['execution_lease'], proof)
        self.assertNotIn(proof['token'], json.dumps(result))
        self.adapter.send('raw-A', 'next', connection_id='connection-A')
        self.assertNotIn('execution_lease', self.api.calls[-1][2])
        calls = len(self.api.calls)
        with self.assertRaises(ClientError):
            with self.adapter.execution_scope(dict(proof, generation=True)):
                self.adapter.send('raw-A', 'invalid', connection_id='connection-A')
        self.assertEqual(len(self.api.calls), calls)

    def test_concurrent_invocations_do_not_share_execution_lease_secrets(self):
        barrier, failures = threading.Barrier(2), []
        def send(token, device, connection, generation):
            try:
                with self.adapter.execution_scope({'id': 'lease-' + device, 'generation': generation,
                                                   'token': 'proof-' + device}):
                    barrier.wait(timeout=2)
                    self.adapter.send(device, 'help', connection_id=connection, token=token)
            except Exception as error:
                failures.append(error)
        workers = [threading.Thread(target=send, args=values) for values in (
            (TOKEN_A, 'raw-A', 'connection-A', 1), (TOKEN_B, 'board-B', 'connection-B', 2))]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        writes = {token: body for token, route, body in self.api.calls if route == '/api/session-send'}
        self.assertEqual(writes[TOKEN_A]['execution_lease']['token'], 'proof-raw-A')
        self.assertEqual(writes[TOKEN_B]['execution_lease']['token'], 'proof-board-B')
        self.assertIsNone(self.adapter.client().execution_lease)

    def test_task_request_id_is_forwarded_and_can_query_original_without_submission(self):
        self.adapter.start_task('raw-A', 'identify', connection_id='connection-A', request_id='identify-001')
        self.assertEqual(self.api.calls[-1][2]['request_id'], 'identify-001')
        before = len(self.api.calls)
        result = self.adapter.task('', request_id='identify-001')
        self.assertEqual(result['task']['request_id'], 'identify-001')
        queried = self.api.calls[before:]
        self.assertEqual(len(queried), 1)
        self.assertEqual(queried[0][1], '/api/task')
        self.assertEqual(queried[0][2]['request_id'], 'identify-001')
        with self.assertRaises(ClientError):
            self.adapter.task('', task_id='task1', request_id='identify-001')

    def test_adb_command_submits_one_durable_task_for_the_selected_board(self):
        result = self.adapter.adb_command('board-B', 'uname -a', 'adb-attempt-001', timeout=12, token=TOKEN_B)
        posts = [(token, payload) for token, route, payload in self.api.calls if route == '/api/task-start']
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0], (TOKEN_B, {'actor': 'decklab-mcp', 'key': 'board-B',
                                             'serial': 'SNB', 'connection_id': 'connection-B',
                                             'kind': 'adb-command', 'command': 'uname -a',
                                             'request_id': 'adb-attempt-001', 'timeout': 12}))
        self.assertEqual(result['task']['request_id'], 'adb-attempt-001')

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
        with mock.patch('decklab_mcp.execute', return_value=expected) as command:
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
        with mock.patch.object(decklab_mcp, 'MCPServer', None), \
                mock.patch.object(decklab_mcp, 'SDK_IMPORT_ERROR', 'No module named missing_native'), \
                redirect_stderr(output):
            self.assertEqual(decklab_mcp.main([]), 2)
        self.assertIn('No module named missing_native', output.getvalue())


@unittest.skipUnless(SDK, 'optional mcp==2.3.0 SDK is not installed')
class MCPTransportTests(APICase):
    def test_official_stdio_client_legacy_initialize_and_current_protocol(self):
        from mcp import Client as MCPClient, StdioServerParameters

        async def check(mode):
            params = StdioServerParameters(command=sys.executable, args=[str(ROOT / 'decklab_mcp.py')],
                env=dict(os.environ, DECKLAB_URL=self.url, DECKLAB_TOKEN=TOKEN_A))
            async with MCPClient(params, mode=mode) as client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools.tools}
                self.assertTrue({'get_capabilities', 'get_input_owner', 'claim_board', 'renew_board', 'release_board',
                                 'list_devices', 'read_terminal', 'send_input', 'execute_command', 'execute_adb_command', 'start_task',
                                 'get_task', 'cancel_task', 'read_boot', 'hardware_actions'}.issubset(names))
                schemas = {tool.name: tool.input_schema['properties'] for tool in tools.tools}
                for name in ('send_input', 'execute_command', 'execute_adb_command', 'start_task', 'desktop_input',
                             'start_flash', 'confirm_flash_selection', 'set_boot_priority'):
                    self.assertIn('execution_lease', schemas[name])
                self.assertIn('discover', schemas['list_desktop_sessions'])
                for preview in ('preview_flash', 'preview_flash_selection'):
                    self.assertIn('none', schemas[preview]['verify']['enum'])
                    self.assertEqual(schemas[preview]['reset_after']['default'], False)
                self.assertIn('request_id', schemas['start_task'])
                self.assertIn('request_id', schemas['get_task'])
                self.assertNotIn('execution_lease', schemas['get_capabilities'])
                protocol = await client.call_tool('get_capabilities', {})
                self.assertEqual(protocol.structured_content['contract']['revision'], 1)
                self.assertFalse(protocol.structured_content['scope']['cross_platform_exclusive'])
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
        env = dict(os.environ, DECKLAB_URL=self.url, DECKLAB_TOKEN=TOKEN_B)
        with subprocess.Popen([sys.executable, str(ROOT / 'decklab_mcp.py'), '--transport', 'streamable-http',
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
