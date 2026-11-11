"""CI DNS recovery is fully mocked; never change this host's resolver."""
from contextlib import redirect_stderr, redirect_stdout
import io
import ipaddress
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest import mock

from tools import ci_ubuntu_network as network


HOST = 'archive.ubuntu.com'


class CINetworkTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        for patch in (
                mock.patch.dict(network.os.environ, GITHUB_ACTIONS='true', RUNNER_ENVIRONMENT='github-hosted'),
                mock.patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=1, st_gid=1, st_mode=0o644)),
                mock.patch.object(Path, 'read_text', return_value='nameserver 127.0.0.53\n# credential=private\n'),
                mock.patch.object(network.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name),
                mock.patch.object(network.subprocess, 'run', side_effect=self.run_command),
                mock.patch.object(network.socket, 'socket', side_effect=AssertionError('network forbidden'))):
            patch.start()
            self.addCleanup(patch.stop)
        self.normal, self.apt, self.root = True, True, True
        self.service, self.repair_ok, self.recover, self.repaired = True, True, True, False
        self.route = 'default via 192.0.2.1 dev eth0 proto dhcp\n'
        self.calls = []

    def run_command(self, arguments, **kwargs):
        self.assertLessEqual(kwargs['timeout'], 5)
        self.assertFalse(kwargs['check'])
        self.calls.append(arguments)
        ok, output = True, ''
        if arguments[0] == 'systemctl':
            ok, output = self.service, 'active'
        elif arguments[0] == 'ip':
            output = self.route
        elif 'getent' in arguments:
            ok = (self.apt if '_apt' in arguments else self.root if arguments[0] == 'sudo' else self.normal)
            ok = ok or self.repaired and self.recover
            output = '91.189.90.11 STREAM\n' if ok else ''
        elif 'resolvectl' in arguments:
            ok = self.repair_ok
            if arguments[-1] == 'flush-caches' and ok:
                self.repaired = True
        else:
            self.fail('unexpected command: ' + repr(arguments))
        return SimpleNamespace(returncode=0 if ok else 1, stdout=output)

    def main(self, repair=True):
        with redirect_stdout(self.output), redirect_stderr(self.output):
            return network.main(['--hosts', HOST, *(['--repair'] if repair else [])])

    def mutations(self):
        return [call for call in self.calls if 'resolvectl' in call]

    def test_repair_rejects_any_non_hosted_environment_before_commands(self):
        for actions, environment in (('', 'github-hosted'), ('true', 'self-hosted'), ('True', 'github-hosted')):
            with mock.patch.dict(network.os.environ, GITHUB_ACTIONS=actions, RUNNER_ENVIRONMENT=environment):
                with self.assertRaises(SystemExit):
                    self.main()
        self.assertEqual(self.calls, [])

    def test_healthy_resolvers_are_noop_and_comments_are_not_printed(self):
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.mutations(), [])
        self.assertNotIn('credential', self.output.getvalue())

    def test_user_specific_failure_never_changes_global_dns(self):
        for normal, apt, root in ((True, False, True), (False, True, True), (False, False, True)):
            self.normal, self.apt, self.root = normal, apt, root
            self.assertEqual(self.main(), 1)
        self.assertEqual(self.mutations(), [])

    def test_failed_public_dns_and_read_only_mode_never_repair(self):
        self.normal = self.apt = self.root = False
        with mock.patch.object(network, 'public_dns', return_value=False):
            self.assertEqual(self.main(), 1)
        with mock.patch.object(network, 'public_dns', return_value=True):
            self.assertEqual(self.main(repair=False), 1)
        self.assertEqual(self.mutations(), [])

    def test_missing_service_or_default_interface_never_repair(self):
        self.normal = self.apt = self.root = False
        with mock.patch.object(network, 'public_dns', return_value=True):
            self.service = False
            self.assertEqual(self.main(), 1)
            self.service, self.route = True, 'default via 192.0.2.1 dev --unsafe\n'
            self.assertEqual(self.main(), 1)
        self.assertEqual(self.mutations(), [])

    def test_verified_recovery_orders_commands_and_rechecks_both_users(self):
        self.normal = self.apt = self.root = False
        with mock.patch.object(network, 'public_dns', return_value=True) as probe:
            self.assertEqual(self.main(), 0)
        probe.assert_called_once_with('1.1.1.1', HOST)
        self.assertEqual(self.mutations(), [
            ['sudo', '-n', 'resolvectl', 'dns', 'eth0', '1.1.1.1'],
            ['sudo', '-n', 'resolvectl', 'domain', 'eth0', '~.'],
            ['sudo', '-n', 'resolvectl', 'flush-caches']])
        self.assertEqual(self.calls[-2:], [['getent', 'ahostsv4', HOST],
            ['sudo', '-n', '-u', '_apt', 'getent', 'ahostsv4', HOST]])

    def test_repair_failure_or_failed_recheck_is_not_reported_as_success(self):
        self.normal = self.apt = self.root = False
        with mock.patch.object(network, 'public_dns', return_value=True):
            self.repair_ok = False
            self.assertEqual(self.main(), 1)
            self.assertEqual(len(self.mutations()), 1)
            self.repair_ok, self.recover = True, False
            self.assertEqual(self.main(), 1)


class DNSPacketTests(unittest.TestCase):
    def query(self):
        return b'\x12\x34' + struct.pack('!5H', 0x0100, 1, 0, 0, 0) + b'\x07archive\x06ubuntu\x03com\0\0\1\0\1'

    def response(self, query, address='91.189.90.11', flags=0x8180):
        return (query[:2] + struct.pack('!5H', flags, 1, 1, 0, 0) + query[12:] + b'\xc0\x0c' +
                struct.pack('!HHIH', 1, 1, 30, 4) + ipaddress.IPv4Address(address).packed)

    def test_valid_public_a_answer_and_malformed_or_private_answers(self):
        query = self.query()
        self.assertTrue(network.valid_answer(self.response(query), query))
        for flags in (0x0180, 0x8380, 0x8980, 0x8183):
            self.assertFalse(network.valid_answer(self.response(query, flags=flags), query))
        for address in ('127.0.0.1', '10.0.0.1', '198.18.0.1', '224.0.0.1', '0.0.0.0'):
            self.assertFalse(network.valid_answer(self.response(query, address), query))
        for packet in (b'', self.response(query)[:-1], b'\0\0' + self.response(query)[2:],
                       self.response(query).replace(b'\xc0\x0c', b'\xc0\xff')):
            self.assertFalse(network.valid_answer(packet, query))

    def test_udp_source_and_timeout_are_validated(self):
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.recvfrom.side_effect = lambda size: (
            self.response(connection.sendto.call_args.args[0]), ('1.1.1.1', 53))
        with mock.patch.object(network.socket, 'socket', return_value=connection):
            self.assertTrue(network.public_dns('1.1.1.1', HOST))
            connection.settimeout.assert_called_with(2)
            connection.recvfrom.return_value = b'', ('8.8.8.8', 53)
            connection.recvfrom.side_effect = None
            self.assertFalse(network.public_dns('1.1.1.1', HOST))
            connection.recvfrom.side_effect = TimeoutError()
            self.assertFalse(network.public_dns('1.1.1.1', HOST))
