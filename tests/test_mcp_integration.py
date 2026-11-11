"""Official MCP clients through real grant enforcement and a simulated serial attachment."""
import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

import seriald_mcp
from seriald_core.device import Device
from test_connection_access import protected_connection


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(seriald_mcp.MCPServer is not None, 'optional mcp==2.3.0 SDK is not installed')
class ProtectedMCPTests(unittest.TestCase):
    def test_official_stdio_client_reads_and_writes_unknown_os_with_real_scope_enforcement(self):
        from mcp import Client as MCPClient, StdioServerParameters
        with protected_connection() as fixture:
            device = fixture.device
            device.append(b'unknown-firmware> ')
            async def check():
                params = StdioServerParameters(command=sys.executable, args=[str(ROOT / 'seriald_mcp.py')],
                    env=dict(os.environ, SERIALD_URL=fixture.url, SERIALD_TOKEN=fixture.token))
                async with MCPClient(params, mode='auto') as client:
                    listed = await client.call_tool('list_devices', {})
                    self.assertFalse(listed.is_error, repr(listed))
                    selected = listed.structured_content['devices'][0]
                    self.assertEqual(selected['serial'], '')
                    read = await client.call_tool('read_terminal', {'device': selected['key']})
                    self.assertFalse(read.is_error, repr(read))
                    self.assertIn('unknown-firmware>', read.structured_content['data'])
                    sent = await client.call_tool('send_input', {'device': selected['key'],
                        'connection_id': selected['connection_id'], 'data': 'help', 'enter': True})
                    self.assertFalse(sent.is_error, repr(sent))
                    self.assertTrue(sent.structured_content['ok'])
                    fixture.send.assert_called_once_with('help\r')
                    forbidden = await client.call_tool('start_task', {'device': selected['key'],
                        'connection_id': selected['connection_id'], 'kind': 'ssh-enable'})
                    self.assertTrue(forbidden.is_error)
                    fixture.probe.assert_not_called()
                    device.connection_id = 'replacement'
                    stale = await client.call_tool('send_input', {'device': selected['key'],
                        'connection_id': selected['connection_id'], 'data': 'must not send'})
                    self.assertTrue(stale.is_error)
                    self.assertEqual(fixture.send.call_count, 1)
                    fixture.grant_store.revoke(fixture.grant['id'])
                    revoked = await client.call_tool('list_devices', {})
                    self.assertTrue(revoked.is_error)
            asyncio.run(check())

    def test_official_http_clients_keep_real_connection_grants_separate_and_revalidate_revoke(self):
        from mcp import Client as MCPClient
        from mcp.client.streamable_http import streamable_http_client
        import httpx2
        with protected_connection() as fixture:
            other = Device('fake-usb-other', 'FAKE-COM8', fixture.manager)
            other.fd, other.present = 98764, True
            fixture.manager.devices[other.by_path] = other
            other_token, _ = fixture.grant_store.issue('connection', key=other.key,
                                                     connection_id=other.connection_id)
            with socket.socket() as handle:
                handle.bind(('127.0.0.1', 0))
                port = handle.getsockname()[1]
            endpoint = 'http://127.0.0.1:%d/mcp' % port
            # An HTTP caller must supply its own grant even if the process has owner credentials.
            env = dict(os.environ, SERIALD_URL=fixture.url, SERIALD_TOKEN=fixture.owner_token)
            with subprocess.Popen([sys.executable, str(ROOT / 'seriald_mcp.py'), '--transport', 'streamable-http',
                                   '--port', str(port)], env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE) as process:
                try:
                    deadline = time.monotonic() + 15
                    while True:
                        if process.poll() is not None:
                            self.fail('MCP server exited: ' + process.stderr.read().decode())
                        try:
                            with urlopen(endpoint, timeout=.3):
                                self.fail('MCP HTTP accepted missing credentials')
                        except HTTPError as error:
                            self.assertEqual(error.code, 401)
                            error.close()
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                self.fail('MCP HTTP server did not become ready')
                            time.sleep(.05)

                    async def scoped(token, expected):
                        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + token}) as http:
                            transport = streamable_http_client(endpoint, http_client=http)
                            async with MCPClient(transport, mode='auto') as client:
                                listed = await client.call_tool('list_devices', {})
                                self.assertFalse(listed.is_error, repr(listed))
                                self.assertEqual([d['key'] for d in listed.structured_content['devices']], [expected])
                    async def check():
                        await asyncio.gather(scoped(fixture.token, fixture.device.key), scoped(other_token, other.key))
                        fixture.grant_store.revoke(fixture.grant['id'])
                        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + fixture.token}) as http:
                            revoked = await http.post(endpoint, json={'jsonrpc': '2.0', 'id': 1,
                                                                     'method': 'tools/list'})
                            self.assertEqual(revoked.status_code, 401)
                        await scoped(other_token, other.key)
                    asyncio.run(check())
                    fixture.probe.assert_not_called()
                    fixture.send.assert_not_called()
                finally:
                    other.fd, other.present = None, False
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    process.stderr.close()


if __name__ == '__main__':
    unittest.main()
