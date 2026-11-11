"""Remote MCP shares the control origin, never its owner credentials or JSON rewrites."""
import base64
import io
import json
import os
from types import SimpleNamespace
import threading
import unittest
from unittest import mock

from seriald_core import control_server as control
from seriald_core.agent_grants import AgentGrantStore


class MCPProxyTests(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(os.environ, {'SERIALD_MCP_ALLOWED_HOSTS': ''})
        environment.start()
        self.addCleanup(environment.stop)
        self.grants = AgentGrantStore(':memory:')
        self.addCleanup(self.grants.close)
        self.token, self.grant = self.grants.issue('device', 'SN-ONLY')
        self.host = SimpleNamespace(
            target=('127.0.0.1', 23456),
            snapshot=lambda: {'mcp_enabled': True, 'mcp_url': '/mcp', 'mcp_error': ''})

    def handler(self, method='POST', path='/mcp', body=b'', headers=None):
        handler = control.ControlHandler.__new__(control.ControlHandler)
        handler.path, handler.command = path, method
        handler.headers = {'Host': 'localhost:8898', 'Authorization': 'Bearer ' + self.token,
                           'Content-Type': 'application/json', 'Content-Length': str(len(body))}
        if headers is not None:
            handler.headers = {'Host': 'localhost:8898', **headers}
        handler.auth_token, handler.grant_store = 'owner-secret', self.grants
        handler.trust_loopback = False
        handler.agent_tokens = {'configured-agent': 'named-agent-secret'}
        handler.client_address = ('127.0.0.1', 43210)
        handler.server = SimpleNamespace(mcp_host=self.host, server_address=('0.0.0.0', 8898))
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        return handler

    def run_handler(self, handler):
        getattr(handler, 'do_' + handler.command)()
        return handler.send_response.call_args.args[0], handler.wfile.getvalue()

    def reply(self, body=b'{"jsonrpc":"2.0","id":1,"result":{}}', status=200, headers=None):
        stream = io.BytesIO(body)
        fields = {'Content-Type': 'application/json', 'Content-Length': str(len(body))}
        fields.update(headers or {})
        return SimpleNamespace(status=status, read=stream.read,
                               getheader=lambda name, default=None: fields.get(name, default))

    def test_only_current_revocable_grants_can_reach_mcp_even_in_open_or_loopback_modes(self):
        cookie_handler = self.handler()
        share = cookie_handler._new_share()
        credentials = ({}, {'Authorization': 'Bearer owner-secret'},
                       {'Authorization': 'Basic ' + base64.b64encode(b'seriald:owner-secret').decode()},
                       {'Authorization': 'Bearer named-agent-secret'},
                       {'Authorization': 'Bearer ' + share}, {'Cookie': 'seriald_share=' + share})
        for headers in credentials:
            for open_access in (False, True):
                with self.subTest(headers=list(headers), open_access=open_access):
                    handler = self.handler(headers=headers)
                    handler.auth_token = '' if open_access else 'owner-secret'
                    handler.trust_loopback = True
                    with mock.patch.object(control.http.client, 'HTTPConnection') as upstream:
                        status, body = self.run_handler(handler)
                    self.assertEqual(status, 401)
                    self.assertNotIn(b'owner-secret', body)
                    upstream.assert_not_called()

    def test_jsonrpc_body_and_grant_are_forwarded_without_api_actor_or_scope_rewriting(self):
        body = b'{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"list_devices"}}'
        handler = self.handler(body=body)
        handler.headers.update({'Accept': 'application/json, text/event-stream',
            'MCP-Protocol-Version': '2025-06-18', 'Mcp-Session-Id': 'session-id',
            'X-Seriald-Actor': 'web', 'Cookie': 'seriald_share=secret', 'X-Forwarded-For': 'remote'})
        connection = mock.Mock()
        connection.getresponse.return_value = self.reply(headers={'MCP-Protocol-Version': '2025-06-18'})
        with mock.patch.object(control.http.client, 'HTTPConnection', return_value=connection) as factory:
            status, result = self.run_handler(handler)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(result)['id'], 1)
        self.assertEqual(factory.call_args.args[:2], self.host.target)
        request = connection.request.call_args
        self.assertEqual(request.args[:2], ('POST', '/mcp'))
        self.assertEqual(request.kwargs['body'], body)
        headers = {key.lower(): value for key, value in request.kwargs['headers'].items()}
        self.assertEqual(headers['authorization'], 'Bearer ' + self.token)
        self.assertEqual(headers['mcp-protocol-version'], '2025-06-18')
        self.assertEqual(headers['mcp-session-id'], 'session-id')
        self.assertNotIn('cookie', headers)
        self.assertNotIn('x-seriald-actor', headers)
        self.assertNotIn('x-forwarded-for', headers)
        connection.close.assert_called_once()

    def test_invalid_or_unbounded_request_never_reaches_the_sdk_process(self):
        cases = [({'Content-Length': '65537'}, b'', 413),
                 ({'Content-Length': '-1'}, b'', 413),
                 ({'Content-Length': 'invalid'}, b'', 400),
                 ({'Content-Length': '4'}, b'{}', 400),
                 ({'Transfer-Encoding': 'chunked'}, b'', 400)]
        for extra, body, expected in cases:
            with self.subTest(extra=extra):
                handler = self.handler(body=body)
                handler.headers.update(extra)
                with mock.patch.object(control.http.client, 'HTTPConnection') as upstream:
                    status, _ = self.run_handler(handler)
                self.assertEqual(status, expected)
                upstream.assert_not_called()
        handler = self.handler(path='/mcp?token=must-not-be-used')
        with mock.patch.object(control.http.client, 'HTTPConnection') as upstream:
            status, _ = self.run_handler(handler)
        self.assertEqual(status, 400)
        upstream.assert_not_called()

    def test_sdk_error_status_and_protocol_headers_reach_the_client(self):
        handler = self.handler(method='GET')
        connection = mock.Mock()
        connection.getresponse.return_value = self.reply(b'{"error":"unsupported"}', status=405,
            headers={'Allow': 'POST', 'Mcp-Session-Id': 'sdk-session',
                     'MCP-Protocol-Version': '2025-06-18'})
        with mock.patch.object(control.http.client, 'HTTPConnection', return_value=connection):
            status, body = self.run_handler(handler)
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(body)['error'], 'unsupported')
        headers = {key.lower(): value for key, value in (call.args for call in handler.send_header.call_args_list)}
        self.assertEqual(headers['allow'], 'POST')
        self.assertEqual(headers['mcp-session-id'], 'sdk-session')
        self.assertEqual(headers['mcp-protocol-version'], '2025-06-18')

    def test_revoked_grant_is_rechecked_for_each_mcp_request(self):
        self.grants.revoke(self.grant['id'])
        handler = self.handler()
        with mock.patch.object(control.http.client, 'HTTPConnection') as upstream:
            status, _ = self.run_handler(handler)
        self.assertEqual(status, 401)
        upstream.assert_not_called()

    def test_browser_origin_must_match_public_host_and_is_not_forwarded_to_loopback_sdk(self):
        for origin in ('null', 'http://[broken', 'https://other.example', 'http://localhost.evil:8898',
                       'http://user@localhost:8898', 'http://localhost:8898/path'):
            with self.subTest(origin=origin):
                handler = self.handler()
                handler.headers.update({'Origin': origin})
                with mock.patch.object(control.http.client, 'HTTPConnection') as upstream:
                    status, _ = self.run_handler(handler)
                self.assertEqual(status, 403)
                upstream.assert_not_called()
        handler = self.handler()
        handler.headers.update({'Origin': 'http://localhost:8898'})
        connection = mock.Mock()
        connection.getresponse.return_value = self.reply()
        with mock.patch.object(control.http.client, 'HTTPConnection', return_value=connection):
            status, _ = self.run_handler(handler)
        self.assertEqual(status, 200)
        forwarded = {key.lower() for key in connection.request.call_args.kwargs['headers']}
        self.assertNotIn('origin', forwarded)

    def test_matching_forged_host_and_origin_are_denied_unless_the_proxy_host_is_explicitly_configured(self):
        handler = self.handler()
        handler.headers.update({'Host': 'attacker.example:8898', 'Origin': 'http://attacker.example:8898'})
        with mock.patch.object(control.http.client, 'HTTPConnection') as upstream:
            status, _ = self.run_handler(handler)
        self.assertEqual(status, 403)
        upstream.assert_not_called()
        handler = self.handler()
        handler.headers.update({'Host': 'decklab.example:443', 'Origin': 'https://decklab.example:443'})
        connection = mock.Mock()
        connection.getresponse.return_value = self.reply()
        with mock.patch.dict(os.environ, {'SERIALD_MCP_ALLOWED_HOSTS': 'decklab.example:443'}), \
                mock.patch.object(control.http.client, 'HTTPConnection', return_value=connection):
            status, _ = self.run_handler(handler)
        self.assertEqual(status, 200)

    def test_missing_malformed_or_wildcard_hosts_are_never_public_mcp_authorities(self):
        for authority in ('', '*', '*.example:8898', 'user@localhost:8898', 'localhost:invalid'):
            with self.subTest(authority=authority):
                handler = self.handler()
                handler.headers['Host'] = authority
                with mock.patch.object(control.http.client, 'HTTPConnection') as upstream:
                    status, _ = self.run_handler(handler)
                self.assertEqual(status, 403)
                upstream.assert_not_called()
        for configured in ('*', '*:8898', '*.example:*'):
            with self.subTest(configured=configured):
                handler = self.handler()
                handler.headers['Host'] = 'attacker.example:8898'
                with mock.patch.dict(os.environ, {'SERIALD_MCP_ALLOWED_HOSTS': configured}), \
                        mock.patch.object(control.http.client, 'HTTPConnection') as upstream:
                    status, _ = self.run_handler(handler)
                self.assertEqual(status, 403)
                upstream.assert_not_called()

    def test_mcp_concurrency_limit_preserves_workers_for_nested_protected_api_calls(self):
        self.host.requests = threading.BoundedSemaphore(1)
        self.assertTrue(self.host.requests.acquire(blocking=False))
        handler = self.handler()
        with mock.patch.object(control.http.client, 'HTTPConnection') as upstream:
            status, _ = self.run_handler(handler)
        self.assertEqual(status, 429)
        upstream.assert_not_called()
        self.host.requests.release()
        handler = self.handler()
        handler.headers['Content-Length'] = 'invalid'
        status, _ = self.run_handler(handler)
        self.assertEqual(status, 400)
        self.assertTrue(self.host.requests.acquire(blocking=False), 'failed request leaked an MCP concurrency slot')
        self.host.requests.release()

    def test_oversized_sdk_response_is_bounded_and_not_partially_forwarded(self):
        handler = self.handler()
        connection = mock.Mock()
        connection.getresponse.return_value = self.reply(b'x' * (8 * 1024 ** 2 + 1))
        with mock.patch.object(control.http.client, 'HTTPConnection', return_value=connection):
            status, body = self.run_handler(handler)
        self.assertEqual(status, 502)
        self.assertLess(len(body), 4096)
        connection.close.assert_called_once()

    def test_unavailable_server_component_is_explicit_and_does_not_fall_back_to_owner_access(self):
        self.host.target = None
        self.host.snapshot = lambda: {'mcp_enabled': False, 'mcp_url': '/mcp',
                                     'mcp_error': '服务端 MCP 组件未安装'}
        handler = self.handler()
        with mock.patch.object(control.http.client, 'HTTPConnection') as upstream:
            status, body = self.run_handler(handler)
        self.assertEqual(status, 503)
        self.assertIn('MCP', body.decode())
        upstream.assert_not_called()
        access = self.handler(method='GET', path='/api/access', headers={'Authorization': 'Bearer owner-secret'})
        with mock.patch.object(control, 'lan_hosts', return_value=[]):
            status, body = self.run_handler(access)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload['mcp_url'], '/mcp')
        self.assertFalse(payload['mcp_enabled'])
        self.assertEqual(payload['mcp_error'], '服务端 MCP 组件未安装')


if __name__ == '__main__':
    unittest.main()
