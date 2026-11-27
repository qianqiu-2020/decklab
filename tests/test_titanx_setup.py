import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from decklab_core import titanx_setup as setup
from decklab_core.titanx_enroll import enroll
from decklab_core.web_settings import SettingsError


class SetupTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.gui = self.root / 'gui.json'
        for key, value in [('CONFIG', self.root / 'admin.json'), ('GUI_CONFIG', self.gui)]:
            patcher = patch.object(setup.worker, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = {'node_id': 'station', 'broker': 'tcp://mqtt.example.test:1883',
                       'api_url': 'https://platform.example.test', 'api_token': 'dummy-token',
                       'lab_id': 3, 'project': 'K1', 'projects': {'2': 'K3'},
                       'web_origin': 'https://station.example.test', 'slots': {'1': 'ONE', '2': 'TWO'}}
        rows = [{'serial': serial, 'alive': online, 'info': {'identity_status': state}}
                for serial, online, state in [('ONE', True, 'confirmed'), ('TWO', False, 'confirmed'),
                                               ('UNKNOWN', True, 'provisional')]]
        self.connect = Mock()
        self.connect.return_value.getresponse.return_value = Mock(status=200, read=lambda n: json.dumps({'devices': rows}))
        self.nodes = []
        self.boards = []
        self.live = []
        self.writes = []
        def handle(path, body):
            if path == '/deviceNamespace/list':
                return [{'id': 3, 'name': 'Lab'}]
            if path == '/deviceNode/list':
                return self.nodes
            if path == '/deviceNode/listVO':
                return [dict(n, deviceBoards=self.boards) for n in self.nodes]
            if path == '/deviceBoard/unregistered':
                return self.live
            self.writes.append((path, body))
            if path == '/deviceNode/add':
                self.nodes.append(dict(body, id=9))
            elif path == '/deviceBoard/add':
                self.boards.append(body)
            else:
                self.fail(path)
        self.platform = Mock(request=handle)
        client = patch.object(setup.TeamCases, 'request', side_effect=handle)
        client.start()
        self.addCleanup(client.stop)

    def test_batch_registration_and_readback(self):
        self.assertEqual([b['serial'] for b in setup.status(self.connect)['boards']], ['ONE', 'TWO'])
        setup.initialize(self.config, self.connect)
        self.assertEqual(self.gui.stat().st_mode & 0o777, 0o600)
        read = setup.worker.read_config()
        def advance():
            return enroll(read, read['slots'], self.platform, self.root/'pending')
        self.assertFalse(advance())
        for slot, serial in enumerate(['ONE', 'TWO'], 1):
            self.live.append({'nodeId':'station', 'deviceId':f'station:{slot}', 'decklabSerial':serial,
                              'decklabSlot':slot,'remoteComserver':100,'remoteCom':slot})
            self.assertFalse(advance())
            if slot == 1:
                self.assertFalse(advance())  # second board still offline
        self.assertTrue(advance())
        self.assertTrue(advance())
        self.assertEqual(len(self.nodes), 1)
        self.assertEqual(self.nodes[0]['namespaceId'], 3)
        self.assertEqual([(r['nodeId'], r['project'], r['remoteCom']) for r in self.boards],
                         [(9,'K1',1),(9,'K3',2)])
        self.assertEqual(len(self.writes), 3)
        result = setup.status(self.connect, True)
        self.assertEqual(result['registered'], ['ONE','TWO'])
        self.assertNotIn('dummy-token', json.dumps(result))
        with self.assertRaises(SettingsError):
            setup.initialize(dict(self.config, node_id='other'), self.connect)
        self.assertEqual(setup.worker.read_config(), read)

    def test_stale_selection_and_foreign_lab_do_not_create(self):
        for change in [{'slots': {'1':'UNKNOWN'}}, {'lab_id': 2}, {'first_com':65535}]:
            with self.subTest(change=change), self.assertRaises(SettingsError):
                setup.initialize(dict(self.config, **change), self.connect)
            self.assertFalse(self.gui.exists())
        setup.worker.CONFIG.write_text('{}')
        with self.assertRaises(SettingsError):
            setup.initialize(self.config, self.connect)
        self.assertFalse(self.gui.exists())
