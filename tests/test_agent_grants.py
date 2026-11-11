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

    def test_connection_grant_persists_attachment_without_inventing_serial(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'grants.sqlite3')
            store = AgentGrantStore(path)
            token, grant = store.issue('connection', key='unprovisioned-usb', connection_id='attachment-1')
            self.assertEqual((grant['scope'], grant['serial'], grant['key'], grant['connection_id']),
                             ('connection', '', 'unprovisioned-usb', 'attachment-1'))
            store.close()
            store = AgentGrantStore(path)
            try:
                self.assertEqual(store.lookup(token), grant)
                store.revoke(grant['id'])
                self.assertIsNone(store.lookup(token))
            finally:
                store.close()

    def test_connection_grant_requires_both_target_fields(self):
        store = AgentGrantStore(':memory:')
        self.addCleanup(store.close)
        for target in ({}, {'key': 'port'}, {'connection_id': 'attachment'},
                       {'key': 'port', 'connection_id': 'x' * 81}):
            with self.subTest(target=target), self.assertRaises(ValueError):
                store.issue('connection', **target)
