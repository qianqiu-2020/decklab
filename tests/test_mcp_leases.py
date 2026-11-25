"""MCP board credentials stay in memory; all HTTP clients here are isolated fakes."""
import copy
import asyncio
import json
import unittest
from unittest import mock

from decklabctl import ClientError
from decklab_mcp import Adapter, Settings
import decklab_mcp


TOKEN_A = 'decklab-ag-' + 'A' * 32
TOKEN_B = 'decklab-ag-' + 'B' * 32


class FakeClientFactory:
    """Public ownership survives Adapter replacement; private client state does not."""

    def __init__(self):
        self.now = 2000000000.0
        self.clients = []
        self.calls = []
        self.owners = {}
        self.effects = {}
        self.sequence = 0
        self.aliases = {'board-a': 'BOARD-A', 'alias-a': 'BOARD-A', 'board-b': 'BOARD-B'}

    def __call__(self, url, timeout=180, token=''):
        client = FakeClient(self, url, timeout, token)
        self.clients.append(client)
        return client

    def selected(self, selector):
        serial = self.aliases.get(selector, selector)
        if selector == 'raw':
            serial = ''
        return {'key': selector, 'serial': serial, 'connection_id': 'connection-' + selector}

    def receipt(self, serial, ttl=300, proof=None):
        if proof is None:
            self.sequence += 1
            proof = {'id': 'lease-%d' % self.sequence, 'generation': self.sequence,
                     'token': 'private-proof-%d' % self.sequence}
        return {'ok': True, 'input_owner': {
            'id': proof['id'], 'generation': proof['generation'], 'serial': serial,
            'scope': 'board', 'actor': 'decklab-mcp', 'state': 'active', 'expires': self.now + ttl},
            'execution_lease': copy.deepcopy(proof)}

    def invoke(self, client, action, ttl=300, **parameters):
        self.calls.append({'action': action, 'token': client.token, 'serial': client.serial,
                           'proof': copy.deepcopy(client.execution_lease), 'ttl': ttl,
                           **parameters})
        effect = self.effects.get(action)
        if effect is not None:
            return effect(client, ttl)
        if action == 'input-owner':
            return {'input_owner': copy.deepcopy(self.owners.get(client.serial))}
        if action == 'claim':
            if self.owners.get(client.serial):
                raise ClientError('Board already occupied', status=423, code='busy')
            receipt = self.receipt(client.serial, ttl)
        elif action == 'renew':
            if not client.execution_lease:
                raise ClientError('This client has no board lease proof')
            receipt = self.receipt(client.serial, ttl, client.execution_lease)
        elif action == 'release':
            if not client.execution_lease:
                raise ClientError('This client has no board lease proof')
            self.owners.pop(client.serial, None)
            client.execution_lease = None
            return {'ok': True, 'input_owner': None}
        else:
            return {'ok': True}
        self.owners[client.serial] = copy.deepcopy(receipt['input_owner'])
        client.execution_lease = copy.deepcopy(receipt['execution_lease'])
        return {key: copy.deepcopy(value) for key, value in receipt.items()
                if key != 'execution_lease'}

    def count(self, action):
        return sum(call['action'] == action for call in self.calls)


class FakeClient:
    def __init__(self, factory, url, timeout, token):
        self.factory, self.url, self.timeout, self.token = factory, url, timeout, token
        self.actor = ''
        self.key = self.serial = self.connection_id = None
        self.execution_lease = None

    def select(self, selector):
        selected = self.factory.selected(selector)
        self.key, self.serial = selected['key'], selected['serial']
        self.connection_id = selected['connection_id']
        self.factory.calls.append({'action': 'select', 'token': self.token, 'serial': self.serial})
        return selected

    def claim_board(self, ttl=300):
        return self.factory.invoke(self, 'claim', ttl)

    def renew_board(self, ttl=300):
        return self.factory.invoke(self, 'renew', ttl)

    def release_board(self, *args, **kwargs):
        return self.factory.invoke(self, 'release')

    def input_owner(self):
        return self.factory.invoke(self, 'input-owner')

    def get(self, route, **query):
        if route == '/api/input-owner':
            return self.input_owner()
        return self.request(route, query=query)

    def post(self, route, **payload):
        routes = {'/api/input-claim': 'claim', '/api/input-renew': 'renew',
                  '/api/input-release': 'release'}
        if route in routes:
            return self.factory.invoke(self, routes[route], payload.get('ttl', 300))
        return self.request(route, payload=payload)

    def request(self, route, payload=None, query=None, timeout=None):
        if route == '/api/input-owner':
            return self.input_owner()
        self.factory.calls.append({'action': 'request', 'route': route, 'token': self.token,
            'serial': self.serial, 'proof': copy.deepcopy(self.execution_lease),
            'payload': copy.deepcopy(payload), 'query': copy.deepcopy(query)})
        return {'ok': True}


