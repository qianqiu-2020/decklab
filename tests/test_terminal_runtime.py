"""Pinned runtime preparation uses synthetic archives and offline asset checks."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from tools import prepare_terminal_runtime as runtime

ROOT = Path(__file__).resolve().parents[1]


class TerminalRuntimeTests(unittest.TestCase):
    def archive(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            prefix = 'node-v%s-win-x64/' % runtime.VERSION
            archive.writestr(prefix + 'node.exe', b'MZ runtime fixture')
            archive.writestr(prefix + 'LICENSE', b'upstream license fixture')
            archive.writestr(prefix + '../outside', b'never extract')
        return stream.getvalue()

    def test_verified_archive_cache_repairs_assets_and_retains_license_and_provenance(self):
        data = self.archive()
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(runtime, 'ARCHIVE_SHA256', hashlib.sha256(data).hexdigest()):
            download = mock.Mock(return_value=data)
            target = runtime.prepare(root, download)
            source = json.loads((target / 'source.json').read_text())
            self.assertEqual(source['archive_sha256'], hashlib.sha256(data).hexdigest())
            self.assertEqual(source['executable_sha256'], hashlib.sha256((target / 'node.exe').read_bytes()).hexdigest())
            self.assertEqual(source['license_sha256'], hashlib.sha256((target / 'LICENSE').read_bytes()).hexdigest())
            self.assertFalse((target.parent / 'outside').exists())
            (target / 'node.exe').write_bytes(b'corrupted')
            runtime.prepare(root, download)
            self.assertEqual((target / 'node.exe').read_bytes(), b'MZ runtime fixture')
            download.assert_called_once_with(runtime.URL)

    def test_bad_download_fails_before_publishing_executable(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                runtime.prepare(root, lambda _: b'wrong archive')
            self.assertFalse((Path(root) / 'build/terminal-runtime/node.exe').exists())

    def test_committed_parser_assets_and_full_notices_match_their_provenance(self):
        for name, filename in [('headless', 'xterm-headless.js'), ('addon-serialize', 'addon-serialize.js')]:
            root = ROOT / 'vendor/terminal'
            manifest = json.loads((root / (name + '-source.json')).read_text())
            self.assertEqual(hashlib.sha256((root / filename).read_bytes()).hexdigest(), manifest['js_sha256'])
            self.assertEqual(hashlib.sha256((root / (name + '-LICENSE')).read_bytes()).hexdigest(), manifest['license_sha256'])
            self.assertEqual(manifest['transform']['target'], 'node12')
            self.assertTrue(manifest['integrity'].startswith('sha512-'))
