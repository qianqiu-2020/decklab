"""Offline Windows administration never imports the COM/HTTP host."""
import builtins
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from decklab_core import admin
import decklab_mcp


ROOT = Path(__file__).resolve().parents[1]


class WindowsEntrypointTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'requires Windows PowerShell')
    def test_protected_lan_launcher_reports_actual_child_exit(self):
        with tempfile.TemporaryDirectory(prefix='launcher test ') as directory:
            launcher = shutil.copy(ROOT / 'tools/windows-launchers/Start-decklab-LAN.ps1', directory)
            compile_child = """Add-Type -OutputAssembly ./decklab.exe -OutputType ConsoleApplication -TypeDefinition '
public class Fixture {
    public static int Main(string[] args) {
        if (string.Join(" ", args) != "--bind 0.0.0.0 --port 19543") return 99;
        return int.Parse(System.Environment.GetEnvironmentVariable("FIXTURE_HOST_EXIT"));
    }
}'"""
            powershell = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass']
            subprocess.run([*powershell, '-Command', compile_child], cwd=directory,
                           check=True, capture_output=True, timeout=30)
            for code in (0, 37):
                # Isolate credential persistence from process supervision. Windows SSH
                # build accounts may have no DPAPI user profile.
                token_file = Path(directory, 'data', 'lan-token.dpapi')
                token_file.unlink(missing_ok=True)
                command = ("function ConvertTo-SecureString { param($String, [switch]$AsPlainText, [switch]$Force) 'secure' }; "
                           "function ConvertFrom-SecureString { process { 'fixture' } }; & $env:FIXTURE_LAUNCHER -Port 19543")
                result = subprocess.run([*powershell, '-Command', command],
                                        cwd=directory, env=dict(os.environ, FIXTURE_LAUNCHER=launcher,
                                                                FIXTURE_HOST_EXIT=str(code)),
                                        capture_output=True, timeout=30)
                if code:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b'decklab exited with code 37', result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)

    def run_admin(self, arguments):
        return self.run_dispatch('admin', arguments)

    def run_dispatch(self, command, arguments):
        original_import = builtins.__import__

        def without_host(name, *args, **kwargs):
            if name in ('decklab_core.windows_manager', 'decklab_core.httpd',
                        'decklab_core.control_server') or name == 'serial':
                raise AssertionError('offline administration imported host: ' + name)
            return original_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            unused = Path(directory) / 'unused'
            with mock.patch.object(sys, 'platform', 'win32'), \
                    mock.patch.object(sys, 'argv', ['decklab.exe', command] + arguments), \
                    mock.patch.dict(os.environ, {'DECKLAB_BASE': str(unused)}), \
                    mock.patch('builtins.__import__', side_effect=without_host), \
                    self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(ROOT / 'decklab_windows.py'), run_name='__main__')
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
        with mock.patch.object(decklab_mcp, 'main', side_effect=lambda: received.extend(sys.argv[1:])):
            self.assertIsNone(self.run_dispatch('mcp', arguments))
        self.assertEqual(received, arguments)

    def test_mcp_help_never_starts_a_collector_or_creates_storage(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(self.run_dispatch('mcp', ['--help']), 0)
        self.assertIn('streamable-http', output.getvalue())



class WindowsSplitRoleTests(unittest.TestCase):
    def test_web_and_backend_dispatch_preserve_role_arguments_without_starting_com_host(self):
        from decklab_core import control_server, backend_server
        original_import = builtins.__import__
        def without_com(name, *args, **kwargs):
            if name == 'decklab_core.windows_manager' or name == 'serial':
                raise AssertionError('role dispatcher loaded the COM host')
            return original_import(name, *args, **kwargs)
        for role, module in [('web', control_server), ('backend', backend_server)]:
            arguments = ['--bind', '127.0.0.1', '--port', '18899']
            received = []
            with tempfile.TemporaryDirectory() as directory, \
                    mock.patch.object(sys, 'platform', 'win32'), \
                    mock.patch.object(sys, 'argv', ['decklab.exe', role] + arguments), \
                    mock.patch.dict(os.environ, {'DECKLAB_BASE': directory + '/unused', 'DECKLAB_BACKEND_URL': 'https://backend.test:8899'}), \
                    mock.patch('builtins.__import__', side_effect=without_com), \
                    mock.patch.object(module, 'main', side_effect=lambda: received.extend(sys.argv[1:])), \
                    self.assertRaises(SystemExit):
                runpy.run_path(str(ROOT / 'decklab_windows.py'), run_name='__main__')
            self.assertEqual(received, arguments)

    def test_network_backend_selects_the_existing_windows_manager(self):
        from decklab_core import backend_server
        from types import SimpleNamespace
        manager = object()
        with mock.patch.dict(sys.modules, {'decklab_core.windows_manager': SimpleNamespace(WindowsManager=manager)}), \
                mock.patch.object(backend_server.os, 'name', 'nt'):
            self.assertIs(backend_server.manager_type(), manager)

    def test_frozen_restart_preserves_web_or_backend_role(self):
        from decklab_core.settings_restart import reexecute
        for role in ('web', 'backend'):
            with self.subTest(role=role), mock.patch.object(sys, 'frozen', True, create=True), \
                    mock.patch.object(sys, 'argv', ['decklab.exe', '--port', '8899']), \
                    mock.patch('os.execve') as execute:
                reexecute({'fixture': 'environment'}, role=role)
            self.assertEqual(execute.call_args.args[1], [sys.executable, role, '--port', '8899'])


if __name__ == '__main__':
    unittest.main()
