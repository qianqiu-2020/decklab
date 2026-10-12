"""Installer language is only a validated default, not a browser preference."""
from pathlib import Path
import tempfile
import unittest

from seriald_core.config import read_ui_language


class UiLanguageTests(unittest.TestCase):
    def test_supported_installer_defaults(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / 'ui-language.txt'
            for value in ('en', 'zh-CN'):
                path.write_text(value + '\r\n', encoding='utf-8-sig')
                self.assertEqual(read_ui_language(base), value)

    def test_missing_invalid_or_oversized_defaults_fall_back(self):
        with tempfile.TemporaryDirectory() as base:
            self.assertEqual(read_ui_language(base), '')
            path = Path(base) / 'ui-language.txt'
            for value in ('fr', 'en' * 100, 'en\x00'):
                path.write_text(value)
                self.assertEqual(read_ui_language(base), '')
            path.write_bytes(b'\xff')
            self.assertEqual(read_ui_language(base), '')
