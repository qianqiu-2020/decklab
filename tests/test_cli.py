"""Agent client regressions: fake HTTP/SSH streams; shell tests use local builtins only."""
import base64
import contextlib
import http.client
import io
import json
import subprocess
import unittest
from unittest import mock

import serialctl as ctl


NONCE = '0123456789abcdef01234567'


def response(data):
    return io.BytesIO(json.dumps(data).encode())


class ClientTests(unittest.TestCase):
    def test_client_preserves_stable_error_code_from_http_and_success_status_failure(self):
        client = ctl.Client('http://127.0.0.1:8898')
        error = ctl.HTTPError(client.url, 409, 'Conflict', {}, response({'code': 'busy', 'error': 'busy'}))
        with mock.patch.object(ctl, 'urlopen', side_effect=error):
            with self.assertRaises(ctl.ClientError) as failure:
                client.request('/api/session-send', {'key': 'board', 'data': 'x'})
        self.assertEqual((failure.exception.status, failure.exception.code), (409, 'busy'))
        with mock.patch.object(ctl, 'urlopen', return_value=response({'ok': False, 'code': 'operation-failed'})):
            with self.assertRaises(ctl.ClientError) as failure:
                client.request('/api/probe', {'key': 'board'})
        self.assertEqual(failure.exception.code, 'operation-failed')

    def test_capabilities_and_operation_commands_use_public_endpoints(self):
        with mock.patch.object(ctl, 'Client') as factory, contextlib.redirect_stdout(io.StringIO()):
            client = factory.return_value
            client.request.return_value = {'api_version': 1}
            self.assertEqual(ctl.main(['capabilities']), 0)
            client.request.assert_called_once_with('/api/capabilities')
            client.select.assert_not_called()
            client.request.reset_mock()
            self.assertEqual(ctl.main(['diagnostics']), 0)
            client.request.assert_called_once_with('/api/diagnostics')
            client.select.assert_not_called()
            client.get.return_value = {'operation': None}
            self.assertEqual(ctl.main(['--device', 'board', 'operation']), 0)
            client.get.assert_called_once_with('/api/operation')
            client.post.return_value = {'ok': True}
            self.assertEqual(ctl.main(['--device', 'board', 'operation-cancel', 'operation-id']), 0)
            client.post.assert_called_once_with('/api/operation-cancel', id='operation-id')
            client.post.reset_mock()
            self.assertEqual(ctl.main(['--device', 'board', 'input-claim']), 0)
            client.post.assert_called_once_with('/api/input-claim')
            client.post.reset_mock()
            self.assertEqual(ctl.main(['--device', 'board', 'input-release', 'claim-id']), 0)
            client.post.assert_called_once_with('/api/input-release', id='claim-id')

    def test_boot_log_latest_resolves_id_then_reads_complete_epoch(self):
        with mock.patch.object(ctl, 'Client') as factory, contextlib.redirect_stdout(io.StringIO()):
            client = factory.return_value
            client.get.side_effect = [{'boots': [{'id': 'boot-stable'}]}, {'data': 'full boot'}]
            self.assertEqual(ctl.main(['--device', 'board', 'boot-log', '--latest']), 0)
        self.assertEqual(client.get.call_args_list,
                         [mock.call('/api/logs'), mock.call('/api/boot-log', id='boot-stable')])

    def test_boot_log_id_does_not_reselect_latest_and_empty_latest_fails(self):
        with mock.patch.object(ctl, 'Client') as factory, contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.get.return_value = {'data': 'complete'}
            self.assertEqual(ctl.main(['--device', 'board', 'boot-log', '--id', 'boot-old']), 0)
            factory.return_value.get.assert_called_once_with('/api/boot-log', id='boot-old')
        with mock.patch.object(ctl, 'Client') as factory, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            factory.return_value.get.return_value = {'boots': []}
            self.assertNotEqual(ctl.main(['--device', 'board', 'boot-log', '--latest']), 0)

    def test_boot_search_passes_literal_query_and_page_offset(self):
        with mock.patch.object(ctl, 'Client') as factory, contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.get.return_value = {'results': []}
            self.assertEqual(ctl.main(['--device', 'board', 'boot-search', '--query', 'panic',
                                       '--offset', '20']), 0)
            factory.return_value.get.assert_called_once_with('/api/boot-search', q='panic', offset=20)

    def test_health_does_not_require_a_device_selector(self):
        out = io.StringIO()
        with mock.patch.object(ctl, 'Client') as factory, contextlib.redirect_stdout(out):
            factory.return_value.request.return_value = {'ok': True, 'role': 'collector', 'pid': 123}
            self.assertEqual(ctl.main(['health']), 0)
        factory.return_value.request.assert_called_once_with('/api/health')
        self.assertEqual(json.loads(out.getvalue())['pid'], 123)

    def test_boot_marker_write_requires_explicit_cli_flag(self):
        for flag in ([], ['--init-boot-marker']):
            with self.subTest(flag=flag), mock.patch.object(ctl, 'Client') as factory, \
                    contextlib.redirect_stdout(io.StringIO()):
                client = factory.return_value
                client.post.return_value = {'ok': True}
                self.assertEqual(ctl.main(['--device', 'unprovisioned-test', 'identify'] + flag), 0)
                if flag:
                    client.post.assert_called_once_with('/api/identify', init_boot_marker=True)
                else:
                    client.post.assert_called_once_with('/api/identify')

    def test_device_selection_rejects_ambiguous_names_but_prefers_exact_key(self):
        client = ctl.Client(ctl.DEFAULT_URL)
        devices = [{'key': 'key1', 'label': 'same', 'hostname': 'host'},
                   {'key': 'key2', 'label': 'same', 'hostname': 'key1'}]
        with mock.patch.object(client, 'request', return_value={'devices': devices}):
            with self.assertRaises(ctl.ClientError):
                client.select('same')
            with self.assertRaises(ctl.ClientError):
                client.select('missing')
            client.select('key1')
            self.assertEqual(client.key, 'key1')

    def test_post_is_json_and_never_retried_after_network_failure(self):
        client = ctl.Client(ctl.DEFAULT_URL)
        client.key = 'board-key'
        with mock.patch.object(ctl, 'urlopen', side_effect=TimeoutError('uncertain')) as open_url:
            with self.assertRaises(ctl.ClientError):
                client.post('/api/session-send', sid='serial', data='uname\r')
        self.assertEqual(open_url.call_count, 1)
        req = open_url.call_args.args[0]
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(json.loads(req.data), {
            'key': 'board-key', 'sid': 'serial', 'data': 'uname\r', 'actor': 'agent'})

    def test_access_token_is_sent_as_bearer_header(self):
        client = ctl.Client(ctl.DEFAULT_URL, token='secret-token')
        with mock.patch.object(ctl, 'urlopen', return_value=response({'ok': True})) as open_url:
            client.request('/api/health')
        self.assertEqual(open_url.call_args.args[0].get_header('Authorization'),
                         'Bearer secret-token')

    def test_read_reports_gap_and_preserves_raw_bytes(self):
        client = ctl.Client(ctl.DEFAULT_URL)
        client.key = 'usb:path'
        payload = {'key': client.key, 'sid': 'serial', 'off': 102, 'alive': True,
                   'data_b64': base64.b64encode(b'\xff\x00').decode()}
        with mock.patch.object(ctl, 'urlopen', return_value=response(payload)) as open_url:
            result, data = client.read('serial', 50)
        self.assertTrue(result['gap'])
        self.assertEqual(data, b'\xff\x00')
        self.assertIn('key=usb%3Apath', open_url.call_args.args[0].full_url)

    def test_read_rejects_cross_session_and_regressed_offsets(self):
        client = ctl.Client(ctl.DEFAULT_URL)
        client.key = 'board'
        good = {'key': 'board', 'sid': 'ssh1', 'off': 10, 'alive': True, 'data_b64': ''}
        for changes in ({'key': 'other'}, {'sid': 'ssh2'}, {'off': 1},
                        {'data_b64': '!'}, {'off': '10'}):
            with self.subTest(changes=changes), \
                    mock.patch.object(client, 'request', return_value=dict(good, **changes)):
                with self.assertRaises(ctl.ClientError):
                    client.read('ssh1', 5)

    def test_service_level_failure_is_not_successful_http(self):
        with mock.patch.object(ctl, 'urlopen', return_value=response({'ok': False, 'error': 'busy'})):
            with self.assertRaisesRegex(ctl.ClientError, 'busy'):
                ctl.Client(ctl.DEFAULT_URL).post('/api/probe')

    def test_incomplete_http_body_becomes_client_error(self):
        stream = mock.MagicMock()
        stream.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b'partial', 10)
        with mock.patch.object(ctl, 'urlopen', return_value=stream):
            with self.assertRaises(ctl.ClientError):
                ctl.Client(ctl.DEFAULT_URL).request('/api/devices')

    def test_cli_exec_end_to_end_over_fake_http(self):
        calls = []
        reads = 0
        offset = 0

        def serve(request, **kwargs):
            nonlocal reads, offset
            route = ctl.urlsplit(request.full_url).path
            payload = json.loads(request.data) if request.data else None
            calls.append((route, payload))
            if route == '/api/devices':
                return response({'devices': [{'key': 'board-key', 'label': 'board'}]})
            if route == '/api/ssh-connect':
                self.assertEqual(payload['key'], 'board-key')
                return response({'ok': True, 'sid': 'ssh3'})
            if route == '/api/session':
                reads += 1
                data = b'root@board:# ' if reads == 1 else (
                    '__SD_AGENT_B_' + NONCE + '__\nhello\n__SD_AGENT_E_' + NONCE + '__:0\n').encode()
                offset += len(data)
                return response({'key': 'board-key', 'sid': 'ssh3', 'off': offset,
                                 'alive': True, 'data_b64': base64.b64encode(data).decode()})
            self.assertIn(route, ('/api/session-send', '/api/session-result', '/api/session-close'))
            self.assertEqual(payload['key'], 'board-key')
            self.assertEqual(payload['sid'], 'ssh3')
            return response({'ok': True})

        out = io.StringIO()
        with mock.patch.object(ctl, 'urlopen', side_effect=serve), \
                mock.patch.object(ctl.secrets, 'token_hex', return_value=NONCE), \
                contextlib.redirect_stdout(out):
            status = ctl.main(['--device', 'board', 'exec', '--command', 'printf hello'])
        result = json.loads(out.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(result['output'], 'hello')
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(result['session_closed'])
        self.assertEqual([route for route, _ in calls], [
            '/api/devices', '/api/ssh-connect', '/api/session', '/api/session-send',
            '/api/session', '/api/session-result', '/api/session-close'])

    def test_large_send_is_rejected_before_network(self):
        with mock.patch.object(ctl, 'urlopen') as open_url:
            with self.assertRaises(ctl.ClientError):
                ctl.Client(ctl.DEFAULT_URL).post('/api/session-send', data='中' * 20000)
        open_url.assert_not_called()

    def test_cli_read_tail_has_exact_offsets_and_explicit_truncation(self):
        client = mock.Mock()
        client.read.return_value = ({'off': 12, 'gap': False}, b'hello world!')
        out = io.StringIO()
        with mock.patch.object(ctl, 'Client', return_value=client), contextlib.redirect_stdout(out):
            status = ctl.main(['--device', 'board', 'read', '--tail', '6'])
        result = json.loads(out.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(result['data'], 'world!')
        self.assertEqual(result['truncated_bytes'], 6)
        self.assertEqual(result['start_off'], 6)
        self.assertEqual(result['off'], 12)


class FramingTests(unittest.TestCase):
    def test_child_shell_status_quotes_multiline_stderr_and_exit(self):
        for command, expected, code in (
                ("printf '%s' \"a'b\"", "a'b", 0),
                ("printf 'first\\n'\nprintf second", 'first\nsecond', 0),
                ("printf err >&2; exit 7", 'err', 7),
                ("set -e; false; printf unreachable", '', 1),
                ("printf '末尾\\n'", '末尾\n', 0)):
            with self.subTest(command=command):
                framed = ctl.frame_command(command, NONCE).rstrip('\r')
                self.assertNotIn('__SD_AGENT_B_' + NONCE, framed)
                self.assertNotIn('__SD_AGENT_E_' + NONCE, framed)
                proc = subprocess.run(['/bin/sh'], input=framed.encode(), stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, timeout=3, env={'PATH': '/usr/bin:/bin'})
                # Echo, old frames, and unrelated startup output cannot become the result.
                stream = framed.encode() + b'\r\nold output\r\n' + proc.stdout
                actual, output = ctl.parse_frame(stream, NONCE)
                self.assertEqual((actual, output), (code, expected))

    def test_partial_end_marker_and_wrong_nonce_do_not_complete(self):
        begin = ('__SD_AGENT_B_' + NONCE + '__\nhello').encode()
        for end in (b'', b'\n__SD_AGENT_E_wrong__:0\n',
                    ('\n__SD_AGENT_E_' + NONCE + '__:0').encode()):
            self.assertIsNone(ctl.parse_frame(begin + end, NONCE)[0])

    def test_command_validation_precedes_session_creation(self):
        client = mock.Mock(key='board')
        for command in ('', 'a' * 2049, 'echo\x03bad', 'echo\rbad'):
            with self.assertRaises(ctl.ClientError):
                ctl.execute(client, command, 1)
        client.post.assert_not_called()


class ExecutionTests(unittest.TestCase):
    def client(self, body=b'output', code=0):
        client = mock.Mock(key='board-key', timeout=90)
        client.post.side_effect = lambda route, **kwargs: (
            {'ok': True, 'sid': 'ssh7'} if route == '/api/ssh-connect' else {'ok': True})
        self.prompt = ({'off': 10, 'alive': True, 'gap': False}, b'root@test:# ')
        framed = ('\n__SD_AGENT_B_' + NONCE + '__\n').encode() + body
        framed += ('\n__SD_AGENT_E_' + NONCE + '__:%d\n' % code).encode()
        client.read.side_effect = [self.prompt,
                                   ({'off': 100, 'alive': True, 'gap': False}, framed)]
        return client

    def run_command(self, client):
        with mock.patch.object(ctl.secrets, 'token_hex', return_value=NONCE):
            return ctl.execute(client, 'printf output', 1)

    def test_new_session_is_closed_and_nonzero_exit_is_reported(self):
        for code in (0, 7):
            client = self.client(code=code)
            result = self.run_command(client)
            self.assertEqual(result['exit_code'], code)
            self.assertEqual(result['ok'], code == 0)
            self.assertTrue(result['completed'])
            self.assertTrue(result['session_closed'])
            self.assertFalse(result['may_still_be_running'])
            self.assertEqual(result['output'], 'output')
            self.assertEqual(client.post.call_args_list, [mock.call('/api/ssh-connect', title='printf output'),
                             mock.call('/api/session-result', sid='ssh7', completed=True, exit_code=code),
                             mock.call('/api/session-close', sid='ssh7')])
            self.assertEqual(client.request.call_args.kwargs['payload']['sid'], 'ssh7')

    def test_timeout_retains_partial_output_and_closes_session(self):
        client = self.client()
        client.read.side_effect = [self.prompt, ({'off': 30, 'alive': True, 'gap': False},
            ('__SD_AGENT_B_' + NONCE + '__\npartial').encode()), ctl.ClientError('timeout')]
        result = self.run_command(client)
        self.assertFalse(result['completed'])
        self.assertIsNone(result['exit_code'])
        self.assertEqual(result['output'], 'partial')
        self.assertTrue(result['session_closed'])
        self.assertTrue(result['may_still_be_running'])

    def test_send_network_failure_never_claims_command_was_not_sent(self):
        client = self.client()
        client.request.side_effect = ctl.ClientError('connection lost')
        result = self.run_command(client)
        self.assertTrue(result['sent'])
        self.assertTrue(result['may_still_be_running'])
        self.assertEqual(client.request.call_count, 1)
        self.assertTrue(result['session_closed'])

    def test_output_gap_fails_instead_of_reporting_partial_success(self):
        client = self.client()
        client.read.side_effect = [self.prompt, ({'off': 1000, 'alive': True, 'gap': True}, b'gap')]
        result = self.run_command(client)
        self.assertFalse(result['ok'])
        self.assertIn('lost', result['error'])
        self.assertTrue(result['session_closed'])

    def test_login_failure_does_not_send_command(self):
        client = self.client()
        client.read.side_effect = [({'off': 20, 'alive': False, 'gap': False}, b'Permission denied')]
        result = self.run_command(client)
        self.assertFalse(result['sent'])
        self.assertFalse(result['may_still_be_running'])
        self.assertIn('Permission denied', result['output'])
        client.request.assert_not_called()
        self.assertTrue(result['session_closed'])

    def test_cleanup_failure_is_reported_even_after_command_completed(self):
        client = self.client()
        client.post.side_effect = [{'sid': 'ssh7'}, {'ok': True}, ctl.ClientError('close failed')]
        result = self.run_command(client)
        self.assertTrue(result['completed'])
        self.assertFalse(result['ok'])
        self.assertFalse(result['session_closed'])
        self.assertEqual(result['sid'], 'ssh7')
        self.assertIn('close failed', result['cleanup_error'])


if __name__ == '__main__':
    unittest.main()
