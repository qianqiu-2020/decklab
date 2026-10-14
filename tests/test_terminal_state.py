"""Automatic UART setup must never type into stale, owned or active consoles."""
import unittest
from unittest import mock

from seriald_core.actions import DeviceActions
from seriald_core.device import Device
from seriald_core.terminal_state import SerialTerminalState


class SerialTerminalTests(unittest.TestCase):
    def setUp(self):
        self.device = Device('fixture-port', '/fake/tty', record={
            'serial': 'SN-terminal', 'label': 'board', 'info': {}})
        self.device.fd, self.device.present = 98765, True
        self.actions = DeviceActions(mock.Mock())
        self.writer = mock.patch.object(self.device, '_write', return_value=(True, ''))
        self.write = self.writer.start()
        self.addCleanup(self.writer.stop)
        self.recorder = mock.patch.object(self.device, 'record_input')
        self.recorder.start()
        self.addCleanup(self.recorder.stop)

    def feed(self, data):
        self.device.terminal.observe(data)
        self.device.buf += data

    def resize(self, client='viewer1', **extra):
        payload = {'sid': 'serial', 'cols': 140, 'rows': 42, 'automatic': True,
                   'observed_off': self.device.now_off(), 'client_id': client}
        payload.update(extra)
        return self.actions.execute('session-resize', payload, self.device)

    def test_setup_only_at_live_idle_prompt_and_matching_offset(self):
        self.assertEqual(self.resize().status, 409)
        self.feed(b'root@ubuntu:~# ')
        self.assertEqual(self.resize(observed_off=0).status, 409)
        result = self.resize()
        self.assertEqual(result.status, 200)
        command = self.write.call_args.args[0]
        self.assertIn('stty echoctl -ixon -ixoff rows 42 cols 140', command)
        self.assertIn('infocmp xterm-256color', command)
        self.assertIn('${LC_ALL:-${LC_CTYPE:-$LANG}}', command)
        self.assertTrue(command.endswith('\r'))
        self.assertTrue(self.device.terminal.input_pending)

    def test_shared_geometry_survives_buffer_eviction_and_deduplicates_viewers(self):
        self.feed(b'root@ubuntu:~# ')
        self.resize()
        self.feed(b'\r\n\x1b[8;42;140troot@ubuntu:~# ')
        self.device.buf.clear()
        self.device.base = 1000000
        self.assertEqual(self.resize(client='viewer2', cols=100).payload['geometry'], {'rows': 42, 'cols': 140})
        self.resize()
        self.assertEqual(self.write.call_count, 1)
        self.resize(client='viewer2', cols=100, automatic=False)
        self.assertEqual(self.write.call_count, 2)

    def test_fedora_prompt_and_busy_program_are_distinguished(self):
        self.feed(b'[root@fedora ~]# ')
        self.assertTrue(self.resize().payload['ok'])
        self.feed(b'\r\nInstalling packages...')
        self.assertEqual(self.resize().status, 409)

    def test_replay_geometry_is_selected_at_the_first_byte_not_latest_state(self):
        from seriald_core.queries import SessionQueries
        self.feed(b'root@ubuntu:~# ')
        self.resize()
        old_start = self.device.now_off()
        self.feed(b'echoed setup\r\n\x1b[8;42;140t')
        new_start = self.device.now_off()
        self.feed(b'root@ubuntu:~# \x1b[8;54;200tmore output')
        first = SessionQueries().execute('session', {'off': old_start}, self.device).payload
        second = SessionQueries().execute('session', {'off': new_start}, self.device).payload
        self.assertEqual(first['terminal_initial_geometry'], {'cols': 80, 'rows': 24})
        self.assertEqual(second['terminal_initial_geometry'], {'cols': 140, 'rows': 42})
        self.assertEqual(first['terminal_geometry'], {'cols': 200, 'rows': 54})
        self.device.terminal.prune(new_start)
        self.assertEqual(self.device.terminal.geometry_at(new_start), {'cols': 140, 'rows': 42})

    def test_pending_input_ownership_and_transactions_block_setup(self):
        self.feed(b'root@ubuntu:~# ')
        self.device.terminal.input_pending = True
        self.assertEqual(self.resize().status, 409)
        self.device.terminal.input_pending = False
        with self.device.transactions.actor_scope('web', 'other'):
            self.device.transactions.claim('web')
        self.assertEqual(self.resize().status, 423)
        self.write.assert_not_called()

    def test_manual_profiles_never_setup_and_software_flow_is_preserved(self):
        from types import SimpleNamespace
        profile = SimpleNamespace(probe='manual', serial=SimpleNamespace(flow_control='xonxoff'))
        self.feed(b'root@ubuntu:~# ')
        with mock.patch.object(Device, 'profile', new_callable=mock.PropertyMock, return_value=profile):
            self.assertEqual(self.resize().status, 409)
            self.write.assert_not_called()
            profile.probe = 'linux'
            self.assertTrue(self.resize().payload['ok'])
        self.assertNotIn('-ixon', self.write.call_args.args[0])

    def test_actual_input_writer_becomes_sizing_coordinator(self):
        self.feed(b'root@ubuntu:~# ')
        self.resize()
        self.feed(b'\r\n\x1b[8;42;140troot@ubuntu:~# ')
        with self.device.transactions.actor_scope('web', 'viewer2'):
            self.device.send('uname -a\r')
        self.assertEqual(self.device.terminal.authority, ('web', 'viewer2'))
        self.assertTrue(self.device.terminal.input_pending)
        self.feed(b'\r\nLinux riscv64\r\nroot@ubuntu:~# ')
        result = self.resize(client='viewer2', cols=100)
        self.assertTrue(result.payload['pending'])

    def test_failed_setup_does_not_repeat_on_every_prompt(self):
        self.feed(b'root@ubuntu:~# ')
        self.resize()
        self.feed(b'\r\nstty: error\r\nroot@ubuntu:~# ')
        self.assertTrue(self.resize().payload['skipped'])
        self.write.assert_called_once()

    def test_split_resize_and_boot_reset(self):
        state = SerialTerminalState(self.device)
        state.observe(b'\x1b[8;42;')
        state.observe(b'140t')
        self.assertEqual(state.geometry, {'rows': 42, 'cols': 140})
        state.authority = ('web', 'viewer1')
        state.attempted = True
        state.observe(b'\r\nLinux ver')
        state.observe(b'sion 6.18.0\r\n')
        self.assertIsNone(state.geometry)
        self.assertIsNone(state.authority)
        self.assertFalse(state.attempted)
