"""Control provider fixtures: local HTTP only, no BMC or hardware access."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from seriald_core.board_control import control_actions
from seriald_core.hardware import HardwareBook
from seriald_core.tasks import TaskCancelled, TaskError


@contextmanager
def bmc_fixture():
    state = {'requests': [], 'mapping': 47, 'redirect': False, 'broken': False, 'reject': False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.reply(json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0)))))

        def do_GET(self):
            self.reply(None)

        def reply(self, body):
            state['requests'].append((self.path, body, self.headers.get('Authorization')))
            if state['redirect']:
                self.send_response(302)
                self.send_header('Location', '/redirected')
                self.end_headers()
                return
            if self.path.endswith('/login'):
                result = {'code': 0, 'data': {'access_token': 'private-token'}}
            elif self.path.startswith('/api/v1/devices/name/'):
                result = {'code': 0, 'data': {'name': 'sub23-1', 'module_index': state['mapping'], 'adb_port': 'usb:1-8.3'}}
            else:
                result = {'code': 1 if state['reject'] else 0, 'message': 'private-token private-password'}
                if state['broken']:
                    self.close_connection = True
                    return
            raw = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port), state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class BoardControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.credentials = Path(self.tmp.name) / 'credentials.json'
        self.credentials.write_text(json.dumps({'username': 'tester', 'password': 'private-password'}))
        self.credentials.chmod(0o600)
        self.config = {'provider': 'bmc', 'url': 'http://127.0.0.1:1', 'board': 'sub23-1',
                       'module_index': 47, 'usb_port': '1-8.3', 'credentials_file': str(self.credentials),
                       'actions': ['reset', 'power-on', 'power-off', 'enter-download']}

    def test_explicit_provider_actions_and_agent_policy_without_network(self):
        book = HardwareBook({'boards': {'SN': {'control': self.config}}})
        self.assertEqual(book.available('SN', 'web')['actions'], self.config['actions'])
        self.assertEqual(book.available('SN', 'agent')['actions'], [])
        self.assertEqual(book.available('SN', 'web')['providers']['reset'], 'bmc')
        with self.assertRaises(TaskError) as failure:
            book.command('SN', 'reset', 'agent')
        self.assertEqual(failure.exception.status, 403)
        public = json.dumps(book.available('SN', 'web'))
        for private in ('127.0.0.1', 'credentials', 'sub23'):
            self.assertNotIn(private, public)
        # Admin-supplied command and BMC providers can serve different actions.
        mixed = {'control': dict(self.config, actions=['reset']),
                 'power-on': {'argv': [os.path.abspath(__file__)]}}
        self.assertEqual(HardwareBook({'boards': {'SN': mixed}}).available('SN', 'web')['providers'],
                         {'reset': 'bmc', 'power-on': 'command'})
        mixed['reset'] = mixed['power-on']
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            HardwareBook({'boards': {'SN': mixed}})

    def test_invalid_configuration_is_rejected_before_network(self):
        for change in ({'provider': 'unknown'}, {'url': 'http://user:secret@localhost'},
                       {'url': 'http://localhost/path'}, {'url': 'http://localhost/#x'},
                       {'url': 'http://localhost:invalid'}, {'url': 'http://local\nhost'},
                       {'board': '../another'}, {'module_index': True}, {'usb_port': 'auto'},
                       {'actions': []}, {'actions': ['reset', 'reset']}, {'actions': ['flash']},
                       {'actions': [{}]}, {'credentials_file': 'relative'}, {'password': 'inline'},
                       {'timeout': True}, {'timeout': 0}, {'allow_agent': 1}, {'ca_file': 'relative'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                control_actions(dict(self.config, **change))

    def test_actions_pin_mapping_and_send_exactly_one_mutation(self):
        with bmc_fixture() as (origin, state):
            actions = control_actions(dict(self.config, url=origin))
            for action, suffix, body in [('reset', 'reset', {'reset_type': 'ForceRestart'}),
                    ('power-on', 'reset', {'reset_type': 'PowerOn'}),
                    ('power-off', 'reset', {'reset_type': 'PowerOff'}),
                    ('enter-download', 'firmware-update', {})]:
                state['requests'].clear()
                with mock.patch('seriald_core.hardware.helper_process', side_effect=AssertionError('no UART helper')):
                    result = actions[action].bind(mock.Mock()).run(lambda: None)
                self.assertEqual(result['control_result'], 'acknowledged')
                self.assertEqual(len(state['requests']), 3)
                self.assertEqual(state['requests'][-1], ('/api/v1/openbmc/devices/sub23-1/' + suffix, body, 'Bearer private-token'))
                self.assertNotIn('private', json.dumps(result))

    def test_mapping_change_and_redirect_prevent_mutation(self):
        with bmc_fixture() as (origin, state):
            action = control_actions(dict(self.config, url=origin))['reset']
            state['mapping'] = 48
            with self.assertRaisesRegex(TaskError, 'mapping changed'):
                action.run(lambda: None)
            self.assertEqual(len(state['requests']), 2)
            state['redirect'] = True
            state['requests'].clear()
            with self.assertRaisesRegex(TaskError, 'preflight failed'):
                action.run(lambda: None)
            self.assertEqual(len(state['requests']), 1)

    def test_lost_or_rejected_receipt_is_unknown_and_never_retried(self):
        with bmc_fixture() as (origin, state):
            action = control_actions(dict(self.config, url=origin))['reset']
            for change in ('broken', 'reject'):
                state[change] = True
                state['requests'].clear()
                with self.assertRaisesRegex(TaskError, 'outcome unknown') as failure:
                    action.run(lambda: None)
                self.assertEqual(len(state['requests']), 3)
                self.assertNotIn('private', str(failure.exception))
                state[change] = False

    def test_cancellation_before_and_after_submission(self):
        with bmc_fixture() as (origin, state):
            action = control_actions(dict(self.config, url=origin))['reset']
            def cancel_before():
                raise TaskCancelled('cancelled')
            with self.assertRaises(TaskCancelled):
                action.run(cancel_before)
            self.assertEqual(state['requests'], [])
            def cancel_after():
                if len(state['requests']) == 3:
                    raise TaskCancelled('cancelled')
            with self.assertRaisesRegex(TaskCancelled, 'outcome unknown'):
                action.run(cancel_after)
            self.assertEqual(len(state['requests']), 3)

    def test_credentials_never_leak_in_errors(self):
        action = control_actions(self.config)['reset']
        for content in ('invalid private-password', '{"username":"x","password":"private-password","token":"x"}'):
            self.credentials.write_text(content)
            with self.assertRaisesRegex(TaskError, 'private JSON') as failure:
                action.run(lambda: None)
            self.assertNotIn('private-password', str(failure.exception))
        if os.name == 'posix':
            self.credentials.chmod(0o644)
            with self.assertRaisesRegex(TaskError, 'private JSON'):
                action.run(lambda: None)
