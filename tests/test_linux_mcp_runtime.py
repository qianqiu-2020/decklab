"""Offline validation of the architecture-specific private Linux MCP runtime."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('linux_mcp_builder', ROOT / 'tools/build_linux_mcp.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


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
            'executable': builder.EXECUTABLE, 'python_version': '3.10.12', 'mcp_version': '2.3.0',
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

    def test_architecture_baseline_and_sources_must_match(self):
        cases = [('architecture', 'arm64'), ('mcp_version', 'missing'),
                 ('requirements_sha256', '0' * 64), ('source_files', {}),
                 ('build_platform', {'os': 'ubuntu', 'version': '24.04', 'glibc': '2.39'}),
                 ('python_version', '3.12.1')]
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
