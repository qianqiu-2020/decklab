"""Documentation coverage must detect new interfaces without importing collectors."""
from pathlib import Path
import tempfile
import unittest

from tools.check_public_docs import GUIDES, check


class PublicDocumentationTests(unittest.TestCase):
    def fixture(self, directory):
        root = Path(directory)
        (root / 'decklab_core').mkdir()
        (root / 'docs/reference').mkdir(parents=True)
        (root / 'decklab.py').write_text('raise RuntimeError("must not import")\n')
        (root / 'decklab_windows.py').write_text('')
        for guide in GUIDES:
            path = root / ('docs/' + guide + '.md')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('')
        (root / 'docs/reference/http-api.md').write_text('/healthz')
        return root

    def test_missing_new_route_and_environment_are_reported_without_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            (root / 'decklab_core/web.py').write_text(
                'raise RuntimeError("must not import")\n'
                'port = environ.get("DECKLAB_FUTURE_OPTION", "0")\n'
                'if route == "/api/future": pass\n')
            environment, routes, missing = check(root)
            self.assertIn('DECKLAB_FUTURE_OPTION', environment)
            self.assertIn('/api/future', routes)
            self.assertEqual(len(missing), 2)
            (root / 'docs/reference/http-api.md').write_text('/healthz and `/api/future`')
            (root / 'docs/reference/environment.md').write_text('`DECKLAB_FUTURE_OPTION`')
            self.assertEqual(check(root)[2], [])

    def test_prefixed_actions_are_not_confused_with_top_level_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            (root / 'decklab_core/actions.py').write_text(
                'def dispatch(action):\n'
                '    if action == "send": pass\n'
                '    if action.startswith("file-transfer/"):\n'
                '        if action in ("start", "chunk"): pass\n')
            (root / 'decklab_core/web.py').write_text('prefix = "/api/artifact-"\n')
            routes = check(root)[1]
            self.assertEqual(routes, {'/healthz', '/api/send',
                                     '/api/file-transfer/start', '/api/file-transfer/chunk'})

    def test_near_match_does_not_document_a_different_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fixture(directory)
            (root / 'decklab_core/web.py').write_text('route = "/api/task"\n')
            (root / 'docs/reference/http-api.md').write_text('/healthz `/api/tasks`')
            self.assertEqual(check(root)[2], ['Undocumented API route: /api/task'])


if __name__ == '__main__':
    unittest.main()
