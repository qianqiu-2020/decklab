"""Application service contracts through direct calls and the HTTP adapter."""
import io
import json
import types
import unittest
from unittest import mock

from seriald_core.actions import DeviceActions, capabilities
from seriald_core.device import Device
from seriald_core.transactions import TransactionError
from seriald_core import web


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.device = Device('fake-usb', '/fake/tty', record={
            'serial': 'SN1', 'label': 'board', 'info': {}})
        self.device.fd, self.device.present = 98765, True
        self.manager = types.SimpleNamespace(store=mock.Mock(), resolve=lambda key: self.device)
        self.actions = DeviceActions(self.manager)

    def test_direct_resize_rejects_infinity_before_session_lookup(self):
        with mock.patch.object(self.device, 'get_session') as lookup:
            result = self.actions.execute('session-resize', {'sid': 'ssh1', 'cols': float('inf'),
                                           'rows': 24}, self.device)
        self.assertEqual(result.status, 400)
        lookup.assert_not_called()

    def http(self, action, payload):
        handler = web.Handler.__new__(web.Handler)
        body = json.dumps(dict(payload, key='board', serial='SN1')).encode()
        handler.path = '/api/' + action
        handler.headers = {'Content-Length': str(len(body))}
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.object(web, 'MGR', self.manager):
            handler.do_POST()
        result = json.loads(handler.wfile.getvalue())
        result.pop('key', None)  # The HTTP adapter annotates the selected key.
        return handler.send_response.call_args.args[0], result

    def test_direct_and_http_input_share_actor_scope_and_audit(self):
        actors = []

        def write(text):
            actors.append(self.device.transactions.current.actor)
            return True, ''

        with mock.patch.object(self.device, '_write', side_effect=write), \
                mock.patch.object(self.device, 'record_input') as record:
            payload = {'sid': 'serial', 'data': 'uname\r', 'actor': 'agent-one'}
            direct = self.actions.execute('session-send', payload, self.device, actor='agent-one')
            status, http = self.http('session-send', payload)
        self.assertEqual((direct.status, direct.payload), (status, http))
        self.assertEqual(actors, ['agent-one', 'agent-one'])
        self.assertEqual(record.call_args_list, [mock.call('serial', 'agent-one', 'uname\r')] * 2)
        self.assertIsNone(self.device.transactions.current)

    def test_invalid_input_and_ssh_permissions_have_stable_codes(self):
        cases = [('session-send', {'data': ''}, 400, 'invalid-argument'),
                 ('session-close', {'sid': 'missing'}, 404, 'not-found'),
                 ('unknown', {}, 404, 'not-found')]
        for action, payload, status, code in cases:
            direct = self.actions.execute(action, payload, self.device)
            self.assertEqual(direct.status, status)
            self.assertEqual(direct.payload['code'], code)
            self.assertEqual(self.http(action, payload), (direct.status, direct.payload))
        session = mock.Mock(actor='owner', takeovers=set())
        self.device.ssh_sessions['ssh1'] = session
        result = self.actions.execute('session-send', {'sid': 'ssh1', 'data': 'x'},
                                      self.device, actor='spectator')
        self.assertEqual(result.status, 403)
        self.assertEqual(result.payload['code'], 'forbidden')
        session.write.assert_not_called()

    def test_cancellation_service_enforces_owner_and_audits_success(self):
        with self.assertRaises(TransactionError):
            with self.device.transactions.begin('probe', 2, actor='agent-one') as lease:
                payload = {'id': lease.id}
                denied = self.actions.execute('operation-cancel', payload, self.device, 'agent-two')
                self.assertEqual((denied.status, denied.payload['code']), (403, 'forbidden'))
                self.assertFalse(lease.cancelled.is_set())
                result = self.actions.execute('operation-cancel', payload, self.device, 'agent-one')
                self.assertEqual(result.status, 200)
                self.manager.store.event.assert_called_once_with('SN1', 'agent-one', 'operation-cancel', lease.id)

    def test_direct_service_rejects_invalid_actor_or_payload_before_writing(self):
        with mock.patch.object(self.device, 'send') as send:
            for actor in ('', 'x' * 81, None):
                result = self.actions.execute('send', {'data': 'x'}, self.device, actor)
                self.assertEqual(result.status, 400)
            self.assertEqual(self.actions.execute('send', [], self.device).status, 400)
        send.assert_not_called()

    def test_capabilities_need_no_board_lookup_and_expose_no_credentials(self):
        handler = web.Handler.__new__(web.Handler)
        handler.path = '/api/capabilities'
        handler.wfile = io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.object(web, 'MGR') as manager:
            handler.do_GET()
        manager.resolve.assert_not_called()
        manager.all_devices.assert_not_called()
        self.assertEqual(json.loads(handler.wfile.getvalue()), capabilities().payload)
        self.assertTrue(capabilities().payload['file_transfer']['requires_linux_shell'])


if __name__ == '__main__':
    unittest.main()