class MCPLeaseCacheTests(unittest.TestCase):
    def setUp(self):
        self.factory = FakeClientFactory()
        self.clock = mock.patch('decklab_mcp.time.time', side_effect=lambda: self.factory.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.adapter = self.make_adapter()

    @unittest.skipUnless(decklab_mcp.MCPServer is not None, 'optional MCP SDK is not installed')
    def test_mcp_tools_claim_renew_write_and_release_without_returning_proof(self):
        from mcp import Client
        async def check():
            server = decklab_mcp.create_server(self.adapter.settings, self.adapter)
            async with Client(server) as client:
                async def call(name, **arguments):
                    result = await client.call_tool(name, arguments)
                    self.assertFalse(result.is_error, str(result))
                    self.assertNotIn('private-proof-', json.dumps(result.structured_content))
                    return result.structured_content
                claimed = await call('claim_board', device='board-a', ttl=300)
                self.assertEqual(claimed['input_owner']['scope'], 'board')
                await call('send_input', device='board-a', data='help')
                sent = [entry for entry in self.factory.calls if entry['action'] == 'request'][-1]
                self.assertEqual(sent['proof']['id'], claimed['input_owner']['id'])
                renewed = await call('renew_board', device='board-a', ttl=600)
                self.assertEqual(renewed['input_owner']['generation'], claimed['input_owner']['generation'])
                public = await call('get_input_owner', device='board-a')
                self.assertEqual(public['input_owner']['id'], claimed['input_owner']['id'])
                released = await call('release_board', device='board-a')
                self.assertIsNone(released['input_owner'])
                self.assertIsNone(self.adapter.client(device='board-a').execution_lease)
        asyncio.run(check())

    def make_adapter(self):
        return Adapter(Settings('http://127.0.0.1:8899', TOKEN_A), self.factory)

    def claimed(self, device='board-a', token=None, ttl=300):
        result = self.adapter.claim_board(device, ttl=ttl, token=token)
        proof = copy.deepcopy(self.factory.clients[-1].execution_lease)
        self.assertIsInstance(proof, dict)
        self.assertNotIn(proof['token'], json.dumps(result))
        self.assertNotIn('execution_lease', result)
        return result, proof

    def test_claim_caches_private_proof_for_same_bearer_and_selected_board(self):
        result, proof = self.claimed()
        self.assertEqual(result['input_owner']['serial'], 'BOARD-A')
        self.assertEqual(self.adapter.client(device='board-a').execution_lease, proof)
        self.assertEqual(self.factory.count('claim'), 1)

    def test_cache_uses_selected_serial_not_selector_or_another_board(self):
        _, proof = self.claimed()
        self.assertEqual(self.adapter.client(device='alias-a').execution_lease, proof)
        self.assertIsNone(self.adapter.client(device='board-b').execution_lease)
        self.assertIsNone(self.adapter.client().execution_lease)

    def test_different_bearer_never_receives_cached_secret(self):
        _, proof = self.claimed()
        self.assertIsNone(self.adapter.client(TOKEN_B, 'board-a').execution_lease)
        self.assertEqual(self.adapter.client(TOKEN_A, 'board-a').execution_lease, proof)

    def test_client_proof_is_a_copy_and_cannot_mutate_cache(self):
        _, proof = self.claimed()
        client = self.adapter.client(device='board-a')
        client.execution_lease['token'] = 'changed-by-invocation'
        self.assertEqual(self.adapter.client(device='board-a').execution_lease, proof)

    def test_explicit_execution_scope_overrides_then_restores_cached_proof(self):
        _, cached = self.claimed()
        explicit = {'id': 'explicit', 'generation': 77, 'token': 'explicit-proof'}
        with self.adapter.execution_scope(explicit):
            self.assertEqual(self.adapter.client(device='board-a').execution_lease, explicit)
        self.assertEqual(self.adapter.client(device='board-a').execution_lease, cached)

    def test_second_claim_with_cached_proof_does_not_submit_again(self):
        self.claimed()
        with self.assertRaises(ClientError):
            self.adapter.claim_board('alias-a')
        self.assertEqual(self.factory.count('claim'), 1)

    def test_unidentified_connection_cannot_be_claimed(self):
        with self.assertRaises(ClientError):
            self.adapter.claim_board('raw')
        self.assertEqual(self.factory.count('claim'), 0)

    def test_invalid_claim_receipt_never_caches_proof(self):
        for malformed in ('missing-proof', 'serial', 'id', 'generation', 'expires', 'not-ok'):
            with self.subTest(malformed=malformed):
                factory = FakeClientFactory()
                adapter = Adapter(Settings('http://127.0.0.1:8899', TOKEN_A), factory)

                def bad(client, ttl):
                    receipt = factory.receipt(client.serial, ttl)
                    client.execution_lease = copy.deepcopy(receipt['execution_lease'])
                    if malformed == 'missing-proof':
                        client.execution_lease = None
                    elif malformed == 'serial':
                        receipt['input_owner']['serial'] = 'DIFFERENT-BOARD'
                    elif malformed == 'id':
                        receipt['input_owner']['id'] = 'different-lease'
                    elif malformed == 'generation':
                        client.execution_lease['generation'] = True
                    elif malformed == 'expires':
                        receipt['input_owner']['expires'] = 'tomorrow'
                    else:
                        receipt['ok'] = False
                    return {key: value for key, value in receipt.items() if key != 'execution_lease'}

                factory.effects['claim'] = bad
                with self.assertRaises(ClientError):
                    adapter.claim_board('board-a')
                self.assertIsNone(adapter.client(device='board-a').execution_lease)

    def test_renew_uses_cached_proof_and_replaces_only_valid_receipt(self):
        _, original = self.claimed(ttl=30)
        self.factory.now += 10
        result = self.adapter.renew_board('alias-a', ttl=600)
        self.assertEqual(self.factory.calls[-1]['proof'], original)
        self.assertEqual(result['input_owner']['expires'], self.factory.now + 600)
        self.assertNotIn(original['token'], json.dumps(result))
        self.factory.now += 25
        self.assertEqual(self.adapter.client(device='board-a', writing=True).execution_lease, original)

    def test_release_success_clears_proof_and_allows_a_fresh_observed_claim(self):
        _, original = self.claimed()
        released = self.adapter.release_board('alias-a')
        self.assertTrue(released['ok'])
        self.assertEqual(self.factory.calls[-1]['proof'], original)
        self.assertIsNone(self.adapter.client(device='board-a').execution_lease)
        self.adapter.claim_board('board-a')
        self.assertEqual(self.factory.count('claim'), 2)
        self.assertNotEqual(self.adapter.client(device='board-a').execution_lease, original)

    def test_expired_proof_is_not_used_or_silently_reclaimed(self):
        self.claimed(ttl=30)
        self.factory.now += 31
        self.factory.owners['BOARD-A']['state'] = 'recovery'
        with self.assertRaises(ClientError):
            self.adapter.client(device='board-a', writing=True)
        with self.assertRaises(ClientError):
            self.adapter.claim_board('board-a')
        self.assertEqual(self.factory.count('claim'), 1)

    def test_expired_tombstone_does_not_block_a_collector_confirmed_human_recovery(self):
        self.claimed(ttl=30)
        self.factory.now += 31
        self.factory.owners.clear()  # Explicit human recovery released the owner.
        claimed = self.adapter.claim_board('board-a')
        self.assertEqual(self.factory.count('claim'), 2)
        self.assertEqual(claimed['input_owner']['generation'], 2)

    def test_restarted_adapter_observes_occupied_public_owner_before_any_claim(self):
        _, proof = self.claimed()
        restarted = self.make_adapter()
        with self.assertRaises(ClientError):
            restarted.claim_board('board-a')
        self.assertGreaterEqual(self.factory.count('input-owner'), 2)
        self.assertEqual(self.factory.count('claim'), 1)
        self.assertIsNone(restarted.client(device='board-a').execution_lease)
        self.assertNotIn('token', self.factory.owners['BOARD-A'])
        self.assertNotEqual(self.factory.owners['BOARD-A'], proof)

    def test_malformed_public_owner_is_not_treated_as_available(self):
        self.factory.effects['input-owner'] = lambda client, ttl: {'ok': True}
        with self.assertRaises(ClientError):
            self.adapter.claim_board('board-a')
        self.assertEqual(self.factory.count('claim'), 0)

    def test_unknown_claim_never_repeats_mutation_after_public_owner_appears(self):
        def unknown(client, ttl):
            receipt = self.factory.receipt(client.serial, ttl)
            self.factory.owners[client.serial] = receipt['input_owner']
            error = ClientError('claim response lost', code='lease-unconfirmed')
            error.outcome = 'unknown'
            raise error

        self.factory.effects['claim'] = unknown
        with self.assertRaises(ClientError):
            self.adapter.claim_board('board-a')
        with self.assertRaises(ClientError):
            self.adapter.claim_board('board-a')
        self.assertEqual(self.factory.count('claim'), 1)

    def test_unknown_release_preserves_secret_for_reconciliation_not_writes(self):
        _, proof = self.claimed()

        def unknown(client, ttl):
            error = ClientError('release response lost', code='lease-unconfirmed')
            error.outcome = 'unknown'
            raise error

        self.factory.effects['release'] = unknown
        with self.assertRaises(ClientError):
            self.adapter.release_board('board-a')
        with self.assertRaises(ClientError):
            self.adapter.client(device='board-a', writing=True)
        with self.assertRaises(ClientError):
            self.adapter.claim_board('board-a')
        self.assertEqual(self.factory.count('claim'), 1)
        del self.factory.effects['release']
        result = self.adapter.release_board('board-a')
        self.assertTrue(result['ok'])
        releases = [call for call in self.factory.calls if call['action'] == 'release']
        self.assertEqual([call['proof'] for call in releases], [proof, proof])

    def test_unknown_renew_does_not_turn_into_a_fresh_claim_or_usable_write(self):
        _, proof = self.claimed()

        def unknown(client, ttl):
            error = ClientError('renew response lost', code='lease-unconfirmed')
            error.outcome = 'unknown'
            raise error

        self.factory.effects['renew'] = unknown
        with self.assertRaises(ClientError):
            self.adapter.renew_board('board-a')
        with self.assertRaises(ClientError):
            self.adapter.client(device='board-a', writing=True)
        with self.assertRaises(ClientError):
            self.adapter.claim_board('board-a')
        self.assertEqual(self.factory.count('claim'), 1)
        self.assertEqual([call['proof'] for call in self.factory.calls if call['action'] == 'renew'], [proof])

    def test_unknown_renew_retains_proof_past_old_expiry_for_explicit_reconciliation(self):
        _, proof = self.claimed(ttl=30)
        def unknown(client, ttl):
            raise ClientError('renew response lost', code='lease-unconfirmed')
        self.factory.effects['renew'] = unknown
        with self.assertRaises(ClientError):
            self.adapter.renew_board('board-a', ttl=600)
        self.factory.now += 31
        with self.assertRaises(ClientError):
            self.adapter.client(device='board-a', writing=True)
        self.adapter.release_board('board-a')
        self.assertEqual(self.factory.calls[-1]['proof'], proof)

    def test_invalid_renew_receipt_keeps_original_secret_for_reconciliation(self):
        _, original = self.claimed()

        def invalid(client, ttl):
            receipt = self.factory.receipt(client.serial, ttl)
            client.execution_lease = receipt['execution_lease']
            receipt['input_owner']['serial'] = 'DIFFERENT-BOARD'
            return {'ok': True, 'input_owner': receipt['input_owner']}

        self.factory.effects['renew'] = invalid
        with self.assertRaises(ClientError):
            self.adapter.renew_board('board-a')
        with self.assertRaises(ClientError):
            self.adapter.client(device='board-a', writing=True)
        self.adapter.release_board('board-a')
        self.assertEqual(self.factory.calls[-1]['proof'], original)

    def test_failed_release_receipt_does_not_discard_secret_or_claim_success(self):
        _, original = self.claimed()
        self.factory.effects['release'] = lambda client, ttl: {'ok': False, 'error': 'not released'}
        with self.assertRaises(ClientError):
            self.adapter.release_board('board-a')
        with self.assertRaises(ClientError):
            self.adapter.claim_board('board-a')
        del self.factory.effects['release']
        self.adapter.release_board('board-a')
        self.assertEqual(self.factory.calls[-1]['proof'], original)
        self.assertEqual(self.factory.count('claim'), 1)

    def test_cache_bound_does_not_evict_a_live_secret_to_claim_another_board(self):
        for number in range(128):
            self.adapter.claim_board('BOARD-%03d' % number)
        first = self.adapter.client(device='BOARD-000').execution_lease
        with self.assertRaises(ClientError):
            self.adapter.claim_board('BOARD-128')
        self.assertEqual(self.factory.count('claim'), 128)
        self.assertEqual(self.adapter.client(device='BOARD-000').execution_lease, first)


if __name__ == '__main__':
    unittest.main()
