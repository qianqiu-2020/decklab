"""Check shipped relative links in both release layouts, without installation."""
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

from tools.package_docs import LINK, publish, render


class PackageDocsTests(unittest.TestCase):
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
