import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from decklab_core.agent_grants import AgentGrantStore
from decklab_core.titanx_workbench import prepare
from decklab_core.titanx_enroll import enroll


class TitanXWorkbenchTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = Path(folder.name) / 'worker.json'
        self.grants = AgentGrantStore(':memory:')
        self.addCleanup(self.grants.close)
        self.config = {'broker': 'ssl://mqtt.example.test:8883', 'node_id': 'bench'}
        self.discover = Mock(return_value=[
            {'alive': True, 'serial': 'BOARD-A', 'info': {'identity_status': 'confirmed'}}])

    def generate(self):
        return prepare(self.config, self.state, 'http://127.0.0.1:8898', self.grants, self.discover)

    def test_pause_last_slot_and_restore_same_com(self):
        self.config.update(slots={'7':'BOARD-A'}, first_com=2)
        self.generate()
        token = json.loads(self.state.read_text())['tokens']['BOARD-A']
        self.config['paused_slots'] = ['7']
        self.assertEqual(self.generate(), '')
        self.assertIsNone(self.grants.lookup(token))
        self.config['paused_slots'] = []
        self.assertIn('7: 8', self.generate())
        self.assertEqual(json.loads(self.state.read_text())['slots'], {'7':'BOARD-A'})

    def test_first_start_restart_and_explicit_board_selection(self):
        self.config.update(api_url='https://example.test', api_token='catalogue-secret')
        self.discover.return_value.append({'serial': 'OFFLINE', 'alive': False})
        config = self.generate()
        saved = json.loads(self.state.read_text())
        token = saved['tokens']['BOARD-A']
        self.assertEqual(self.grants.lookup(token)['scope'], 'device')
        self.assertIn('backend: decklab', config.splitlines())
        self.assertNotIn('manage_boards', config)
        self.assertNotIn('catalogue-secret', config)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        self.discover.reset_mock()
        self.assertEqual(self.generate(), config)
        self.discover.assert_not_called()
        self.config.update(slots={'2': 'BOARD-B'}, first_com=7)
        config = self.generate()
        self.assertIn('2: 8', config)
        self.assertIsNone(self.grants.lookup(token))
        saved = json.loads(self.state.read_text())
        grant = self.grants.lookup(saved['tokens']['BOARD-B'])
        self.grants.revoke_request(grant['id'])
        with self.assertRaisesRegex(ValueError, 'revoked'):
            self.generate()

    def test_ambiguous_boards_and_invalid_broker_leave_no_grants(self):
        self.discover.return_value *= 2
        with self.assertRaisesRegex(ValueError, 'multiple boards'):
            self.generate()
        self.config.update(broker='tcp://user:password@mqtt.example.test:1883')
        with self.assertRaisesRegex(ValueError, 'broker'):
            self.generate()
        self.assertEqual(self.grants.list(), [])

    def test_enroll_reads_back_a_lost_reply_and_preserves_existing_policy(self):
        nodes, boards, writes = [], [], []
        self.config.update(api_url='https://platform.example.test', api_token='dummy', lab_id=1,
                           project='K1', web_origin='https://station.example.test', accept_dispatch=True)
        live = {'nodeId': 'bench', 'deviceId': 'bench:1', 'decklabSlot': 1, 'decklabSerial': 'S',
                'remoteComserver': 100, 'remoteCom': 1}

        def request(path, body):
            if path == '/deviceNode/list':
                return nodes
            if path == '/deviceNode/listVO':
                return [dict(nodes[0], deviceBoards=boards)]
            if path == '/deviceBoard/unregistered':
                return [live]
            writes.append(path)
            if path == '/deviceNode/add':
                nodes.append(dict(body, id=8))
            elif path == '/deviceBoard/add':
                boards.append(dict(body, id=12))
                raise OSError('reply lost')
            else:
                self.fail(path)

        platform = Mock(request=request)
        run = lambda: enroll(self.config, {'1': 'S'}, platform, self.state)
        self.assertFalse(run())
        with self.assertRaisesRegex(OSError, 'reply lost'):
            run()
        saved_board = boards.pop()
        self.assertFalse(run())  # delayed readback cannot repeat the POST
        boards.append(saved_board)
        boards[0]['noDispatch'] = True
        self.assertTrue(run())
        self.assertTrue(run())
        self.assertTrue(boards[0]['noDispatch'])
        self.assertEqual(writes, ['/deviceNode/add', '/deviceBoard/add'])
        self.assertFalse(self.state.exists())
        boards[0]['decklabSerial'] = 'different'
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            run()
