"""Documentation coverage must detect new interfaces without importing collectors."""
from pathlib import Path
import tempfile
import unittest

from tools.check_public_docs import GUIDES, check


class PublicDocumentationTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        (root / 'seriald_core').mkdir()
        (root / 'docs').mkdir()
        (root / 'seriald.py').write_text('raise RuntimeError("must not import")\n')
        (root / 'seriald_windows.py').write_text('')
        for guide in GUIDES:
            (root / ('docs/' + guide + '.md')).write_text('')
        (root / 'docs/API.md').write_text('/healthz')
        return root

    def test_missing_new_route_and_environment_are_reported_without_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            (root / 'seriald_core/web.py').write_text(
                'raise RuntimeError("must not import")\n'
                'port = environ.get("SERIALD_FUTURE_OPTION", "0")\n'
                'if route == "/api/future": pass\n')
            environment, routes, missing = check(root)
            self.assertIn('SERIALD_FUTURE_OPTION', environment)
            self.assertIn('/api/future', routes)
            self.assertEqual(len(missing), 2)
            (root / 'docs/API.md').write_text('/healthz and `/api/future`')
            (root / 'docs/CONFIGURATION.md').write_text('`SERIALD_FUTURE_OPTION`')
            self.assertEqual(check(root)[2], [])

    def test_prefixed_actions_are_not_confused_with_top_level_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            (root / 'seriald_core/actions.py').write_text(
                'def dispatch(action):\n'
                '    if action == "send": pass\n'
                '    if action.startswith("file-transfer/"):\n'
                '        if action in ("start", "chunk"): pass\n')
            (root / 'seriald_core/web.py').write_text('prefix = "/api/artifact-"\n')
            routes = check(root)[1]
            self.assertEqual(routes, {'/healthz', '/api/send',
                                     '/api/file-transfer/start', '/api/file-transfer/chunk'})

    def test_near_match_does_not_document_a_different_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            (root / 'seriald_core/web.py').write_text('route = "/api/task"\n')
            (root / 'docs/API.md').write_text('/healthz `/api/tasks`')
            self.assertEqual(check(root)[2], ['Undocumented API route: /api/task'])


if __name__ == '__main__':
    unittest.main()
