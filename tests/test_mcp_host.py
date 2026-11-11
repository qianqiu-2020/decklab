"""MCP worker lifecycle and public single-port access; no real serial devices."""
import asyncio
import json
import os
from pathlib import Path
import sys
import queue
import shutil
import socket
import subprocess
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import seriald_mcp
from seriald_core.mcp_host import MCPHost, local_control_url
from test_connection_access import protected_connection, api_request


class MCPHostTests(unittest.TestCase):
    def test_local_callback_reaches_specific_bind_addresses_and_formats_ipv6(self):
        for address, expected in ((('0.0.0.0', 8898), 'http://127.0.0.1:8898'),
                                  (('127.0.0.2', 9001), 'http://127.0.0.2:9001'),
                                  (('10.0.90.98', 8898), 'http://10.0.90.98:8898'),
                                  (('::', 8898, 0, 0), 'http://[::1]:8898'),
                                  (('2001:db8::1', 8898, 0, 0), 'http://[2001:db8::1]:8898')):
            self.assertEqual(local_control_url(SimpleNamespace(server_address=address)), expected)

    def test_disabled_worker_does_not_spawn_or_require_the_sdk(self):
        with mock.patch.dict(os.environ, {'SERIALD_MCP_ENABLED': '0'}), \
                mock.patch('seriald_core.mcp_host.subprocess.Popen') as spawn:
            with MCPHost('http://127.0.0.1:8898') as host:
                self.assertIsNone(host.target)
                self.assertEqual(host.snapshot(), {'mcp_enabled': False, 'mcp_url': '/mcp',
                                                   'mcp_error': '服务端 MCP 已停用'})
            spawn.assert_not_called()

    def test_missing_server_component_is_reported_without_stopping_control(self):
        host = MCPHost('http://127.0.0.1:8898', startup_timeout=2)
        with mock.patch.object(host, '_command', return_value=[sys.executable, '-c', 'pass']):
            with host:
                self.assertFalse(host.snapshot()['mcp_enabled'])
                self.assertIn('服务端 MCP 组件不可用', host.snapshot()['mcp_error'])
                self.assertIsNone(host.target)
            self.assertIsNone(host.process)
            self.assertIsNone(host._temporary)

    def test_server_python_is_one_executable_and_frozen_host_reuses_its_dispatch(self):
        host = MCPHost('http://127.0.0.1:8898')
        with mock.patch.dict(os.environ, {'SERIALD_MCP_PYTHON': '/opt/a path/python'}), \
                mock.patch.object(sys, 'frozen', False, create=True):
            command = host._command(Path('/tmp/readiness.json'))
            self.assertEqual(command[0], '/opt/a path/python')
            self.assertIn(str(Path('/tmp/readiness.json')), command)
            self.assertEqual(command[-2:], ['--parent-pid', str(os.getpid())])
        with mock.patch.object(sys, 'frozen', True, create=True):
            self.assertEqual(host._command(Path('/tmp/status'))[:2], [sys.executable, 'mcp'])


@unittest.skipUnless(seriald_mcp.MCPServer is not None, 'optional server-side MCP SDK is not installed')
class PublicMCPHostTests(unittest.TestCase):
    def test_abrupt_parent_exit_does_not_leave_a_private_mcp_listener_running(self):
        program = ('import json,time; from seriald_core.mcp_host import MCPHost; '
                   "h=MCPHost('http://127.0.0.1:9'); h.start(); "
                   "print(json.dumps({'target':h.target,'temporary':h._temporary.name if h._temporary else None}),flush=True); "
                   'time.sleep(60)')
        environment = dict(os.environ, SERIALD_MCP_ENABLED='1')
        child = subprocess.Popen([sys.executable, '-c', program], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, env=environment)
        output = queue.Queue()
        reader = threading.Thread(target=lambda: output.put(child.stdout.readline()), daemon=True)
        reader.start()
        directory = None
        try:
            details = json.loads(output.get(timeout=20))
            directory = details['temporary']
            self.assertIsNotNone(details['target'], details)
            address = tuple(details['target'])
            child.kill()
            child.wait(timeout=5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(address, timeout=.2):
                        pass
                except OSError:
                    break
                time.sleep(.05)
            else:
                self.fail('MCP worker kept listening after its host exited')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            reader.join(timeout=2)
            child.stdout.close()
            if directory:
                shutil.rmtree(directory, ignore_errors=True)

    def test_official_remote_client_uses_only_public_control_url_and_revocable_grant(self):
        from mcp import Client as MCPClient
        from mcp.client.streamable_http import streamable_http_client
        import httpx2

        with protected_connection() as fixture:
            with MCPHost(fixture.url) as host:
                fixture.control.mcp_host = host
                self.assertTrue(host.snapshot()['mcp_enabled'], host.snapshot())
                child = host.process
                port = host.target[1]
                status, access = api_request(fixture.url, '/api/access', fixture.owner_token)
                self.assertEqual(status, 200)
                self.assertTrue(access['mcp_enabled'])
                self.assertEqual(access['mcp_url'], '/mcp')
                fixture.device.append(b'unknown-rtos> ')

                async def run():
                    async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + fixture.token}) as http:
                        transport = streamable_http_client(fixture.url + '/mcp', http_client=http)
                        async with MCPClient(transport, mode='legacy') as client:
                            tools = await client.list_tools()
                            self.assertIn('read_terminal', {tool.name for tool in tools.tools})
                            listed = await client.call_tool('list_devices', {})
                            self.assertFalse(listed.is_error, repr(listed))
                            selected = listed.structured_content['devices'][0]
                            self.assertFalse(selected['serial'])
                            read = await client.call_tool('read_terminal', {'device': selected['key']})
                            self.assertIn('unknown-rtos>', read.structured_content['data'])
                            sent = await client.call_tool('send_input', {'device': selected['key'],
                                'connection_id': selected['connection_id'], 'data': 'help', 'enter': True})
                            self.assertFalse(sent.is_error, repr(sent))
                            fixture.send.assert_called_once_with('help\r')
                            fixture.probe.assert_not_called()
                        fixture.grant_store.revoke(fixture.grant['id'])
                        denied = await http.post(fixture.url + '/mcp', json={
                            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
                        self.assertEqual(denied.status_code, 401)
                asyncio.run(run())
            self.assertIsNotNone(child.poll())
            with self.assertRaises(OSError):
                urlopen('http://127.0.0.1:%d/mcp' % port, timeout=.2)

    def test_worker_restart_never_replays_a_tool_request(self):
        with protected_connection() as fixture, MCPHost(fixture.url) as host:
            fixture.control.mcp_host = host
            self.assertTrue(host.snapshot()['mcp_enabled'], host.snapshot())
            old = host.process
            old.terminate()
            old.wait(timeout=5)
            self.assertFalse(host.snapshot()['mcp_enabled'])
            self.assertIsNotNone(host.target)
            self.assertIsNot(host.process, old)
            self.assertTrue(host.snapshot()['mcp_enabled'])
            fixture.send.assert_not_called()
            fixture.probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
