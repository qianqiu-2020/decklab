"""Offline Windows administration never imports the COM/HTTP host."""
import builtins
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock

from seriald_core import admin
import seriald_mcp


ROOT = Path(__file__).resolve().parents[1]


class WindowsEntrypointTests(unittest.TestCase):
    def run_admin(self, arguments):
        return self.run_dispatch('admin', arguments)

    def run_dispatch(self, command, arguments):
        original_import = builtins.__import__

        def without_host(name, *args, **kwargs):
            if name in ('seriald_core.windows_manager', 'seriald_core.httpd',
                        'seriald_core.control_server') or name == 'serial':
                raise AssertionError('offline administration imported host: ' + name)
            return original_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            unused = Path(directory) / 'unused'
            with mock.patch.object(sys, 'platform', 'win32'), \
                    mock.patch.object(sys, 'argv', ['seriald.exe', command] + arguments), \
                    mock.patch.dict(os.environ, {'SERIALD_BASE': str(unused)}), \
                    mock.patch('builtins.__import__', side_effect=without_host), \
                    self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(ROOT / 'seriald_windows.py'), run_name='__main__')
            self.assertFalse(unused.exists(), 'help/dispatch must not create runtime storage')
            return stopped.exception.code

    def test_admin_dispatch_preserves_arguments_and_returns_without_starting_host(self):
        arguments = ['--base', 'verified-data', 'resolve-conflict', '--serial', 'SN001',
                     '--reason', 'verified board', '--apply']
        received = []
        with mock.patch.object(admin, 'main', side_effect=lambda: received.extend(sys.argv[1:])):
            self.assertIsNone(self.run_admin(arguments))
        self.assertEqual(received, arguments)

    def test_admin_help_does_not_start_host_or_create_storage(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(self.run_admin(['--help']), 0)
        self.assertIn('correct-serial', output.getvalue())

    def test_invalid_admin_arguments_keep_failure_exit_status(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_admin(['not-an-admin-command']), 2)

    def test_mcp_dispatch_preserves_arguments_without_loading_com_host(self):
        arguments = ['--transport', 'streamable-http', '--url', 'http://127.0.0.1:8898',
                     '--host', '127.0.0.1', '--port', '18899']
        received = []
        with mock.patch.object(seriald_mcp, 'main', side_effect=lambda: received.extend(sys.argv[1:])):
            self.assertIsNone(self.run_dispatch('mcp', arguments))
        self.assertEqual(received, arguments)

    def test_mcp_help_never_starts_a_collector_or_creates_storage(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(self.run_dispatch('mcp', ['--help']), 0)
        self.assertIn('streamable-http', output.getvalue())



class WindowsSplitRoleTests(unittest.TestCase):
    def test_web_and_backend_dispatch_preserve_role_arguments_without_starting_com_host(self):
        from seriald_core import control_server, backend_server
        original_import = builtins.__import__
        def without_com(name, *args, **kwargs):
            if name == 'seriald_core.windows_manager' or name == 'serial':
                raise AssertionError('role dispatcher loaded the COM host')
            return original_import(name, *args, **kwargs)
        for role, module in [('web', control_server), ('backend', backend_server)]:
            arguments = ['--bind', '127.0.0.1', '--port', '18899']
            received = []
            with tempfile.TemporaryDirectory() as directory, \
                    mock.patch.object(sys, 'platform', 'win32'), \
                    mock.patch.object(sys, 'argv', ['seriald.exe', role] + arguments), \
                    mock.patch.dict(os.environ, {'SERIALD_BASE': directory + '/unused', 'SERIALD_BACKEND_URL': 'https://backend.test:8899'}), \
                    mock.patch('builtins.__import__', side_effect=without_com), \
                    mock.patch.object(module, 'main', side_effect=lambda: received.extend(sys.argv[1:])), \
                    self.assertRaises(SystemExit):
                runpy.run_path(str(ROOT / 'seriald_windows.py'), run_name='__main__')
            self.assertEqual(received, arguments)

    def test_network_backend_selects_the_existing_windows_manager(self):
        from seriald_core import backend_server
        from types import SimpleNamespace
        manager = object()
        with mock.patch.dict(sys.modules, {'seriald_core.windows_manager': SimpleNamespace(WindowsManager=manager)}), \
                mock.patch.object(backend_server.os, 'name', 'nt'):
            self.assertIs(backend_server.manager_type(), manager)

    def test_frozen_restart_preserves_web_or_backend_role(self):
        from seriald_core.settings_restart import reexecute
        for role in ('web', 'backend'):
            with self.subTest(role=role), mock.patch.object(sys, 'frozen', True, create=True), \
                    mock.patch.object(sys, 'argv', ['seriald.exe', '--port', '8899']), \
                    mock.patch('os.execve') as execute:
                reexecute({'fixture': 'environment'}, role=role)
            self.assertEqual(execute.call_args.args[1], [sys.executable, role, '--port', '8899'])


if __name__ == '__main__':
    unittest.main()
