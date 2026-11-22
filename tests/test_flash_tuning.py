"""Measured flash configurations: storage, lookup, use in requests, HTTP action."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from seriald_core import flash_tuning
from seriald_core.flash_runner import LocalFlashExecutor
import test_flash_discovery_integration as usb_fixture
from test_flash_stream import StreamExecutor

TUNING = {'codec': 'gzip', 'gzip_level': 6, 'run_bytes': 128 << 20,
          'usb_bps': 43e6, 'gzip_bps': 250e6, 'write_bps': 570e6}
LEGACY_TUNING = dict(TUNING, codec='dlz', zstd_level=2, background=True, dlz_bps=1.2e9)


def binding(port=(1, 8, 2), chip='k3'):
    return SimpleNamespace(profile_data={'id': 'auto-spacemit-gpt-v1', 'chip_family': chip},
                           device={'port_path': list(port)})


def result(serial='SN-1', tuning=None):
    return {'identity': {'eeprom-serial': serial}, 'system_disk': 'nvme:0',
            'measured': {'usb_bps': 43e6}, 'codecs': [], 'predicted': {'data_s_per_gb': 9.2},
            'tuning': dict(tuning or TUNING)}


class FlashTuningStoreTests(unittest.TestCase):
    def test_port_configuration_wins_and_other_ports_use_newest_of_same_chip(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(flash_tuning.lookup(root, binding()))
            with mock.patch.object(flash_tuning.time, 'time', return_value=100):
                flash_tuning.record(root, binding((1, 8, 2)), result('A', dict(TUNING, gzip_level=1)))
            with mock.patch.object(flash_tuning.time, 'time', return_value=200):
                flash_tuning.record(root, binding((1, 8, 3)), result('B'))
            own = flash_tuning.lookup(root, binding((1, 8, 2)))
            self.assertEqual((own['scope'], own['board_serial'], own['tuning']['gzip_level']), ('port', 'A', 1))
            other = flash_tuning.lookup(root, binding((3, 3)))
            self.assertEqual((other['scope'], other['board_serial']), ('chip', 'B'))
            self.assertIsNone(flash_tuning.lookup(root, binding((3, 3), chip='k1')))
            self.assertTrue(flash_tuning.forget(root, binding((1, 8, 2))))
            self.assertEqual(flash_tuning.lookup(root, binding((1, 8, 2)))['scope'], 'chip')

    def test_saved_legacy_configuration_is_normalized_without_rewriting_history(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'tuning.json'
            entry = dict(key=flash_tuning.key(binding()), chip_family='k3', measured_at=100,
                         tuning=LEGACY_TUNING, measured={'dlz_bps': 1.2e9, 'gzip_bps': 250e6, 'background': True},
                         predicted={'data_s_per_gb': 9.2}, codecs=[{'name': 'dlz'}, {'name': 'gzip'}])
            original = json.dumps({'version': 1, 'entries': [entry]})
            path.write_text(original)
            for target, scope in ((binding(), 'port'), (binding((8, 3)), 'chip')):
                got = flash_tuning.lookup(root, target)
                self.assertEqual(got['scope'], scope)
                self.assertEqual(got['tuning'], TUNING)
                self.assertEqual(got['measured'], {'gzip_bps': 250e6})
                self.assertEqual(got['predicted'], {})
                self.assertEqual(got['codecs'], [])
            self.assertEqual(path.read_text(), original)

    def test_invalid_executor_results_are_not_stored(self):
        with tempfile.TemporaryDirectory() as root:
            for bad in ({'codec': 'lz4', 'usb_bps': 1}, dict(TUNING, usb_bps=0), dict(TUNING, extra=1),
                        dict(TUNING, background='yes'), dict(TUNING, write_bps=float('nan'))):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    flash_tuning.record(root, binding(), result(tuning=bad))
            self.assertFalse((Path(root) / 'tuning.json').exists())

    def test_runner_adds_configuration_to_k3_execute_and_stream_requests_only(self):
        with tempfile.TemporaryDirectory() as root:
            executor = LocalFlashExecutor(root)
            target = binding()
            seen = []
            with mock.patch.object(executor, '_run', side_effect=lambda b, op, request, *a, **k: seen.append((op, request)) or {}), \
                    mock.patch.object(executor, '_request', return_value={'version': 1}):
                executor.stream_flash(target, {'version': 1})
                executor.execute(target, {'id': 'plan'})
                self.assertNotIn('tuning', seen[0][1])
                self.assertNotIn('tuning', seen[1][1])
                executor.stream_flash(target, {'version': 1, 'tuning': dict(LEGACY_TUNING, codec='DLZ1')})
                flash_tuning.record(executor.root, target, result(tuning=LEGACY_TUNING))
                executor.stream_flash(target, {'version': 1})
                executor.execute(target, {'id': 'plan'})
                executor.execute(binding(chip='k1'), {'id': 'plan'})
            self.assertEqual(seen[2][1]['tuning'], TUNING)
            self.assertEqual(seen[3][1]['tuning'], TUNING)
            self.assertEqual(seen[4][1]['tuning'], TUNING)
            self.assertNotIn('tuning', seen[5][1])


class CalibrateExecutor(StreamExecutor):
    supports_calibrate = True

    def __init__(self, root):
        super().__init__(root)
        self.calibrations = []

    def calibrate(self, binding, request, on_event=None, check=lambda: None):
        self.calibrations.append(copy.deepcopy(request))
        on_event({'event': 'progress', 'phase': 'calibrating', 'done': 3, 'total': 7})
        value = result()
        return dict(value, configuration=flash_tuning.record(self.root, binding, value))

    def tuning(self, binding):
        return flash_tuning.lookup(self.root, binding)

    def forget_tuning(self, binding):
        return flash_tuning.forget(self.root, binding)


class FlashCalibrateHTTPTests(usb_fixture.FlashDiscoveryHTTPIntegrationTests):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_measure_then_capabilities_show_configuration_and_forget_clears_it(self):
        with usb_fixture.automatic_flashing() as fixture:
            fixture.manager.flash_executor = fixture.executor = CalibrateExecutor(self.tmp.name)
            probed = self.probe(fixture, self.discover(fixture)[0])
            self.assertTrue(probed['capabilities']['calibrate_supported'], probed['capabilities'])
            self.assertIsNone(probed['capabilities']['flash_configuration'])
            target = probed['target']
            body = dict(self.scope(target), system_storage='nvme:0', measure_write=True, request_id='calibrate-001')
            code, started = self.request(fixture, '/api/flash-calibrate', body)
            self.assertEqual(code, 202, started)
            task = self.terminal(fixture, started['task']['id'])
            self.assertEqual(task['state'], 'completed', task)
            self.assertEqual(task['flash']['operation'], 'calibrate')
            self.assertEqual(task['flash']['configuration']['tuning'], TUNING)
            request = fixture.executor.calibrations[0]
            self.assertEqual((request['system_storage'], request['measure_write'], request['chip_family']), ('nvme:0', True, 'k3'))
            # The same request ID never measures twice.
            self.assertEqual(self.request(fixture, '/api/flash-calibrate', body)[1]['task']['id'], task['id'])
            capabilities = self.probe(fixture, target)['capabilities']
            self.assertEqual(capabilities['flash_configuration']['scope'], 'port')
            code, cleared = self.request(fixture, '/api/flash-calibrate', dict(self.scope(target), action='forget'))
            self.assertEqual((code, cleared['configuration']), (200, None), cleared)

    def test_invalid_options_are_rejected_before_any_device_access(self):
        with usb_fixture.automatic_flashing() as fixture:
            fixture.manager.flash_executor = fixture.executor = CalibrateExecutor(self.tmp.name)
            target = self.probe(fixture, self.discover(fixture)[0])['target']
            for extra in ({'measure_write': 'yes'}, {'system_storage': 'nvme:9'}, {'action': 'erase'}, {'url': 'x'}):
                code, _ = self.request(fixture, '/api/flash-calibrate', dict(self.scope(target), **extra))
                self.assertEqual(code, 400, extra)
            self.assertEqual(fixture.executor.calibrations, [])

    def test_measurement_defaults_to_usb_and_ram(self):
        with usb_fixture.automatic_flashing() as fixture:
            fixture.manager.flash_executor = fixture.executor = CalibrateExecutor(self.tmp.name)
            target = self.probe(fixture, self.discover(fixture)[0])['target']
            body = dict(self.scope(target), request_id='calibrate-usb-only')
            code, started = self.request(fixture, '/api/flash-calibrate', body)
            self.assertEqual(code, 202, started)
            self.assertEqual(self.terminal(fixture, started['task']['id'])['state'], 'completed')
            request, = fixture.executor.calibrations
            self.assertEqual((request['system_storage'], request['measure_write']), ('auto', False))


# The base class's own tests are not repeated here.
for name in list(vars(usb_fixture.FlashDiscoveryHTTPIntegrationTests)):
    if name.startswith('test_') and name not in vars(FlashCalibrateHTTPTests):
        setattr(FlashCalibrateHTTPTests, name, None)


if __name__ == '__main__':
    unittest.main()
