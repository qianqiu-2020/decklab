import json
from pathlib import Path
from contextlib import nullcontext
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from decklab_core.control_slots import ControlSlots
from decklab_core.ha_slots import SlotAction, inventory
from decklab_core.hardware import HardwareBook
from decklab_core.tasks import TaskError
from decklab_core.tasks import TaskManager
from decklab_core.storage import Store
from decklab_core.web_settings import WebSettings


URL = 'http://ha.example.test'
TOKEN = 'fixture-token'
ONE, TWO = 'controllerA/1', 'controllerB/1'


def slot(prefix, number=1):
    entity_prefix = f'{prefix.lower()}_board_{number}'
    return {'device': prefix, 'number': number, 'name': prefix, 'area': 'desk',
            'entities': {'reset': 'switch.' + entity_prefix + '_reset',
                         'enter-download': 'button.' + entity_prefix + '_fastboot_mode'}}


class ControlSlotsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = WebSettings(Path(tmp.name)/'settings.json', {})
        self.manager = SimpleNamespace(lock=threading.RLock(), hardware=HardwareBook(), _flash_presence={},
            boards={sn: SimpleNamespace(serial=sn, label=sn) for sn in ['A','B']},
            store=Mock(has_conflict=Mock(return_value=False)))
        self.slots = ControlSlots(self.manager)
        self.slots.settings = self.settings
        fake = patch('decklab_core.ha_slots.inventory', return_value={ONE:slot('controllerA'), TWO:slot('controllerB')})
        self.inventory = fake.start()
        self.addCleanup(fake.stop)

    def scan(self, token=TOKEN, action='discover'):
        current = self.slots.execute({})
        return self.slots.execute(dict(action=action, revision=current['revision'], origin=URL, token=token))

    def save(self, scan, changes):
        return self.slots.execute(dict(action='save', scan=scan['scan'], revision=scan['revision'],
                                       changes={k:{'serial':sn, 'allow_agent':True} for k,sn in changes.items()}))

    def test_first_use_pulses_only_selected_empty_slot_and_requires_the_current_uart(self):
        self.check_slot_identification()

    def test_offline_slot_can_identify_replacement_but_online_slot_is_protected(self):
        self.save(self.scan(), {TWO: 'A', ONE: 'B'})
        self.manager.boards['B'].connection = SimpleNamespace(dev='COM2', present=True)
        self.check_slot_identification(active_slot=ONE)

    def check_slot_identification(self, active_slot=None):
        scan = self.scan()
        before = self.slots.execute({})['bindings']
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'tasks.sqlite3')
            self.addCleanup(store.close)
            self.manager.store = store
            tasks = self.manager.tasks = TaskManager(store)
            self.addCleanup(tasks.close)
            device = SimpleNamespace(key='fixture-console', serial='', present=True, connection_id='uart-fixture', stop=threading.Event())
            self.manager.resolve = lambda key: device if key == device.key else None
            action = dict(action='identify-slot', revision=scan['revision'], scan=scan['scan'], slot=TWO,
                          request_id='fixture-onboarding-1', key=device.key, connection_id=device.connection_id)
            if active_slot:
                with self.assertRaisesRegex(TaskError, '原板卡仍在线'):
                    self.slots.execute(dict(action, slot=active_slot))
            def target(name):
                return {'target_id': name, 'target_generation': name + '-generation', 'present': True}

            old, unrelated, attached = map(target, ('before', 'other', 'attached'))
            discovery = Mock(argv=('fixture-executor',))
            discovery.refresh.side_effect = [{'targets': [old]}, {'targets': [old, unrelated, attached]}]
            discovery.get.side_effect = [{'document': {'uart_link': {'connection_id': 'another-uart'}}},
                                         {'document': {'uart_link': {'connection_id': device.connection_id, 'state': 'linked'}}}]

            def probe(target_id, generation):
                if target_id == attached['target_id']:
                    device.serial = 'verified-board'
                return {'serial': device.serial}
            discovery.probe.side_effect = probe
            transaction = Mock()
            transaction.actor_scope.return_value = transaction.begin.return_value = nullcontext()

            def trigger(check, outcome):
                check()
                outcome('issued')
                outcome('acknowledged')

            with patch('decklab_core.flash_tasks._discovery',return_value=discovery), \
                    patch('decklab_core.control_slots.transactions_for', return_value=transaction), \
                    patch('decklab_core.ha_slots.SlotAction') as actuator:
                actuator.return_value.run.side_effect = trigger
                started = self.slots.execute(action)['task']
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    task = tasks.store.get(started['id'])
                    if task['state'] not in ('queued', 'running'):
                        break
                    time.sleep(.01)
                self.assertEqual(task['state'], 'completed', task)
                self.assertEqual(task['result']['serial'], 'verified-board')
                self.assertEqual(task['outcome'], 'observed')
                self.assertTrue(task['result']['control']['acknowledged'])
                self.assertEqual(task['result']['control']['observation'], 'usb-identity-verified')
                self.assertEqual(task['progress']['phase'], 'board-usb-identity-verified')
                self.assertEqual(self.slots.execute(action)['task']['id'], started['id'])
                actuator.assert_called_once_with(URL, TOKEN, TWO, 'enter-download', slot('controllerB')['entities']['enter-download'], False)
                actuator.return_value.run.assert_called_once()
                self.assertEqual([call.args[0] for call in discovery.probe.call_args_list], ['other', 'attached'])
                self.assertEqual(self.slots.execute({})['bindings'], before)

    def test_bulk_assign_same_slot_numbers_swap_and_restart(self):
        first = self.scan()
        self.slots.active_revision = first['revision']
        saved = self.save(first, {ONE:'A', TWO:'B'})
        self.assertEqual(saved['revision'], self.slots.active_revision)
        self.assertNotIn(TOKEN,json.dumps(self.settings.snapshot()))
        self.assertNotIn(TOKEN,json.dumps(self.slots.execute({})))
        with self.assertRaisesRegex(TaskError,'更新'):
            self.save(first, {ONE:'B'})
        with self.assertRaisesRegex(ValueError,'重复绑定'):
            self.save(self.scan(), {ONE:'B'})
        self.assertEqual(self.manager.hardware.command('A','reset','web').key,ONE)
        self.save(self.scan(), {ONE:'B',TWO:'A'})
        restored = HardwareBook(self.settings.effective()['documents']['hardware'])
        self.assertEqual(restored.command('A','reset','agent').key,TWO)

    def test_unbound_board_cannot_use_old_command(self):
        self.save(self.scan(), {ONE:'A'})
        cmd = self.manager.hardware.command('A','reset','web')
        with self.slots.using('A','reset',cmd):
            with self.assertRaisesRegex(TaskError,'占用'):
                self.save(self.scan(), {ONE:'B'})
            with self.assertRaisesRegex(TaskError,'执行'):
                self.slots.unbind('A')
        self.slots.unbind('A')
        self.manager.boards['B'].connection = SimpleNamespace(by_path='port', dev='COM2', present=True)
        self.manager._flash_presence['B'] = {'connected':True,'endpoint':'3.7'}
        handoff = self.slots.execute({'action':'list', 'serial':'B'})
        self.assertEqual(handoff['bindings'], [])
        self.assertEqual(handoff['boards'][0]['uart'], 'COM2')
        self.assertEqual(handoff['boards'][0]['flash_usb'], '3.7')
        self.assertEqual(self.manager.hardware.available('A','web')['actions'],[])
        with self.assertRaisesRegex(TaskError,'变化'):
            with self.slots.using('A','reset',cmd):
                self.fail('stale command entered')
        self.save(self.scan(), {ONE:'B'})
        self.assertEqual(self.manager.hardware.command('B','reset','web').key,ONE)
        self.assertFalse(self.manager.hardware.available('A','web')['actions'])

    def test_offline_unbind_and_never_reuse_token_for_another_origin(self):
        self.save(self.scan(), {ONE:'A'})
        self.inventory.reset_mock()
        review = self.scan('', 'saved')
        self.assertFalse(review['slots'][0]['present'])
        self.save(review, {ONE:''})
        self.inventory.assert_not_called()
        with self.assertRaisesRegex(ValueError,'令牌'):
            self.slots.execute(dict(action='discover', revision=self.slots.execute({})['revision'],
                                    origin='http://another.example.test'))

    def test_adopt_only_matching_legacy_commands(self):
        item = slot('controllerA')
        old = {a:{'argv':['/usr/bin/python3','-I','/opt/home_assistant_control.py',URL,'/private/token',e],
                  'allow_agent':True} for a,e in item['entities'].items()}
        old['power-off'] = {'argv':['/bin/false']}
        doc = {'boards':{'A':old}}
        self.manager.hardware = HardwareBook(doc)
        self.settings.save({'version':1,'documents':{'hardware':doc}}, self.settings.snapshot()['revision'])
        scan = self.scan()
        self.assertTrue(scan['slots'][0]['legacy'])
        self.save(scan,{ONE:'B'})
        self.assertEqual(set(self.manager.hardware.boards['A']), {'power-off'})
        self.assertEqual(set(self.manager.hardware.boards['B']), {'reset','enter-download'})

    def test_slot_service_ack_and_unknown_never_replay(self):
        self.save(self.scan(),{ONE:'A'})
        action = self.manager.hardware.command('A','enter-download','web')
        state = {'entity_id':action.entity, 'state':'unknown'}
        for reply in ([], TaskError('timed out',502)):
            with patch('decklab_core.ha_slots.request', side_effect=[state,reply]) as api:
                outcome=Mock()
                if isinstance(reply, list):
                    self.assertEqual(action.run(lambda:None,outcome=outcome)['control_result'],'acknowledged')
                else:
                    with self.assertRaisesRegex(TaskError,'结果未知'):
                        action.run(lambda:None,outcome=outcome)
                self.assertEqual(api.call_count,2)
                self.assertEqual(api.call_args.args[2:],('/api/services/button/press',{'entity_id':action.entity}))

    def test_discovery_groups_by_registry_id_and_ignores_disabled_entities(self):
        rows=[{'device_id':'abc','entity_id':e,'platform':'esphome'} for e in slot('a')['entities'].values()]
        rows += [dict(rows[0],device_id='def',entity_id='switch.b_board_1_reset'),
                 dict(rows[0],entity_id='switch.a_board_4_reset',disabled_by='user')]
        with patch('decklab_core.ha_slots.registries', return_value=([],
                [{'id':'abc','name':'Desk'},{'id':'def','name':'Desk'}],rows)):
            self.assertEqual(set(inventory(URL,TOKEN)),{'abc/1','def/1'})

    def test_zero_based_slots_use_exact_home_assistant_numbers(self):
        rows = [{'device_id': device, 'entity_id': entity, 'platform': 'esphome'}
                for device, numbers in [('abc', range(4)), ('def', range(1, 5))]
                for number in numbers for entity in slot(device, number)['entities'].values()]
        self.inventory.side_effect = inventory
        with patch('decklab_core.ha_slots.registries', return_value=([],
                [{'id':'abc','name':'Desk'}, {'id':'def','name':'Desk'}], rows)):
            first = self.scan()
            self.assertEqual([row['key'] for row in first['slots']],
                             ['abc/0', 'abc/1', 'abc/2', 'abc/3', 'def/1', 'def/2', 'def/3', 'def/4'])
            self.assertEqual(first['slots'][1]['serial'], '')
            self.save(first, {'abc/0': 'A', 'abc/2': 'B'})
        restored = HardwareBook(self.settings.effective()['documents']['hardware'])
        self.assertEqual(restored.wiring('A')['number'], 0)
        self.assertEqual(restored.command('A', 'enter-download', 'agent').entity,
                         'button.abc_board_0_fastboot_mode')
        self.assertEqual(restored.command('B', 'reset', 'web').entity, 'switch.abc_board_2_reset')
