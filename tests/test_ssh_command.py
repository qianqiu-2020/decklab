"""An isolated SSH server exercises the real OpenSSH command channel."""
import os
import shutil
import socket
import subprocess
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import paramiko

from decklab_core.ssh_command import command_channel, LoginDenied, serial_ssh_identity
from decklab_core.tasks import TaskError


class FixtureServer(paramiko.ServerInterface):
    def __init__(self):
        self.users = []
        self.commands = []
        self.command_ready = threading.Event()

    def check_auth_password(self, username, password):
        self.users.append(username)
        if (username, password) == ('fixture', 'fixture-password'):
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return 'password'

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        self.commands.append(command)
        self.command_ready.set()
        return True


@unittest.skipUnless(os.name == 'posix' and shutil.which('ssh') and shutil.which('sshpass'), 'requires OpenSSH and sshpass')
class CommandChannelTests(unittest.TestCase):
    def setUp(self):
        self.key = paramiko.RSAKey.generate(2048)
        self.peer = FixtureServer()
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.listener.settimeout(8)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 8)
        self.addCleanup(self.listener.close)
        port = patch('decklab_core.ssh_command.SSH_PORT', self.listener.getsockname()[1])
        port.start()
        self.addCleanup(port.stop)
        self.operation = Mock(serial='FIXTURE', boot_id='boot', ssh_login=None)
        self.operation.device.probe_info = {'ips': ['eth0 127.0.0.1/8']}
        self.operation.manager.store.get_ssh_credentials.return_value = {
            'ssh_user':'fixture', 'ssh_password':'fixture-password'}
        # The test listens only on loopback, which production board selection filters.
        address = patch('decklab_core.ssh_command.ssh_address', return_value='127.0.0.1')
        address.start()
        self.addCleanup(address.stop)

    def serve(self):
        try:
            sock, _ = self.listener.accept()
        except OSError:
            return
        with sock, paramiko.Transport(sock) as server:
            server.add_server_key(self.key)
            try:
                server.start_server(server=self.peer)
                channel = server.accept(6)
                if channel and self.peer.command_ready.wait(3):
                    channel.sendall(b'fixture stdout\n')
                    channel.sendall_stderr(b'fixture stderr\n')
                    channel.send_exit_status(7)
                    channel.close()
                    # Let the SSH client drain the session and close its master.
                    server.join(5)
            except (EOFError, OSError, paramiko.SSHException):
                pass

    def channel(self, key):
        probe = SimpleNamespace(returncode=0, stdout=key.get_name() + ' ' + key.get_base64())
        pubkeys = patch('decklab_core.ssh_command.adb_command', return_value=probe)
        pubkeys.start()
        self.addCleanup(pubkeys.stop)
        return command_channel(self.operation, '1', Mock())

    def test_saved_login_reaches_verified_peer_and_keeps_streams_separate(self):
        self.operation.ssh_login = {'user':'other', 'password':'job-only'}
        with self.channel(self.key) as argv:
            result = subprocess.run([*argv, 'fixture-command'], capture_output=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (7, b'fixture stdout\n', b'fixture stderr\n'))
        self.assertEqual(self.peer.commands, [b'fixture-command'])
        self.assertEqual(self.peer.users, ['fixture'])

    def test_verified_serial_keys_can_establish_ssh_without_adb(self):
        from contextlib import nullcontext
        boot = 'abcdabcd-0000-0000-0000-000000000001'
        device = self.operation.device
        device.serial = self.operation.serial
        device.transactions.actor_scope.return_value = nullcontext()
        device._run_cmd.return_value = (True, 'BOOT=' + boot + '\n' + self.key.get_name() + ' ' + self.key.get_base64())
        facts = serial_ssh_identity(device, 'web', '', 'current')
        self.assertEqual(facts['boot_id'], boot)
        self.operation.serial_host_keys = facts['host_keys']
        with patch('decklab_core.ssh_command.adb_command', side_effect=AssertionError('ADB unavailable')):
            with command_channel(self.operation, None, Mock()) as argv:
                result = subprocess.run([*argv, 'fixture-command'], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.peer.commands, [b'fixture-command'])

    def test_job_login_rejection_is_identified_without_issuing_command(self):
        self.operation.ssh_login = {'user':'other', 'password':'job-only'}
        self.operation.manager.store.get_ssh_credentials.return_value = None
        with self.assertRaises(LoginDenied), self.channel(self.key):
            self.fail('SSH accepted a rejected job account')
        self.assertEqual(self.peer.users, ['other'])
        self.assertEqual(self.peer.commands, [])

    def test_rejected_saved_login_does_not_try_a_job_password(self):
        self.operation.manager.store.get_ssh_credentials.return_value = {
            'ssh_user':'stale', 'ssh_password':'unused-password'}
        self.operation.ssh_login = {'user':'fixture', 'password':'fixture-password'}
        with self.assertRaisesRegex(TaskError, '已保存') as rejected, self.channel(self.key):
            self.fail('SSH accepted a rejected saved account')
        self.assertNotIsInstance(rejected.exception, LoginDenied)
        self.assertEqual(self.peer.users, ['stale'])
        self.assertEqual(self.peer.commands, [])

    def test_changed_host_key_rejects_before_password_is_offered(self):
        with self.assertRaises(TaskError), self.channel(paramiko.RSAKey.generate(2048)):
            self.fail('SSH accepted a different server key')
        self.assertEqual(self.peer.users, [])
