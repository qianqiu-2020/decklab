"""Real terminal parser, fake transports: no boards, SSH or USB access."""
import base64
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from seriald_core.terminal_backend import ENGINE, SharedTerminal, node_binary, terminal_payload
from seriald_core.sessions_windows import SshSession
from seriald_core.config import SCROLLBACK
from seriald_core.device import Device


@unittest.skipUnless(node_binary(), 'requires the packaged Node terminal runtime')
class SharedTerminalTests(unittest.TestCase):
    def terminal(self, kind='ssh', cols=120, rows=32):
        terminal = SharedTerminal(kind, cols, rows)
        self.addCleanup(terminal.close)
        return terminal

    def test_live_queries_have_one_responder_without_any_browser(self):
        terminal = self.terminal()
        replies = []
        data = b'hello\r\n\x1b[6n\x1b[32766;32766H\x1b[6n'
        for off, char in enumerate(data, 1):
            replies.extend(terminal.feed(bytes([char]), off))
        self.assertEqual(replies, ['\x1b[2;1R', '\x1b[32;120R'])
        terminal.feed(b'\x1b[2;1Hcontinued', len(data) + 18)
        self.assertEqual(terminal.metadata()['cursor']['y'], 1)

    def test_restoring_history_never_generates_protocol_input(self):
        terminal = self.terminal()
        self.assertEqual(terminal.feed(b'\x1b[6n\x1b[c\x1b]11;?\x07', 20, live=False), [])

    def test_vim_capability_queries_are_answered_without_waiting_for_a_browser(self):
        terminal = self.terminal()
        replies = terminal.feed(b'\x1b[18t\x1bP+q436f\x1b\\\x1bP+q6b75\x1b\\\x1b]11;?\x07\x1b[?12$p', 60)
        self.assertEqual(replies[:3], ['\x1b[8;32;120t', '\x1bP1+r436f=323536\x1b\\', '\x1bP0+r\x1b\\'])
        self.assertTrue(replies[3].startswith('\x1b]11;rgb:'))
        self.assertRegex(replies[4], r'^\x1b\[\?12;[12]\$y$')

    def test_snapshot_preserves_vim_screen_modes_and_fragmented_control_bytes(self):
        prefix = b'shell\r\n\x1b[?1049h\x1b[?1h\x1b[?2004h\x1b[4;8Hvim\x1b]3008;start=context'
        suffix = b'\x1b\\\x1b[6n'
        original, observer = self.terminal(), self.terminal()
        original.feed(prefix, len(prefix))
        snapshot = original.snapshot()
        self.assertEqual(snapshot['cursor']['buffer'], 'alternate')
        self.assertTrue(snapshot['modes']['applicationCursorKeysMode'])
        self.assertTrue(snapshot['modes']['bracketedPasteMode'])
        restored = base64.b64decode(snapshot['data']) + base64.b64decode(snapshot['pending'])
        self.assertEqual(observer.feed(restored, len(prefix), live=False), [])
        self.assertEqual(original.feed(suffix, len(prefix) + len(suffix)),
                         observer.feed(suffix, len(prefix) + len(suffix)))
        self.assertEqual(original.metadata()['cursor'], observer.metadata()['cursor'])
        self.assertEqual(original.metadata()['modes'], observer.metadata()['modes'])

    def test_utf8_pending_prefix_is_restored_without_replacement(self):
        original, observer = self.terminal(), self.terminal()
        text = '汉'.encode('utf8')
        original.feed(b'begin ' + text[:1], 7)
        snapshot = original.snapshot()
        self.assertEqual(base64.b64decode(snapshot['pending']), text[:1])
        observer.feed(base64.b64decode(snapshot['data']) + text[:1], 7, live=False)
        self.assertEqual(original.feed(text[1:] + b'\x1b[6n', 13), observer.feed(text[1:] + b'\x1b[6n', 13))

    def test_snapshot_keeps_scrolling_margins_saved_cursor_attributes_and_pending_wrap(self):
        for prefix, suffix in [
                (b'first\r\n\x1b[2;20r\x1b[5;8H\x1b[31m\x1b7\x1b[30;1H\x1b[32mprogress',
                 b'\x1b8restored\r\n\x1b[6n'),
                (b'\x1b[?1049h\x1b[?1000h\x1b[?1006h\x1b[3;18r\x1b[?6h\x1b[5;8Hvim',
                 b'\r\nmore\x1b[6n\x1b[?1049l\x1b[6n'),
                (b'X' * 120, b'next\x1b[6n')]:
            with self.subTest(prefix=prefix):
                original, observer = self.terminal(), self.terminal()
                original.feed(prefix, len(prefix))
                snapshot = original.snapshot()
                observer.feed(base64.b64decode(snapshot['data']), len(prefix), live=False)
                self.assertEqual(original.feed(suffix, len(prefix) + len(suffix)),
                                 observer.feed(suffix, len(prefix) + len(suffix)))
                self.assertEqual(original.snapshot()['data'], observer.snapshot()['data'])

    def test_evicted_or_partial_cursors_recover_the_screen_and_closed_sessions_keep_it(self):
        terminal = self.terminal()
        terminal.feed(b'\x1b[?1049hvim', 11)
        revision = terminal.metadata()['revision']
        terminal.feed(b'output\r\n' * (SCROLLBACK // 8), 11 + SCROLLBACK)
        recovered = terminal.presentation(5, revision)
        self.assertIn('terminal_snapshot', recovered)
        self.assertEqual(recovered['terminal_snapshot']['cursor']['buffer'], 'alternate')
        terminal.close()
        self.assertEqual(terminal.presentation(0, revision)['terminal_snapshot']['data'], recovered['terminal_snapshot']['data'])

    def test_serial_presentation_corrections_are_shared_but_raw_bytes_remain_distinct(self):
        terminal = self.terminal('serial')
        prefix = b'root@board:~# \x1b[8;52;140t'
        terminal.feed(prefix, len(prefix))
        revision = terminal.metadata()['revision']
        raw = b'\x1b7\x1b[0;51r\x1b8\x1b[1Aprogress'
        terminal.feed(raw, len(prefix) + len(raw))
        presentation = terminal.presentation(len(prefix), revision)
        # A geometry correction changed revision, so viewers get a snapshot.
        snapshot = presentation.get('terminal_snapshot')
        if snapshot:
            revision = snapshot['revision']
        else:
            self.assertIn(b'\x1b[>52z', base64.b64decode(presentation['data_b64']))
        terminal.feed(b'continued', len(prefix) + len(raw) + 9)
        self.assertEqual(base64.b64decode(terminal.presentation(len(prefix) + len(raw), revision)['data_b64']), b'continued')

    def test_firmware_clear_can_leave_edk2_grid_and_kernel_resets_the_background(self):
        terminal = self.terminal('serial', 80, 24)
        terminal.feed(b'\x1b[47m\x1b[2J\x1b[50;27HEDK2', 28)
        self.assertEqual(terminal.metadata()['geometry'], {'cols': 80, 'rows': 50})
        terminal.feed(b'\x1b[2J\x1b[1;1HGRUB', 44)
        self.assertEqual(terminal.metadata()['geometry'], {'cols': 80, 'rows': 24})
        terminal.feed(b'\r\n[    0.000000] Linux version 6.18.0\r\n', 88)
        state = terminal.snapshot()
        self.assertEqual(state['phase'], 'text')
        self.assertNotIn(b'EDK2', base64.b64decode(state['data']))
        self.assertNotIn(b'\x1b[47m', base64.b64decode(state['data']))

    def test_clean_handoff_restores_dimensions_parser_prefix_and_uses_a_new_revision(self):
        original, resumed = self.terminal('serial'), self.terminal('serial')
        prefix = b'\x1b[8;52;140troot@board:~# \x1b]3008;command=' + '汉'.encode()[:1]
        original.feed(prefix, len(prefix))
        snapshot = original.snapshot()
        self.assertTrue(resumed.restore(snapshot))
        self.assertEqual(resumed.metadata()['geometry'], snapshot['geometry'])
        self.assertNotEqual(resumed.metadata()['revision'], snapshot['revision'])
        suffix = '汉'.encode()[1:] + b'\x1b\\\x1b[6n'
        self.assertEqual(original.feed(suffix, len(prefix) + len(suffix)), resumed.feed(suffix, len(prefix) + len(suffix)))
        self.assertFalse(self.terminal().restore({'geometry': {'cols': 'bad'}}))

    def test_shared_ssh_controller_guards_size_and_can_be_taken_over(self):
        terminal = self.terminal()
        self.assertTrue(terminal.control('web', 'page-a'))
        self.assertFalse(terminal.control('web', 'page-b'))
        self.assertTrue(terminal.control('web', 'page-b', takeover=True))
        self.assertFalse(terminal.control('web', 'page-a'))

    def test_takeover_is_serialized_with_pending_writes_and_resizes(self):
        with tempfile.TemporaryDirectory() as root:
            session = SshSession('fixture', '192.0.2.1', 'board', root)
            session.fd = object()
            self.addCleanup(session.terminal.close)
            self.addCleanup(session._close_log)
            session.terminal.control('web', 'page-a')
            entered, release, taken = threading.Event(), threading.Event(), threading.Event()
            def send(*_):
                entered.set()
                self.assertTrue(release.wait(2))
            with mock.patch.object(session, '_send', side_effect=send) as writer, mock.patch.object(session, '_resize') as resize:
                thread = threading.Thread(target=session.write, args=('before', 'web', 'page-a'))
                thread.start()
                self.assertTrue(entered.wait(2))
                takeover = threading.Thread(target=lambda: (session.takeover('web', 'page-b'), taken.set()))
                takeover.start()
                self.assertFalse(taken.wait(.05))
                release.set()
                thread.join(2); takeover.join(2)
                self.assertFalse(session.write('late', 'web', 'page-a'))
                self.assertFalse(session.resize(80, 24, 'web', 'page-a'))
                self.assertEqual(writer.call_count, 1)
                resize.assert_not_called()

    def test_failed_transport_resize_keeps_the_previous_canonical_grid(self):
        with tempfile.TemporaryDirectory() as root:
            session = SshSession('fixture', '192.0.2.1', 'board', root)
            session.fd = object()
            self.addCleanup(session.terminal.close)
            self.addCleanup(session._close_log)
            session.terminal.feed(b'prompt', 6)
            with mock.patch.object(session, '_resize', side_effect=OSError('channel closed')):
                self.assertFalse(session.resize(140, 50))
            self.assertEqual((session.cols, session.rows), (120, 32))
            self.assertEqual(session.terminal.metadata()['geometry'], {'cols': 120, 'rows': 32})

    @unittest.skipIf(sys.platform == 'win32', 'Linux sshpass wrapper signaling')
    def test_linux_resize_explicitly_notifies_the_sshpass_wrapper(self):
        import signal
        from seriald_core.sessions import SshSession as LinuxSession
        with tempfile.TemporaryDirectory() as root:
            session = LinuxSession('fixture', '192.0.2.1', 'board', root)
            self.addCleanup(session._close_log)
            session.proc = mock.Mock()
            session.proc.poll.return_value = None
            with mock.patch.object(session, '_set_winsize') as resize:
                session._resize(98765)
                resize.assert_called_once_with(98765)
                session.proc.send_signal.assert_called_once_with(signal.SIGWINCH)

    def test_parser_failure_preserves_capture_and_archive_output(self):
        with tempfile.TemporaryDirectory() as root:
            session = SshSession('fixture', '192.0.2.1', 'board', root)
            self.addCleanup(session._close_log)
            with mock.patch.object(ENGINE, 'call', side_effect=RuntimeError('worker unavailable')):
                session._append(b'captured even when parser fails\r\n')
            self.assertIn(b'captured even when parser fails', session.read(0)[1])
            self.assertIn('worker unavailable', session.terminal.metadata()['error'])
            self.assertFalse(session.terminal.metadata()['managed'])
            self.assertIn('captured even when parser fails', Path(session.logpath).read_text())

    def test_uart_protocol_replies_are_connection_pinned_and_not_user_commands(self):
        device = Device('fake', '/fake/tty')
        device.fd, device.present = (mock.Mock() if sys.platform == 'win32' else 98765), True
        if sys.platform == 'win32':
            device.fd.write.side_effect = len
        self.addCleanup(device.terminal.screen.close)
        with mock.patch('seriald_core.device.write_serial', return_value=(True, '')) as write, \
                mock.patch.object(device, 'record_input') as record:
            self.assertFalse(device._protocol_reply('\x1b[2;1R', 'old-connection'))
            write.assert_not_called()
            self.assertTrue(device._protocol_reply('\x1b[2;1R', device.connection_id))
            self.assertFalse(device.terminal.input_pending)
            record.assert_not_called()

    def test_both_transport_adapters_use_the_same_response_and_snapshot_contract(self):
        adapters = [SshSession]
        try:
            from seriald_core.sessions import SshSession as LinuxSession
            adapters.append(LinuxSession)
        except ImportError:
            pass
        for adapter in adapters:
            with self.subTest(adapter=adapter), tempfile.TemporaryDirectory() as root:
                session = adapter('fixture', '192.0.2.1', 'board', root)
                session.fd = object()
                with mock.patch.object(session, '_send') as send, \
                        mock.patch.object(session, '_dispose_transport'), \
                        mock.patch.object(session, '_interrupt_transport'):
                    session._append(b'\x1b[?1049h\x1b[4;8Hvim\x1b[6n')
                    send.assert_called_once_with(session.fd, b'\x1b[4;11R')
                    raw_off, data, alive = session.read(0)
                    raw = dict(off=raw_off, data_b64=base64.b64encode(data).decode())
                    screen = terminal_payload(session, dict(raw), 0, screen=True)
                    self.assertTrue(alive)
                    self.assertEqual(raw['off'], screen['off'])
                    self.assertEqual(screen['terminal_snapshot']['cursor']['buffer'], 'alternate')
                    self.assertEqual(raw['data_b64'], base64.b64encode(data).decode())
                    session.close()
