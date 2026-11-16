"""Offline validation of the architecture-specific private Linux MCP runtime."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from tools import package_desktop_runtime as desktop_sdk


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('linux_mcp_builder', ROOT / 'tools/build_linux_mcp.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def prepare_desktop_runtime_fixture(destination):
    """Use the production manifest schema and source lock with inert ELF bytes."""
    destination.mkdir(parents=True)
    binary = b'\x7fELF\x02\x01' + bytes(10) + struct.pack('<HHI', 3, 62, 1) + bytes(40)
    for name in ('libdecklab-freerdp.so', 'libfreerdp3.so.3'):
        (destination / name).write_bytes(binary)
    sources = desktop_sdk.reviewed_sources()
    notices = {}
    for component in sources:
        relative = 'third-party/' + component + '/LICENSE.txt'
        path = destination / relative
        path.parent.mkdir(parents=True)
        path.write_text(component + ' notice fixture')
        notices[component] = [relative]
    manifest = {'schema': 1, 'platform': 'linux-amd64', 'abi': 1,
                'wrapper': 'libdecklab-freerdp.so', 'sources': sources,
                'source_files': {name: desktop_sdk.digest(desktop_sdk.ROOT / name) for name in desktop_sdk.SOURCES},
                'notices': notices, 'files': desktop_sdk.tree_inventory(destination)}
    (destination / desktop_sdk.MANIFEST).write_text(json.dumps(manifest))
    return manifest


class LinuxMCPRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        self.executable = self.runtime / builder.EXECUTABLE
        self.executable.write_bytes(self.elf(62))
        self.executable.chmod(0o755)
        (self.runtime / 'third-party').mkdir()
        (self.runtime / 'third-party/inventory.json').write_text('{"schema": 1}')
        self.lock = self.root / 'requirements.lock'
        self.lock.write_text('mcp==2.3.0\n')
        lock = patch.object(builder, 'LOCK', self.lock)
        lock.start()
        self.addCleanup(lock.stop)
        self.notice_verifier = SimpleNamespace(verify=lambda *args, **kwargs: None)
        notices = patch.object(builder, 'notice_module', return_value=self.notice_verifier)
        notices.start()
        self.addCleanup(notices.stop)
        self.manifest = {
            'schema': 1, 'architecture': 'amd64', 'elf_machine': 62,
            'executable': builder.EXECUTABLE, 'python_version': '3.12.16', 'mcp_version': '2.3.0',
            'build_platform': {'os': 'ubuntu', 'version': '22.04', 'glibc': '2.35', 'machine': 'x86_64'},
            'requirements_sha256': builder.sha256(self.lock),
            'source_files': {name: builder.sha256(ROOT / name) for name in builder.SOURCES},
            'notices': 'third-party/inventory.json', 'files': builder.runtime_files(self.runtime)}
        self.publish()

    @staticmethod
    def elf(machine):
        return b'\x7fELF\x02\x01' + bytes(12) + struct.pack('<H', machine)

    def publish(self):
        (self.runtime / 'runtime-manifest.json').write_text(json.dumps(self.manifest))

    def test_valid_descriptor_returns_architecture_without_loading_sdk(self):
        self.assertEqual(builder.validate_runtime(self.runtime, 'amd64'), self.manifest)

    def test_script_resolves_sdk_validator_without_repository_on_python_path(self):
        sdk = self.root / 'desktop-sdk'
        prepare_desktop_runtime_fixture(sdk)
        # A direct script starts with tools/ on sys.path, unlike test imports.
        result = subprocess.run([sys.executable, '-I', '-c',
            "import runpy, sys; ns = runpy.run_path(sys.argv[1]); "
            "print(ns['desktop_runtime_source']('amd64', sys.argv[2]))",
            str(ROOT / 'tools/build_linux_mcp.py'), str(sdk)],
            cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(sdk.resolve()))

    def test_packaged_desktop_sdk_is_checked_even_with_updated_outer_inventory(self):
        sdk = self.runtime / builder.DESKTOP_RUNTIME
        prepare_desktop_runtime_fixture(sdk)
        self.manifest['files'] = builder.runtime_files(self.runtime)
        self.publish()
        self.assertEqual(builder.validate_runtime(self.runtime), self.manifest)
        (sdk / 'libdecklab-freerdp.so').write_bytes((sdk / 'libdecklab-freerdp.so').read_bytes() + b'changed')
        self.manifest['files'] = builder.runtime_files(self.runtime)
        self.publish()
        with self.assertRaisesRegex(ValueError, 'changed checksums'):
            builder.validate_runtime(self.runtime)

    def test_local_freeze_receives_validated_sdk_before_inventory_and_notices(self):
        sdk = self.root / 'desktop-sdk'
        sdk_manifest = prepare_desktop_runtime_fixture(sdk)
        output, wheels = self.root / 'frozen', self.root / 'wheels'
        wheels.mkdir()
        commands = []
        def freeze(command, **unused):
            commands.append(command)
            if str(command[1]).endswith('test_desktop_security.py'):
                self.assertEqual(command[2], '--executable')
                self.assertTrue(Path(command[3]).is_file())
                return
            bundle = Path(command[command.index('--distpath') + 1]) / builder.EXECUTABLE
            bundle.mkdir(parents=True)
            executable = bundle / builder.EXECUTABLE
            executable.write_bytes(self.elf(62))
            executable.chmod(0o755)
            value = command[command.index('--add-data') + 1]
            source, resource = value.rsplit(os.pathsep, 1)
            self.assertEqual(resource, 'desktop-runtime/freerdp')
            shutil.copytree(source, bundle / '_internal' / resource)
            (bundle / '_internal/libfreerdp3.so.3').write_bytes((Path(source) / 'libfreerdp3.so.3').read_bytes())
        def collect(bundle, destination, *unused):
            self.assertEqual(desktop_sdk.validate(bundle / builder.DESKTOP_RUNTIME, 'linux-amd64'), sdk_manifest)
            self.assertFalse((bundle / '_internal/libfreerdp3.so.3').exists())
            destination.mkdir()
            (destination / 'inventory.json').write_text('{"schema": 1}')
        self.notice_verifier.collect = collect
        with patch.object(builder, 'check_local'), patch.object(builder.subprocess, 'run', side_effect=freeze), \
                patch.object(builder.metadata, 'version', return_value='2.3.0'), \
                patch.object(builder.platform, 'python_version', return_value='3.12.16'), \
                patch.object(builder, 'smoke_worker'):
            builder.build_local(output, 'amd64', wheels, sdk)
        manifest = json.loads((output / 'runtime-manifest.json').read_text())
        self.assertIn('seriald_core/desktop_freerdp.py', manifest['source_files'])
        self.assertIn((builder.DESKTOP_RUNTIME / 'runtime-manifest.json').as_posix(), manifest['files'])
        self.assertEqual(len(commands), 2)

    def test_invalid_prepared_sdk_is_rejected_before_freeze(self):
        sdk, wheels = self.root / 'sdk', self.root / 'wheels'
        wheels.mkdir()
        prepare_desktop_runtime_fixture(sdk)
        (sdk / 'libdecklab-freerdp.so').write_bytes(b'changed')
        with patch.object(builder, 'check_local'), patch.object(builder.subprocess, 'run') as run, \
                self.assertRaisesRegex(ValueError, 'changed checksums'):
            builder.build_local(self.root / 'frozen', 'amd64', wheels, sdk)
        run.assert_not_called()

    def test_docker_only_mounts_an_existing_prepared_sdk_read_only(self):
        sdk = self.root / 'sdk'
        prepare_desktop_runtime_fixture(sdk)
        with patch.object(builder.subprocess, 'run') as run, \
                patch.object(builder, 'validate_runtime', return_value=self.manifest):
            builder.build_docker(self.root / 'frozen', 'amd64', 'fixture-image', sdk)
        command = run.call_args.args[0]
        self.assertIn(str(sdk) + ':/desktop-runtime:ro', command)
        self.assertIn('--desktop-runtime /desktop-runtime', command[-1])
        self.assertNotIn('cmake', command[-1])

    def test_prepared_sdk_link_is_not_resolved_before_validation(self):
        sdk = self.root / 'sdk'
        prepare_desktop_runtime_fixture(sdk)
        alias = self.root / 'sdk-link'
        alias.symlink_to(sdk, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'regular directory'):
            builder.desktop_runtime_source('amd64', alias)

    def test_architecture_baseline_and_sources_must_match(self):
        cases = [('architecture', 'arm64'), ('mcp_version', 'missing'),
                 ('requirements_sha256', '0' * 64), ('source_files', {}),
                 ('build_platform', {'os': 'ubuntu', 'version': '24.04', 'glibc': '2.39'}),
                 ('python_version', '3.10.12')]
        for key, value in cases:
            with self.subTest(key=key):
                original = self.manifest[key]
                self.manifest[key] = value
                self.publish()
                with self.assertRaises(ValueError):
                    builder.validate_runtime(self.runtime, 'amd64')
                self.manifest[key] = original
        self.publish()

    def test_changed_missing_and_unlisted_files_are_rejected(self):
        original = self.executable.read_bytes()
        self.executable.write_bytes(original + b'changed')
        with self.assertRaisesRegex(ValueError, 'files'):
            builder.validate_runtime(self.runtime)
        self.executable.write_bytes(original)
        extra = self.runtime / 'credential.json'
        extra.write_text('unexpected build input')
        with self.assertRaisesRegex(ValueError, 'files'):
            builder.validate_runtime(self.runtime)
        extra.unlink()
        self.executable.unlink()
        with self.assertRaisesRegex(ValueError, 'files'):
            builder.validate_runtime(self.runtime)

    def test_worker_and_libraries_must_match_elf_architecture(self):
        for name in (builder.EXECUTABLE, '_internal/module.so'):
            with self.subTest(name=name):
                path = self.runtime / name
                path.parent.mkdir(parents=True, exist_ok=True)
                original = path.read_bytes() if path.exists() else None
                path.write_bytes(self.elf(183))
                self.manifest['files'] = builder.runtime_files(self.runtime)
                self.publish()
                with self.assertRaisesRegex(ValueError, 'architecture'):
                    builder.validate_runtime(self.runtime)
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_missing_execute_bit_or_symlink_is_rejected(self):
        self.executable.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'permission'):
            builder.validate_runtime(self.runtime)
        self.executable.chmod(0o755)
        (self.runtime / 'alias').symlink_to(builder.EXECUTABLE)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            builder.validate_runtime(self.runtime)

    def test_notice_verification_failure_is_not_bypassed(self):
        def fail(*args, **kwargs):
            raise ValueError('unreviewed native provider')
        self.notice_verifier.verify = fail
        with self.assertRaisesRegex(ValueError, 'unreviewed native provider'):
            builder.validate_runtime(self.runtime)

    def test_escaping_and_noncanonical_paths_are_rejected(self):
        for value in ('../outside', '/absolute', 'C:/outside', 'a\\b', 'a//b', './module'):
            with self.subTest(path=value), self.assertRaises(ValueError):
                builder.relative_path(value)

    def test_flatten_only_internal_file_links(self):
        library = self.runtime / 'library.so.1'
        library.write_bytes(b'fixture')
        (self.runtime / 'library.so').symlink_to(library.name)
        builder.flatten_symlinks(self.runtime)
        self.assertFalse((self.runtime / 'library.so').is_symlink())
        self.assertEqual((self.runtime / 'library.so').read_bytes(), b'fixture')
        (self.runtime / 'outside').symlink_to(self.lock)
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            builder.flatten_symlinks(self.runtime)


if __name__ == '__main__':
    unittest.main()
