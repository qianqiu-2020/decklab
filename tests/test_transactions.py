"""Console cancellation and replacement use fake descriptors and no boards."""
import io
import json
import os
import threading
import tempfile
import time
import types
import unittest
from unittest import mock

from seriald_core.device import Device
from seriald_core.transactions import TransactionError
from seriald_core.file_transfer import TransferError
from seriald_core import transport, web


class TransactionTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        logs = mock.patch('seriald_core.device.LOG_ROOT', temporary.name)
        logs.start()
        self.addCleanup(logs.stop)
        self.device = Device('fake-usb', '/fake/tty')
        self.addCleanup(self.device.terminal.screen.close)
        self.addCleanup(self.device.close_log)
        self.device.fd, self.device.present, self.device.newline = 98765, True, '\r'

    def test_exclusion_is_local_to_involved_device(self):
        other = Device('other-usb', '/fake/other')
        other.fd, other.present = 98766, True
        with mock.patch.object(self.device, '_write') as blocked, \
                mock.patch.object(other, '_write', return_value=(True, '')) as allowed:
            with self.device.transactions.begin('identify', 5):
                self.assertFalse(self.device.send('x')[0])
                self.assertTrue(other.send('y')[0])
        blocked.assert_not_called()
        allowed.assert_called_once_with('y')
        self.assertIsNone(self.device.transactions.snapshot())

    def test_cancel_wakes_waiter_and_does_not_leak_into_next_operation(self):
        entered = threading.Event()
        codes = []

        def wait():
            try:
                with self.device.transactions.begin('identify', 10, actor='claude'):
                    entered.set()
                    self.device._wait(0, 'never-arrives', 10)
            except TransactionError as error:
                codes.append(error.code)

        worker = threading.Thread(target=wait)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            active = self.device.transactions.snapshot()
            with self.assertRaises(TransactionError) as denied:
                self.device.transactions.cancel(active['id'], actor='other-agent')
            self.assertEqual(denied.exception.status, 403)
            self.device.transactions.cancel(active['id'], actor='claude')
            worker.join(timeout=0.5)
            self.assertFalse(worker.is_alive(), 'cancellation must wake the condition immediately')
        finally:
            self.device.stop.set()
            with self.device.cv:
                self.device.cv.notify_all()
            worker.join(timeout=2)
        self.assertEqual(codes, ['cancelled'])
        self.device.stop.clear()
        with self.device.transactions.begin('next', 1) as lease:
            self.assertFalse(lease.is_set())
        with self.assertRaises(TransactionError) as ended:
            self.device.transactions.cancel(active['id'])
        self.assertEqual(ended.exception.status, 404)

    def test_deadline_bounds_entire_wait_not_each_prompt(self):
        started = time.monotonic()
        with self.assertRaises(TransactionError) as expired:
            with self.device.transactions.begin('identify', 0.03):
                self.device._wait(0, 'never-arrives', 30)
        self.assertEqual(expired.exception.code, 'timeout')
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIsNone(self.device.transactions.snapshot())

    def test_changed_generation_prevents_writer_open(self):
        with mock.patch('seriald_core.device.write_serial') as write:
            with self.assertRaises(TransactionError) as stale:
                with self.device.transactions.begin('input', 5):
                    self.device.connection_id = 'replacement'
                    self.device._write('must not reach replacement')
        self.assertEqual(stale.exception.code, 'stale')
        write.assert_not_called()

    def test_stop_event_replacement_cannot_revive_old_lease(self):
        with self.assertRaises(TransactionError) as stale:
            with self.device.transactions.begin('input', 5) as lease:
                self.device.stop = threading.Event()
                lease.check()
        self.assertEqual(stale.exception.code, 'stale')

    @unittest.skipIf(os.name == 'nt', 'Linux write-only descriptor adapter')
    def test_partial_write_cancellation_stops_remaining_bytes(self):
        sent = bytearray()

        def write(fd, pending):
            sent.extend(bytes(pending[:2]))
            self.device.transactions.current.cancelled.set()
            return 2

        with mock.patch.object(transport.os, 'open', return_value=98766), \
                mock.patch.object(transport.os, 'close') as close, \
                mock.patch.object(transport.os, 'write', side_effect=write):
            ok, error = self.device.send('abcdef')
        self.assertFalse(ok)
        self.assertIn('取消', error)
        self.assertEqual(sent, b'ab')
        close.assert_called_once_with(98766)

    def test_transfer_checks_original_generation_again_before_command(self):
        transfer = self.device.file_transfer
        original = self.device.connection_id
        check = transfer._check_connection

        def replace(connection):
            check(connection)
            self.device.connection_id = 'replacement'

        with mock.patch.object(transfer, '_check_connection', side_effect=replace), \
                mock.patch.object(self.device, '_run_cmd_locked') as command:
            with self.assertRaises(TransferError) as stale:
                transfer.start('upload', '/tmp/file', original, size=5)
        self.assertEqual(stale.exception.status, 409)
        command.assert_not_called()
        self.assertFalse(transfer.active)

    def test_cancelled_provisioning_never_sends_password(self):
        def write(text):
            self.device.transactions.current.cancelled.set()
            return True, ''

        with mock.patch.object(self.device, 'ensure_shell', return_value=(True, 0)), \
                mock.patch.object(self.device, '_say'), \
                mock.patch.object(self.device, '_write', side_effect=write) as writer:
            ok, error = self.device.configure_root_ssh()
        self.assertFalse(ok)
        self.assertIn('取消', error)
        self.assertEqual(writer.call_count, 1)
        self.assertIn('passwd root', writer.call_args.args[0])
        self.assertFalse(self.device.probing)

    def test_cancelled_upload_does_not_start_automatic_acknowledgement_recovery(self):
        transfer = self.device.file_transfer
        entered = threading.Event()
        results = []

        def command(text, timeout, ensure_shell=True):
            if text.startswith('umask'):
                return True, 'SD_OK=1\nSD_GZIP=0\n'
            if text.startswith('rm -f'):
                return True, ''
            entered.set()
            self.device._wait(0, 'never-arrives', timeout)
            return True, ''

        def upload(state):
            try:
                transfer.chunk(state['id'], self.device.connection_id, 0, 'aGVsbG8=')
            except TransferError as error:
                results.append(error.status)

        with mock.patch.object(self.device, '_run_cmd_locked', side_effect=command), \
                mock.patch.object(transfer, '_schedule_expiry'), \
                mock.patch.object(transfer, '_upload_prefix_matches') as recover:
            state = transfer.start('upload', '/tmp/file', self.device.connection_id, actor='claude', size=5)
            worker = threading.Thread(target=upload, args=(state,))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                active = self.device.transactions.snapshot()
                self.assertEqual(active['actor'], 'claude')
                self.device.transactions.cancel(active['id'], actor='claude')
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results, [409])
                self.assertTrue(transfer.state['failed'])
                recover.assert_not_called()
                transfer.abort(state['id'], self.device.connection_id)
            finally:
                self.device.stop.set()
                with self.device.cv:
                    self.device.cv.notify_all()
                worker.join(timeout=2)
        self.assertFalse(transfer.active)

    def test_actor_scope_is_restored_and_does_not_override_explicit_transfer_actor(self):
        with self.device.transactions.actor_scope('claude'):
            with self.device.transactions.begin('identify', 1) as lease:
                self.assertEqual(lease.actor, 'claude')
            with self.device.transactions.begin('transfer', 1, actor='codex') as lease:
                self.assertEqual(lease.actor, 'codex')
        with self.device.transactions.begin('service', 1) as lease:
            self.assertEqual(lease.actor, 'service')

    def test_http_status_and_owner_cancellation_use_same_transaction(self):
        manager = types.SimpleNamespace(resolve=lambda key: self.device, store=mock.Mock())

        def handler(path, payload=None):
            request = web.Handler.__new__(web.Handler)
            raw = json.dumps(payload).encode() if payload is not None else b''
            request.path, request.headers = path, {'Content-Length': str(len(raw))}
            request.rfile, request.wfile = io.BytesIO(raw), io.BytesIO()
            request.send_response, request.send_header, request.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
            return request

        with mock.patch.object(web, 'MGR', manager), self.assertRaises(TransactionError):
            with self.device.transactions.begin('identify', 5, actor='claude') as lease:
                request = handler('/api/operation?key=target')
                request.do_GET()
                self.assertEqual(json.loads(request.wfile.getvalue())['operation']['id'], lease.id)
                request = handler('/api/operation-cancel', {'key': 'target', 'id': lease.id, 'actor': 'other'})
                request.do_POST()
                self.assertEqual(request.send_response.call_args.args[0], 403)
                request = handler('/api/operation-cancel', {'key': 'target', 'id': lease.id, 'actor': 'claude'})
                request.do_POST()
                self.assertEqual(request.send_response.call_args.args[0], 200)


if __name__ == '__main__':
    unittest.main()
