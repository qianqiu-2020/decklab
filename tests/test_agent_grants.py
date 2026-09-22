"""Revocable agent credentials never store their plaintext token."""
from pathlib import Path
import tempfile
import unittest

from seriald_core.agent_grants import AgentGrantStore


class AgentGrantTests(unittest.TestCase):
    def test_persistent_device_grant_revoke_and_no_expiry_default(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'grants.sqlite3'
            first = AgentGrantStore(str(path))
            token, issued = first.issue('device', 'SN1')
            self.assertIsNone(issued['expires'])
            self.assertNotIn(token, repr(first.list()))
            first.close()
            self.assertNotIn(token.encode(), path.read_bytes())

            second = AgentGrantStore(str(path))
            self.assertEqual(second.lookup(token)['serial'], 'SN1')
            self.assertEqual(second.revoke(issued['id']), issued['actor'])
            self.assertIsNone(second.lookup(token))
            self.assertIsNone(second.revoke(issued['id']))
            second.close()

    def test_all_device_grant_has_independent_actor(self):
        with tempfile.TemporaryDirectory() as folder:
            store = AgentGrantStore(str(Path(folder) / 'grants.sqlite3'))
            first_token, first = store.issue('all')
            second_token, second = store.issue('all')
            self.assertNotEqual(first_token, second_token)
            self.assertNotEqual(first['actor'], second['actor'])
            self.assertEqual(store.lookup(first_token)['scope'], 'all')
            store.close()
