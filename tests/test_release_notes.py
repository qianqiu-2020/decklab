import unittest
from tools.release_notes import extract, ROOT, __version__


class ReleaseNotesTests(unittest.TestCase):
    def test_extracts_only_selected_version_with_nested_headings_and_unicode(self):
        text = '# Changelog\n\n## Unreleased\n- Future\n\n## 1.5.0\n\n### Changes\n- 中文\n\n## 1.4.50\n- Old\n'
        self.assertEqual(extract(text, '1.5.0'), '### Changes\n- 中文\n')

    def test_missing_duplicate_and_empty_sections_fail(self):
        for text in ('## Unreleased\nFuture', '## 1.5.0\n\n## 1.4.50\nOld',
                     '## 1.5.0\nOne\n## 1.5.0\nTwo'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                extract(text, '1.5.0')

    def test_current_version_has_release_notes(self):
        body = extract((ROOT / 'CHANGELOG.md').read_text(encoding='utf-8'), __version__)
        self.assertIn('- ', body)
