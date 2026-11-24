from datetime import date
import re
import unittest
from tools.release_notes import RELEASE_HEADING, extract, ROOT, __version__


class ReleaseNotesTests(unittest.TestCase):
    def test_extracts_only_selected_version_with_nested_headings_and_unicode(self):
        text = ('# Changelog\n\n## [Unreleased]\n- Future\n\n## [1.5.0] - 2026-10-12\n\n'
                '### Changed\n\n- 中文\n\n## [1.4.50] - 2026-10-11\n### Fixed\n\n- Old\n'
                '\n[1.4.50]: https://example.test/releases/v1.4.50\n')
        self.assertEqual(extract(text, '1.5.0'), '### Changed\n\n- 中文\n')
        self.assertEqual(extract(text, '1.4.50'), '### Fixed\n\n- Old\n')

    def test_missing_duplicate_and_empty_sections_fail(self):
        for text in ('## [Unreleased]\nFuture', '## [1.5.0] - 2026-10-12\n\n## [1.4.50] - 2026-10-11\nOld',
                     '## [1.5.0] - 2026-10-12\nOne\n## [1.5.0] - 2026-10-12\nTwo',
                     '## [1.5.0] - 2026-02-30\nInvalid date', '## 1.5.0\nLegacy heading'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                extract(text, '1.5.0')

    def test_project_changelog_uses_dated_linked_versions_and_change_types(self):
        text = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
        unreleased, *headings = re.findall(r'^## (.+)$', text, re.MULTILINE)
        self.assertEqual(unreleased, '[Unreleased]')
        self.assertIn('\n[Unreleased]: ', text)
        versions, dates = [], []
        for heading in headings:
            match = RELEASE_HEADING.fullmatch(heading)
            self.assertIsNotNone(match, heading)
            version, published = match.groups()
            versions.append(version)
            dates.append(date.fromisoformat(published))
            self.assertIn('\n[' + version + ']: ', text)
            body = extract(text, version)
            self.assertRegex(body, r'^### (Added|Changed|Deprecated|Removed|Fixed|Security)\n')
            self.assertIn('- ', body)
        self.assertEqual(versions[0], __version__)
        self.assertEqual(dates, sorted(dates, reverse=True))
