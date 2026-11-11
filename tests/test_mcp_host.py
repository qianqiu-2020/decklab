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
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import seriald_mcp
from seriald_core.mcp_host import LOG_LIMIT, MCPHost, _WorkerLog, local_control_url
from test_connection_access import protected_connection, api_request


class IsolatedMCPHostTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='decklab-mcp-test-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.base_patch = mock.patch('seriald_core.mcp_host.BASE', str(self.directory))
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)
        environment = mock.patch.dict(os.environ, {'SERIALD_BASE': str(self.directory),
            'SERIALD_MCP_ENABLED': '1', 'SERIALD_MCP_PYTHON': ''})
        environment.start()
        self.addCleanup(environment.stop)


class MCPHostTests(IsolatedMCPHostTestCase):
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

    def test_early_exit_is_reported_without_stopping_control(self):
        host = MCPHost('http://127.0.0.1:8898', startup_timeout=2)
        with mock.patch.object(host, '_command', return_value=[sys.executable, '-c', 'raise SystemExit(23)']):
            with host:
                self.assertFalse(host.snapshot()['mcp_enabled'])
                self.assertIn('工作进程提前退出（退出码 23）', host.snapshot()['mcp_error'])
                self.assertIn(str(self.directory / 'mcp-worker.log'), host.snapshot()['mcp_error'])
                self.assertIsNone(host.target)
                self.assertIn('exited with code 23', (self.directory / 'mcp-worker.log').read_text())
            self.assertIsNone(host.process)
            self.assertIsNone(host._temporary)

    def test_dependency_error_is_logged_privately_without_exposing_tokens_to_ui(self):
        program = ('import sys; '
                   'print("ModuleNotFoundError: mcp Bearer hidden-token seriald-ag-secret", file=sys.stderr); '
                   'raise SystemExit(1)')
        host = MCPHost('http://127.0.0.1:8898', startup_timeout=2)
        with mock.patch.object(host, '_command', return_value=[sys.executable, '-c', program]), host:
            self.assertIn('依赖加载失败（退出码 1）', host.error)
            self.assertNotIn('ModuleNotFoundError', host.error)
            log = (self.directory / 'mcp-worker.log').read_text()
            self.assertIn('ModuleNotFoundError', log)
            self.assertNotIn('hidden-token', log)
            self.assertNotIn('seriald-ag-secret', log)
            if os.name != 'nt':
                self.assertEqual((self.directory / 'mcp-worker.log').stat().st_mode & 0o777, 0o600)

    def test_adapter_missing_sdk_diagnostic_is_classified_as_dependency_failure(self):
        message = seriald_mcp.sdk_unavailable_message()
        program = 'import sys; print(sys.argv[1], file=sys.stderr); raise SystemExit(1)'
        host = MCPHost('http://127.0.0.1:8898', startup_timeout=2)
        with mock.patch.object(host, '_command', return_value=[sys.executable, '-c', program, message]), host:
            self.assertIn('依赖加载失败（退出码 1）', host.error)
            self.assertIn('MCP SDK import failed', (self.directory / 'mcp-worker.log').read_text())

    def test_source_entry_point_does_not_follow_custom_static_assets(self):
        host = MCPHost('http://127.0.0.1:8898')
        with mock.patch('seriald_core.mcp_host.STATIC', str(self.directory / 'assets'), create=True), \
                mock.patch.dict(os.environ, {'SERIALD_STATIC': str(self.directory / 'assets')}), \
                mock.patch.object(sys, 'frozen', False, create=True):
            command = host._command(self.directory / 'ready.json')
        self.assertEqual(Path(command[1]), Path(__file__).resolve().parents[1] / 'seriald_mcp.py')

    def test_bundled_worker_is_selected_before_source_python_and_override_takes_priority(self):
        bundle = self.directory / 'mcp-runtime' / 'seriald-mcp-worker'
        bundle.parent.mkdir()
        bundle.write_text('#!/bin/sh\n')
        bundle.chmod(0o755)
        (self.directory / 'seriald_mcp.py').write_text('')
        host = MCPHost('http://127.0.0.1:8898')
        with mock.patch('seriald_core.mcp_host.CODE_ROOT', self.directory), \
                mock.patch.object(sys, 'frozen', False, create=True):
            self.assertEqual(host._command(self.directory / 'ready')[0], str(bundle))
            with mock.patch.dict(os.environ, {'SERIALD_MCP_PYTHON': '/custom/python'}):
                self.assertEqual(host._command(self.directory / 'ready')[:2],
                                 ['/custom/python', str(self.directory / 'seriald_mcp.py')])

    @unittest.skipIf(os.name == 'nt', 'POSIX executable permissions only apply to the Linux bundle')
    def test_nonexecutable_bundle_is_an_installation_error_without_fallback(self):
        bundle = self.directory / 'mcp-runtime' / 'seriald-mcp-worker'
        bundle.parent.mkdir()
        bundle.write_text('invalid runtime')
        bundle.chmod(0o644)
        with mock.patch('seriald_core.mcp_host.CODE_ROOT', self.directory), \
                mock.patch.object(sys, 'frozen', False, create=True), \
                mock.patch('seriald_core.mcp_host.subprocess.Popen') as spawn:
            with MCPHost('http://127.0.0.1:8898') as host:
                self.assertIn('内置 MCP 运行程序不可执行', host.error)
                self.assertIsNone(host.target)
            spawn.assert_not_called()

    def test_missing_executable_permission_and_log_directory_errors_are_specific(self):
        for exception, expected in ((FileNotFoundError(2, 'missing'), '启动程序不存在'),
                                    (PermissionError(13, 'denied'), '启动程序权限不足')):
            with self.subTest(exception=exception), \
                    mock.patch('seriald_core.mcp_host.subprocess.Popen', side_effect=exception):
                with MCPHost('http://127.0.0.1:8898') as host:
                    self.assertIn(expected, host.error)
                    self.assertIsNone(host.process)
                    self.assertIsNone(host._temporary)
        with mock.patch('seriald_core.mcp_host._WorkerLog', side_effect=PermissionError(13, 'denied')), \
                mock.patch('seriald_core.mcp_host.subprocess.Popen') as spawn:
            with MCPHost('http://127.0.0.1:8898') as host:
                self.assertIn('无法创建诊断日志', host.error)
            spawn.assert_not_called()

    def test_invalid_ready_document_and_timeout_are_reported_and_cleaned_up(self):
        host = MCPHost('http://127.0.0.1:8898', startup_timeout=.2)

        def invalid_command(ready):
            program = ('from pathlib import Path; import sys,time; '
                       'Path(sys.argv[1]).write_text(\'{"host":"0.0.0.0","port":1234}\'); time.sleep(5)')
            return [sys.executable, '-c', program, str(ready)]

        with mock.patch.object(host, '_command', side_effect=invalid_command), host:
            self.assertIn('返回的启动信息无效', host.error)
            self.assertIsNone(host._temporary)
        host = MCPHost('http://127.0.0.1:8898', startup_timeout=.2)
        with mock.patch.object(host, '_command', return_value=[sys.executable, '-c', 'import time; time.sleep(5)']), host:
            self.assertIn('启动超时', host.error)
            self.assertIn('0.2 秒', host.error)
            self.assertIsNone(host.process)

    def test_large_child_output_is_continuously_drained_and_log_size_is_bounded(self):
        program = ('import sys; '
                   'sys.stdout.write("x" * 200000 + "\\n"); '
                   'sys.stdout.write("diagnostic line\\n" * 20000); '
                   'sys.stderr.write("last diagnostic\\n"); raise SystemExit(7)')
        host = MCPHost('http://127.0.0.1:8898', startup_timeout=5)
        with mock.patch.object(host, '_command', return_value=[sys.executable, '-c', program]), host:
            self.assertIn('退出码 7', host.error)
            self.assertIn('last diagnostic', host._last_log_tail)
        paths = list(self.directory.glob('mcp-worker.log*'))
        self.assertLessEqual(len(paths), 2)
        for path in paths:
            self.assertLessEqual(path.stat().st_size, LOG_LIMIT)
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_diagnostic_log_redacts_credentials_and_removes_terminal_escapes(self):
        path = self.directory / 'mcp-worker.log'
        log = _WorkerLog(path)
        try:
            log.write('Authorization: Bearer hide\x1b[31m-this\x1b[0m\n')
            log.write('token=seriald-ag-secret_123 url=https://user:password@localhost/mcp\n')
            log.write('control\x00characters\x7f\n')
        finally:
            tail = log.close()
        contents = path.read_text()
        for secret in ('hide-this', 'hide', 'secret_123', 'user:password', '\x1b', '\x00', '\x7f'):
            self.assertNotIn(secret, contents)
            self.assertNotIn(secret, tail)
        self.assertIn('Bearer <redacted>', contents)
        self.assertIn('https://<redacted>@localhost', contents)

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
class PublicMCPHostTests(IsolatedMCPHostTestCase):
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
