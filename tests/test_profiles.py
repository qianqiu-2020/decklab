"""Configuration contracts; no board, tty, real SSH or privileged operation."""
import codecs
import errno
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from seriald_core.profiles import BoardProfile, ProfileBook, SerialSettings
from seriald_core.device import Device
from seriald_core import transport


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_default_retains_existing_development_workflow(self):
        profile = ProfileBook().resolve()
        self.assertEqual(profile.serial, SerialSettings())
        self.assertEqual((profile.console_user, profile.ssh_user), ('root', 'root'))
        self.assertTrue(profile.auto_configure_ssh)
        self.assertIsNone(profile.line_ending)

    def test_environment_settings_and_credentials(self):
        with mock.patch.dict(os.environ, {'SERIALD_BAUD': '9600', 'SERIALD_DATA_BITS': '7',
                                         'SERIALD_PARITY': 'E', 'SERIALD_STOP_BITS': '2',
                                         'SERIALD_FLOW_CONTROL': 'xonxoff',
                                         'SERIALD_CONSOLE_USER': 'ubuntu',
                                         'SERIALD_CONSOLE_PASSWORD': 'console-secret',
                                         'SERIALD_SSH_PASSWORD': 'ssh-secret',
                                         'SERIALD_AUTO_CONFIGURE_SSH': '0',
                                         'SERIALD_NEWLINE': 'crlf'}):
            profile = ProfileBook().resolve()
        self.assertEqual(profile.serial, SerialSettings(9600, 7, 'E', 2, 'xonxoff'))
        self.assertEqual(profile.line_ending, '\r\n')
        self.assertEqual(profile.console_user, 'ubuntu')
        self.assertEqual(profile.console_password, 'console-secret')
        self.assertEqual(profile.ssh_password, 'ssh-secret')
        self.assertFalse(profile.auto_configure_ssh)
        self.assertNotIn('secret', repr(profile))

    def test_serial_assignment_precedes_port_and_default(self):
        book = ProfileBook({'default': 'linux',
                            'profiles': {'linux': {}, 'slow': {'serial': {'baud': 9600}},
                                         'raw': {'probe': 'manual'}},
                            'ports': {'usb-port': 'slow'}, 'boards': {'BOARD-1': 'raw'}})
        self.assertEqual(book.resolve().name, 'linux')
        self.assertEqual(book.resolve(port='usb-port').serial.baud, 9600)
        selected = book.resolve('BOARD-1', 'usb-port')
        self.assertEqual(selected.name, 'raw')
        self.assertFalse(selected.auto_configure_ssh)

    def test_invalid_profiles_fail_before_any_io(self):
        documents = [{'unexpected': {}}, {'default': 'missing'}, {'profiles': []},
                     {'profiles': {'typo': {'ssh_pasword': 'hidden'}}},
                     {'ports': {'usb': 'missing'}}, {'boards': {'': 'default'}},
                     {'profiles': {'x': {'serial': {'baud': True}}}},
                     {'profiles': {'x': {'serial': {'flow_control': 'typo'}}}},
                     {'profiles': {'x': {'probe': 'manual', 'auto_configure_ssh': True}}},
                     {'profiles': {'x': {'ssh_user': '-oProxyCommand=x'}}},
                     {'profiles': {'x': {'console_password': 'secret\ncommand'}}}]
        for document in documents:
            with self.subTest(document=document), self.assertRaises(ValueError):
                ProfileBook(document)

    def test_file_accepts_windows_utf8_bom_and_rejects_duplicates_and_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'profiles.json'
            with mock.patch.dict(os.environ, {'SERIALD_PROFILES': str(path)}):
                path.write_bytes(codecs.BOM_UTF8 + json.dumps({'profiles': {'raw': {'probe': 'manual'}},
                                                              'default': 'raw'}).encode())
                self.assertEqual(ProfileBook.from_env().resolve().probe, 'manual')
                path.write_text('{"default":"default","default":"other"}')
                with self.assertRaisesRegex(ValueError, 'duplicate'):
                    ProfileBook.from_env()
                path.write_bytes(b' ' * 65537)
                with self.assertRaisesRegex(ValueError, '64 KiB'):
                    ProfileBook.from_env()

    def test_manual_profile_never_probes_or_auto_configures_but_allows_raw_input(self):
        book = ProfileBook({'profiles': {'raw': {'probe': 'manual'}}, 'default': 'raw'})
        device = Device('fake', '/fake/tty', manager=types.SimpleNamespace(profiles=book))
        device.auto_identify = True
        device.auto_identify_until = float('inf')
        with mock.patch.object(device, '_run_cmd') as run, mock.patch.object(device, '_write') as write, \
                mock.patch('seriald_core.device.threading.Thread') as thread:
            self.assertFalse(device.run_probe()['ok'])
            self.assertFalse(device.configure_root_ssh()[0])
            self.assertFalse(device.ensure_shell(0)[0])
            device._maybe_auto_identify('root@board:~# ')
            run.assert_not_called()
            write.assert_not_called()
            thread.assert_not_called()
        device.fd = 98765
        with mock.patch.object(device, '_write', return_value=(True, '')) as write:
            self.assertTrue(device.send('help\r')[0])
            write.assert_called_once_with('help\r')

    def test_snapshot_never_discloses_credentials_and_reports_pending_framing(self):
        book = ProfileBook({'profiles': {'raw': {'probe': 'manual', 'ssh_password': 'do-not-publish',
                                               'serial': {'baud': 9600}}}, 'default': 'raw'})
        device = Device('fake', '/fake', types.SimpleNamespace(profiles=book))
        device.serial_settings = SerialSettings()
        snapshot = device.snapshot()
        self.assertEqual(snapshot['profile'], 'raw')
        self.assertTrue(snapshot['settings_pending'])
        self.assertEqual(snapshot['serial_settings']['baud'], 115200)
        self.assertNotIn('do-not-publish', json.dumps(snapshot))

    @unittest.skipIf(transport.termios is None, 'Linux framing adapter')
    def test_linux_framing_is_applied_only_on_reader_open(self):
        t = transport.termios
        attrs = [0, 0, 0, 0, 0, 0, [0] * 32]
        with mock.patch.object(transport.os, 'open', return_value=98765), \
                mock.patch.object(transport.os, 'fstat'), mock.patch.object(transport.os, 'close'), \
                mock.patch.object(t, 'tcgetattr', return_value=attrs), mock.patch.object(t, 'tcsetattr') as configure:
            self.assertEqual(transport.open_serial('/fake', settings=SerialSettings(9600, 7, 'O', 2, 'xonxoff')), 98765)
        configured = configure.call_args.args[2]
        self.assertEqual(configured[4:6], [t.B9600, t.B9600])
        self.assertTrue(configured[2] & t.PARENB)
        self.assertTrue(configured[2] & t.PARODD)
        self.assertTrue(configured[2] & t.CSTOPB)
        self.assertTrue(configured[0] & t.IXON)
        self.assertTrue(configured[0] & t.IXOFF)

    @unittest.skipIf(transport.termios is None, 'Linux framing adapter')
    def test_unsupported_linux_speed_rejected_before_open(self):
        with mock.patch.object(transport.os, 'open') as opening, self.assertRaises(OSError) as error:
            transport.open_serial('/fake', settings=SerialSettings(12345))
        opening.assert_not_called()
        self.assertEqual(error.exception.errno, errno.EINVAL)

    def test_windows_options_have_matching_framing(self):
        options = SerialSettings(57600, 7, 'E', 2, 'rtscts').pyserial_options()
        self.assertEqual(options, {'baudrate': 57600, 'bytesize': 7, 'parity': 'E', 'stopbits': 2,
                                  'rtscts': True, 'xonxoff': False, 'dsrdtr': False})

    def test_windows_collector_passes_profile_to_its_only_reader(self):
        from seriald_core import windows_manager
        book = ProfileBook({'default': 'raw', 'profiles': {
            'raw': {'probe': 'manual', 'newline': 'crlf', 'serial': {'baud': 57600}}}})
        manager = windows_manager.WindowsManager(profiles=book)
        device = Device('fake-usb', 'COM999', manager)
        reader = mock.Mock(in_waiting=1)

        def read(count):
            device.stop.set()
            return b'firmware output'

        reader.read.side_effect = read
        with mock.patch.object(manager, '_matches', return_value=True), \
                mock.patch.object(windows_manager.serial, 'Serial', return_value=reader) as opening, \
                mock.patch.object(device, '_say'), mock.patch.object(device, 'append') as append, \
                mock.patch.object(device, 'close_log'):
            manager._capture(device)
        opening.assert_called_once_with('COM999', timeout=0.25, write_timeout=5,
                                        **SerialSettings(57600).pyserial_options())
        append.assert_called_once_with(b'firmware output')
        self.assertEqual(device.newline, '\r\n')
        self.assertEqual(device.serial_settings.baud, 57600)
        reader.close.assert_called_once()

    def test_profile_credentials_reach_ssh_without_implicit_provisioning(self):
        device = Device('fake', '/fake')
        device.probe_info = {'ips': ['eth0 192.0.2.1/24']}
        with mock.patch.dict(os.environ, {'SERIALD_SSH_USER': 'ubuntu',
                                         'SERIALD_SSH_PASSWORD': 'private-password',
                                         'SERIALD_AUTO_CONFIGURE_SSH': '0'}), \
                mock.patch.object(device, 'stable_dir', return_value='/unused'), \
                mock.patch('seriald_core.device.SshSession') as session_class, \
                mock.patch('seriald_core.device.check_password_login') as check, \
                mock.patch.object(device, 'configure_root_ssh') as provision:
            session_class.return_value.ensure.return_value = True
            session, error = device.ssh_connect()
        self.assertEqual(error, '')
        self.assertIs(session, session_class.return_value)
        self.assertEqual(session_class.call_args.kwargs,
                         {'user': 'ubuntu', 'password': 'private-password'})
        self.assertFalse(session.auto_configured)
        check.assert_not_called()
        provision.assert_not_called()

    def test_shipped_profile_example_is_valid(self):
        path = Path(__file__).resolve().parents[1] / 'examples' / 'profiles.json'
        book = ProfileBook(json.loads(path.read_text()))
        self.assertEqual(book.resolve('EXAMPLE-BOARD-SERIAL').ssh_user, 'ubuntu')
        self.assertEqual(book.resolve(port='EXAMPLE-PHYSICAL-PORT-KEY').probe, 'manual')


if __name__ == '__main__':
    unittest.main()
