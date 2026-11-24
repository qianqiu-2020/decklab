"""Check shipped relative links in both release layouts, without installation."""
from pathlib import Path
import hashlib
import json
import re
import tempfile
import unittest
from urllib.parse import unquote, urlsplit
import zipfile

from tools.package_docs import LINK, publish, render


class PackageDocsTests(unittest.TestCase):
    def test_ace_browser_assets_match_the_pinned_distribution(self):
        root = Path(__file__).resolve().parents[1]
        directory = root / 'vendor/ace'
        source = json.loads((directory / 'source.json').read_text())
        self.assertEqual(source['license'], 'BSD-3-Clause')
        self.assertEqual(set(source['files']), {path.name for path in directory.iterdir() if path.name != 'source.json'})
        for name, expected in source['files'].items():
            self.assertEqual(hashlib.sha256((directory / name).read_bytes()).hexdigest(), expected, name)

    def test_desktop_vendor_sources_cover_exact_hashes_and_complete_module_closure(self):
        root = Path(__file__).resolve().parents[1]
        directory = root / 'vendor/novnc'
        source = json.loads((directory / 'source.json').read_text())
        self.assertEqual((source['version'], source['license'], source['transform']), ('1.7.0', 'MPL-2.0', None))
        self.assertEqual(set(source['files']), {
            path.relative_to(directory).as_posix() for path in directory.rglob('*')
            if path.is_file() and path.name != 'source.json'})
        for name, expected in source['files'].items():
            self.assertEqual(hashlib.sha256((directory / name).read_bytes()).hexdigest(), expected['sha256'], name)
        imports = re.compile(r'\b(?:import|export)\s+(?:[^\"\']*?\s+from\s+)?[\"\']([^\"\']+)[\"\']')
        pending, visited = list(source['entry_points']), set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            path = directory / name
            for specifier in imports.findall(path.read_text()):
                self.assertTrue(specifier.startswith('.'), (name, specifier))
                dependency = (path.parent / specifier).resolve()
                self.assertTrue(dependency.is_relative_to(directory.resolve()))
                relative = dependency.relative_to(directory.resolve()).as_posix()
                self.assertIn(relative, source['files'])
                pending.append(relative)
        self.assertEqual(visited, {name for name in source['files'] if name.endswith('.js')})

    def test_websocket_wheels_match_runtime_pins_and_unmodified_license_notices(self):
        from seriald_core.websocket_io import _WHEELS
        directory = Path(__file__).resolve().parents[1] / 'vendor/python'
        source = json.loads((directory / 'websocket-sources.json').read_text())
        self.assertEqual({(row['name'], row['version'], row['filename'], row['sha256']) for row in source['packages']},
                         set(_WHEELS))
        for row in source['packages']:
            path = directory / row['filename']
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row['sha256'])
            self.assertEqual(urlsplit(row['source']).hostname, 'files.pythonhosted.org')
            with zipfile.ZipFile(path) as archive:
                notice = next(name for name in archive.namelist() if name.endswith(('/LICENSE', '/LICENSE.txt')))
                self.assertEqual(archive.read(notice), (directory / row['license_file']).read_bytes())
                wheel = next(name for name in archive.namelist() if name.endswith('.dist-info/WHEEL'))
                self.assertIn(b'Root-Is-Purelib: true', archive.read(wheel))
                self.assertIn(b'Tag: py3-none-any', archive.read(wheel))

    def test_published_third_party_explains_the_vendored_dependency_boundary(self):
        for platform in ('linux', 'windows'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                publish(root, platform)
                text = (root / 'THIRD_PARTY.md').read_text()
                for detail in ('noVNC 1.7.0', 'wsproto 1.3.2', 'h11 0.16.0', 'MPL-2.0',
                               'vendor/novnc/source.json', 'vendor/python/websocket-sources.json',
                               'ZIP importer', 'separate\nfrom the MCP native-dependency inventory'):
                    self.assertIn(detail, text)

    def test_all_published_relative_links_and_images_resolve_in_both_layouts(self):
        for platform in ('linux', 'windows'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'bundle'
                outputs = publish(root, platform)
                count = 0
                for path in outputs:
                    if path.suffix != '.md':
                        continue
                    for match in LINK.finditer(path.read_text(encoding='utf-8')):
                        link = urlsplit(match[2])
                        if link.scheme or link.netloc or not link.path or link.path.startswith('/'):
                            continue
                        target = (path.parent / unquote(link.path)).resolve()
                        self.assertTrue(target.is_relative_to(root.resolve()), (path, link.path))
                        self.assertTrue(target.is_file(), (path, link.path))
                        count += 1
                self.assertGreater(count, 25)
                if platform == 'linux':
                    self.assertTrue((root / 'API.md').is_file())
                    self.assertIn('(INSTALL.md)', (root / 'README.md').read_text(encoding='utf-8'))
                    self.assertIn('(examples/hardware.json)', (root / 'HARDWARE_EXTENSIONS.md').read_text(encoding='utf-8'))
                else:
                    self.assertTrue((root / 'docs/API.md').is_file())
                    self.assertIn('(docs/HARDWARE_EXTENSIONS.md)', (root / 'README.md').read_text(encoding='utf-8'))
                    self.assertIn('(README-project.md)', (root / 'README.zh-CN.md').read_text(encoding='utf-8'))

    def test_link_render_preserves_fragments_queries_and_external_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'src/a.md'
            source.parent.mkdir()
            source.write_text('[local](b.md?q=1#section) [web](https://example.invalid/a) [anchor](#here)')
            target = root / 'bundle/a.md'
            other = root / 'bundle/docs/中文 guide.md'
            result = render(source, target, {(source.parent / 'b.md').resolve(): other})
            self.assertIn('(docs/%E4%B8%AD%E6%96%87%20guide.md?q=1#section)', result)
            self.assertIn('(https://example.invalid/a)', result)
            self.assertIn('(#here)', result)

    def test_unshipped_link_fails_and_runtime_directory_is_not_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.md'
            source.write_text('[missing](missing.md)')
            with self.assertRaisesRegex(ValueError, 'unpackaged'):
                render(source, root / 'out.md', {})
            (root / 'bundle/data').mkdir(parents=True)
            sentinel = root / 'bundle/data/keep'
            sentinel.write_text('keep')
            with self.assertRaisesRegex(ValueError, 'runtime'):
                publish(root / 'bundle', 'windows')
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')
            self.assertFalse((root / 'bundle/README.md').exists())
