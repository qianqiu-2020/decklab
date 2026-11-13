"""Independent discovery trust-boundary checks with no USB or serial access."""
import copy
from pathlib import Path
import sys
import tempfile
import threading
import unittest

from seriald_core.board_maintenance import maintenance_for
from seriald_core.devices import Manager
from seriald_core.flash_discovery import FlashDiscovery, _profile, _strong_serial
from seriald_core.storage import Store
from seriald_core.tasks import TaskError, TaskStore
from test_flash_discovery_integration import FakeDiscoveryExecutor, usb_candidate


class DiscoveryReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.manager = Manager(self.store, artifact_dir=Path(self.temporary.name) / 'artifacts')
        self.addCleanup(self.manager.close)
        self.executor = FakeDiscoveryExecutor()
        self.discovery = FlashDiscovery(self.manager, self.executor, argv=(sys.executable,))

    def test_strong_flag_requires_matching_live_serial_and_rejects_factory_values(self):
        candidate = usb_candidate(serial='KNOWN-SERIAL-42', strong=True)
        self.assertEqual(_strong_serial(candidate), 'KNOWN-SERIAL-42')
        for serial in ('dfu-device', 'SpacemiT', 'fastboot', 'device', '0123456789abcdef', '00000000'):
            with self.subTest(serial=serial):
                self.assertEqual(_strong_serial(usb_candidate(serial=serial, strong=True)), '')
        for identity in ({}, {'serialno': 'OTHER-SERIAL-42'}):
            candidate['identity'] = identity
            self.assertEqual(_strong_serial(candidate), '')

    def test_automatic_profile_rejects_device_commands_and_unfixed_query_templates(self):
        original = usb_candidate()['profile']
        self.assertEqual(_profile(copy.deepcopy(original)), original)
        for field, value in (('select_oem', 'erase all'), ('verify_getvar', 'private'),
                             ('size_getvar', 'arbitrary-device-query'),
                             ('full_disk_target', 'mtd')):
            with self.subTest(field=field):
                profile = copy.deepcopy(original)
                profile['storages'][0][field] = value
                with self.assertRaises(TaskError):
                    _profile(profile)

    def test_live_ram_profile_accepts_only_the_fixed_download_environment(self):
        profile = {'id': 'auto-spacemit-gpt-v1', 'chip_family': 'k3',
            'identity': {'version-IC': 'spacemit,k3-pico'}, 'ram_loader_protocol': 'decklab-nor-v1',
            'storages': [{'id': 'active', 'layout_source': 'spacemit-gpt',
                'size_getvar': '__decklab_gpt_capacity', 'sector_size': 512, 'partitions': [], 'sparse': False}]}
        self.assertEqual(_profile(copy.deepcopy(profile)), profile)
        for change in ({'ram_loader_protocol': 'arbitrary-oem'}, {'chip_family': 'k1'},
                       {'bootrom_identity': {'version-IC': 'SPA3609'}}, {'prepare': []}):
            with self.subTest(change=change), self.assertRaises(TaskError):
                _profile(dict(profile, **change))

    def test_parallel_refreshes_cannot_commit_old_enumeration_after_new_generation(self):
        entered, release, second = threading.Event(), threading.Event(), threading.Event()
        calls, failures = [], []
        def discover(argv, probe=False, **kwargs):
            calls.append(len(calls) + 1)
            candidate = usb_candidate()
            if len(calls) == 1:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('first enumeration was not released')
            else:
                second.set()
                candidate['device']['usb_address'] = 8
            return {'devices': [candidate]}
        self.executor.discover = discover
        def refresh():
            try:
                self.discovery.refresh()
            except BaseException as error:
                failures.append(error)
        first = threading.Thread(target=refresh)
        later = threading.Thread(target=refresh)
        first.start()
        self.assertTrue(entered.wait(1))
        later.start()
        try:
            self.assertFalse(second.wait(.1), 'later enumeration must wait for the discovery transaction')
        finally:
            release.set()
            first.join(2)
            later.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(later.is_alive())
        self.assertEqual(failures, [])
        rows = self.discovery._rows()
        self.assertEqual(len(rows), 1)
        target = self.discovery.get(rows[0]['id'])
        self.assertEqual(target['document']['device']['usb_address'], 8)

    def test_missing_usb_connection_address_never_yields_a_writable_binding(self):
        self.executor.candidates[0]['device'].pop('usb_address')
        target = self.discovery.refresh()['targets'][0]
        selected = self.discovery.probe(target['id'], target['generation'])
        self.assertFalse(selected['supported'])
        with self.assertRaises(TaskError):
            self.discovery.binding(target['id'], target['generation'])
        self.assertEqual(self.store.boards(), [])

    def test_duplicate_live_serial_persists_conflict_and_invalidates_previous_target(self):
        first = usb_candidate(serial='DUPLICATE-SERIAL-42', strong=True)
        other = usb_candidate(port=(1, 5), serial='DUPLICATE-SERIAL-42', strong=True)
        other['device']['usb_address'] = 8
        self.executor.candidates = [first, other]
        targets = self.discovery.refresh()['targets']
        a = next(target for target in targets if target['usb']['port_path'] == [1, 4])
        b = next(target for target in targets if target['usb']['port_path'] == [1, 5])
        self.assertTrue(self.discovery.probe(a['id'], a['generation'])['supported'])
        with self.assertRaisesRegex(TaskError, 'conflicting USB'):
            self.discovery.probe(b['id'], b['generation'])
        self.assertTrue(self.store.has_conflict('DUPLICATE-SERIAL-42'))
        self.assertFalse(self.discovery.public(self.discovery.get(a['id']))['supported'])
        with self.assertRaises(TaskError):
            self.discovery.binding(a['id'], a['generation'])

    def test_active_usb_task_blocks_selected_probe_before_any_device_query(self):
        target = self.discovery.refresh()['targets'][0]
        maintenance = maintenance_for(self.manager)
        resources = maintenance.target_resources(target['id'], '', ['flash-usb:local:1.4'])
        tasks = TaskStore(self.store)
        task_id = tasks.create_board('flash', 'web', '', '', 'independent-review-start', 'a' * 64,
            {'target_id': target['id']}, resources,
            target_id=target['id'], target_generation=target['generation'])
        try:
            with self.assertRaises(TaskError):
                self.discovery.probe(target['id'], target['generation'])
            self.assertFalse(any(call['probe'] for call in self.executor.discoveries))
        finally:
            maintenance.release(task_id, confirmed=True)
            tasks.update(task_id, state='cancelled', error='review fixture finished')

    def test_offline_recovery_targets_remain_listed_without_executor_or_successful_scan(self):
        target = self.discovery.refresh()['targets'][0]
        maintenance = maintenance_for(self.manager)
        tasks = TaskStore(self.store)
        task_id = tasks.create_board('flash', 'web', '', '', 'offline-recovery-review', 'b' * 64,
            {'target_id': target['id'], 'attempt_id': 'offline-attempt'},
            maintenance.target_resources(target['id'], resources=['flash-usb:local:1.4']),
            target_id=target['id'], target_generation=target['generation'])
        tasks.update(task_id, state='failed', error='fixture cleanup unknown')
        maintenance.keep_recovery(task_id)
        self.executor.candidates = []
        disconnected = self.discovery.refresh()
        self.assertEqual([value['id'] for value in disconnected['targets']], [target['id']])
        self.assertFalse(disconnected['targets'][0]['present'])
        self.assertFalse(disconnected['targets'][0]['supported'])
        self.discovery.argv = ()
        missing = self.discovery.refresh()
        self.assertFalse(missing['available'])
        self.assertEqual(missing['targets'][0]['id'], target['id'])
        self.discovery.argv = (sys.executable,)
        def fail(*args, **kwargs):
            raise OSError('fixture enumeration unavailable')
        self.executor.discover = fail
        unavailable = self.discovery.refresh()
        self.assertFalse(unavailable['available'])
        self.assertEqual(unavailable['targets'][0]['id'], target['id'])
        self.discovery.argv = ()
        maintenance.release(task_id, confirmed=True)
        self.assertEqual(self.discovery.refresh()['targets'], [])


if __name__ == '__main__':
    unittest.main()
