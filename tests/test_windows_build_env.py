"""Packaging environment isolation, without freezing or accessing hardware."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools.prepare_windows_build import prepare


class BuildEnvironmentTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('powershell') or shutil.which('pwsh'), 'requires PowerShell')
    def test_desktop_alias_reconciliation_precedes_validation_and_stops_on_failure(self):
        source = Path(__file__).resolve().parents[1] / 'tools/build_windows.ps1'
        script = r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SOURCE__',[ref]$tokens,[ref]$errors)
if($errors.Count -ne 0){throw 'Builder syntax invalid'}
$sdk=@($ast.FindAll({param($node) $node -is [Management.Automation.Language.IfStatementAst] -and $node.Clauses[0].Item1.Extent.Text -eq '$DesktopRuntime' -and $node.Extent.Text.Contains('--prune-frozen-aliases')},$true))
if($sdk.Count -ne 1){throw 'Expected one SDK finalization block'}
$freeze=$ast.Find({param($node) $node -is [Management.Automation.Language.CommandAst] -and $node.CommandElements[0].Extent.Text -eq '$pyinstaller'},$true)
$notices=$ast.Find({param($node) $node -is [Management.Automation.Language.CommandAst] -and $node.Extent.Text.Contains('@noticeArguments')},$true)
if($freeze.Extent.EndOffset -ge $sdk[0].Extent.StartOffset -or $sdk[0].Extent.EndOffset -ge $notices.Extent.StartOffset){throw 'SDK finalization must be between freeze and notice collection'}
$body=$sdk[0].Clauses[0].Item2.Extent.Text
$block=[ScriptBlock]::Create($body.Substring(1,$body.Length-2))
$DesktopRuntime='fixture-input';$bundle=[IO.Path]::GetTempPath();$python='Invoke-PythonFixture';$desktopManifestHash='fixture-manifest'
function Invoke-PythonFixture {
 $script:calls.Add(@($args))
 $global:LASTEXITCODE=0
 if($script:failPrune -and $args -contains '--prune-frozen-aliases'){$global:LASTEXITCODE=1}
}
function Get-FileHash { param($LiteralPath,$Algorithm) [PSCustomObject]@{Hash='fixture-manifest'} }
foreach($failPrune in @($false,$true)){
 $script:failPrune=$failPrune;$script:calls=[Collections.Generic.List[object]]::new()
 $rejected=$false
 try { & $block } catch { $rejected=$true }
 if($failPrune){
  if(-not $rejected -or $script:calls.Count -ne 1){throw 'Failed reconciliation must stop before validation'}
 } else {
  if($rejected -or $script:calls.Count -ne 2){throw 'Successful reconciliation must validate canonical SDK'}
  if($script:calls[1] -contains '--prune-frozen-aliases'){throw 'Validation must be a separate call'}
 }
 if($script:calls[0] -notcontains '--prune-frozen-aliases'){throw 'First operation must reconcile only validated aliases'}
}
Write-Output 'SDK reconciliation order and failure gate passed'
'''.replace('__SOURCE__', str(source).replace("'", "''"))
        encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        result = subprocess.run([shutil.which('powershell') or shutil.which('pwsh'),
            '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('SDK reconciliation order and failure gate passed', result.stdout)

    @unittest.skipUnless(shutil.which('powershell') or shutil.which('pwsh'), 'requires PowerShell')
    def test_formal_builder_rejects_missing_desktop_sdk_before_preparing_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / 'tools'
            tools.mkdir()
            script = tools / 'build_windows.ps1'
            source = Path(__file__).resolve().parents[1] / 'tools/build_windows.ps1'
            script.write_bytes(source.read_bytes())
            environment = root / 'build/windows-venv'
            environment.mkdir(parents=True)
            marker = environment / 'keep.txt'
            marker.write_text('must not reset an environment for missing SDK input')
            result = subprocess.run([shutil.which('powershell') or shutil.which('pwsh'),
                '-NoProfile', '-NonInteractive', '-File', str(script), '-DesktopRuntime', str(root / 'missing')],
                env=dict(os.environ, OS='Windows_NT'), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('prepared desktop SDK runtime-manifest.json is required', result.stdout + result.stderr)
            self.assertTrue(marker.is_file())

    def test_fast_build_only_reuses_a_complete_matching_dependency_installation(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch('venv.EnvBuilder') as builder:
            root = Path(directory)
            (root / 'tools').mkdir()
            (root / 'tools/build_windows.ps1').write_text('script')
            requirements = root / 'requirements-windows.txt'
            requirements.write_text('dependency==1')
            environment = root / 'build/windows-venv'
            scripts = environment / 'Scripts'
            scripts.mkdir(parents=True)
            for name in ('python.exe', 'pyinstaller.exe'):
                (scripts / name).touch()
            key = prepare(root, local_fast=True)
            builder.return_value.create.assert_called_once_with(environment)
            stamp = environment / 'decklab-dependencies.sha256'
            stamp.write_text(key)
            self.assertEqual(prepare(root, local_fast=True), 'cached')
            builder.return_value.create.assert_called_once()
            requirements.write_text('dependency==2')
            self.assertNotEqual(prepare(root, local_fast=True), key)
            requirements.write_text('dependency==1')
            (scripts / 'python.exe').unlink()
            self.assertEqual(prepare(root, local_fast=True), key)
            self.assertEqual(builder.return_value.create.call_count, 3)

    def test_rebuild_removes_extra_packages_and_preserves_development(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            development = root / '.venv' / 'keep.txt'
            development.parent.mkdir()
            development.write_text('development environment', encoding='utf-8')
            environment = prepare(root)
            python = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
            site = Path(subprocess.check_output(
                [str(python), '-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'],
                text=True).strip())
            unwanted = site / 'decklab_unwanted_build_fixture.py'
            unwanted.write_text('VALUE = 42', encoding='utf-8')
            subprocess.run([str(python), '-I', '-c', 'import decklab_unwanted_build_fixture'], check=True)
            prepare(root)
            result = json.loads(subprocess.check_output([
                str(python), '-I', '-c',
                'import importlib.util,json,sys; print(json.dumps({'
                '"extra": importlib.util.find_spec("decklab_unwanted_build_fixture") is not None,'
                '"isolated": sys.prefix != sys.base_prefix}))'], text=True))
            self.assertEqual(result, {'extra': False, 'isolated': True})
            self.assertIn('include-system-site-packages = false',
                          (environment / 'pyvenv.cfg').read_text())
            self.assertEqual(development.read_text(), 'development environment')

    def test_rejects_redirected_build_directory_before_clearing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / 'other'
            outside.mkdir()
            try:
                (root / 'build').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('Creating directory symlinks is unavailable')
            with mock.patch('tools.prepare_windows_build.venv.EnvBuilder') as builder:
                with self.assertRaises(ValueError):
                    prepare(root)
                builder.assert_not_called()

    def test_rejects_redirected_environment_before_clearing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'build').mkdir()
            outside = root / 'other'
            outside.mkdir()
            try:
                (root / 'build' / 'windows-venv').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('Creating directory symlinks is unavailable')
            with mock.patch('tools.prepare_windows_build.venv.EnvBuilder') as builder:
                with self.assertRaises(ValueError):
                    prepare(root)
                builder.assert_not_called()


if __name__ == '__main__':
    unittest.main()
