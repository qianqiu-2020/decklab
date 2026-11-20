"""USB link nonce pairs a flashing attachment with its board console; no real USB or UART."""
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from seriald_core import device as device_module
from seriald_core import devices
from seriald_core.console_link import LINK_PREFIX, ConsoleLinks
from seriald_core.flash_discovery import FlashDiscovery
from seriald_core.storage import Store
from test_flash_discovery_integration import FakeDiscoveryExecutor, usb_candidate


class ConsoleLinksTests(unittest.TestCase):
    def test_unique_echo_is_reported_and_nonce_is_single_use(self):
        links, console = ConsoleLinks(), object()
        token = links.start()
        links.observe(console, 'noise fastboot_handle_command: ' + LINK_PREFIX + token + '\r\n')
        links.observe(console, 'again ' + LINK_PREFIX + token)
        self.assertEqual(links.finish(token, wait=0.1, settle=0), [console])
        links.observe(object(), LINK_PREFIX + token)
        self.assertEqual(links.finish(token, wait=0, settle=0), [])

    def test_other_nonce_or_partial_text_is_not_evidence(self):
        links, console = ConsoleLinks(), object()
        token = links.start()
        links.observe(console, LINK_PREFIX + token[:-1])
        links.observe(console, LINK_PREFIX + '0' * 16)
        self.assertEqual(links.finish(token, wait=0.05, settle=0), [])

    def test_echo_arriving_after_executor_returns_is_still_collected(self):
        links, console = ConsoleLinks(), object()
        token = links.start()
        timer = threading.Timer(0.1, links.observe, (console, LINK_PREFIX + token))
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertEqual(links.finish(token, wait=1, settle=0), [console])


class EchoingExecutor(FakeDiscoveryExecutor):
    """Simulates SpacemiT firmware printing the unknown getvar name on its UART."""
    def __init__(self):
        super().__init__()
        self.echo_to = []

    def discover(self, argv, probe=False, device=None, check=lambda: None, link_token=None):
        result = super().discover(argv, probe, device, check, link_token)
        if probe and link_token:
            for console in self.echo_to:
                console.append(('fastboot_handle_command: ' + LINK_PREFIX + link_token + '\r\n').encode())
            for candidate in result['devices']:
                candidate['link_sent'] = True
        return result


class ConsoleLinkDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(str(root / 'registry.sqlite3'))
        self.addCleanup(self.store.close)
        for module in (devices, device_module):
            patch = mock.patch.object(module, 'LOG_ROOT', str(root / 'logs'))
            patch.start()
            self.addCleanup(patch.stop)
        self.manager = devices.Manager(self.store, artifact_dir=root / 'artifacts')
        self.addCleanup(self.manager.close)
        self.connections = []
        self.addCleanup(lambda: [connection.close_log() for connection in self.connections])
        self.executor = EchoingExecutor()
        self.discovery = FlashDiscovery(self.manager, self.executor, argv=(sys.executable,))

    def console(self, port, serial=None, label=None):
        connection = devices.Device('usb-' + port, port, self.manager)
        connection.present, connection.fd = True, 98765
        self.connections.append(connection)
        self.manager.devices[connection.by_path] = connection
        if serial:
            self.manager.identify(connection, {'serial': serial, 'eeprom_serial': serial, 'hostname': label or serial})
        return connection

    def probe(self, candidate):
        self.executor.candidates = [candidate]
        target = self.discovery.refresh()['targets'][0]
        return self.discovery.probe(target['id'], target['generation'])

    def test_bootrom_attachment_takes_the_identified_console_board_without_flash_identity(self):
        console = self.console('COM5', 'HW3MPK3321280183', 'debian1')
        self.executor.echo_to = [console]
        selected = self.probe(usb_candidate())
        self.assertEqual(selected['console_link']['state'], 'linked')
        self.assertEqual(selected['console_link']['serial'], 'HW3MPK3321280183')
        self.assertEqual(selected['label'], 'debian1')
        # Flashing identity still comes only from USB-side evidence.
        self.assertEqual(selected['serial'], '')
        self.assertTrue(self.manager._flash_presence['HW3MPK3321280183']['connected'])

    def test_unidentified_console_is_bound_to_strong_usb_serial(self):
        console = self.console('COM7')
        console.append(b'BootROM output before identification\n')
        self.executor.echo_to = [console]
        selected = self.probe(usb_candidate(serial='BPMIM102080640040', strong=True))
        self.assertEqual(selected['console_link']['state'], 'linked')
        self.assertEqual(console.serial, 'BPMIM102080640040')
        record = self.store.get('BPMIM102080640040')
        self.assertEqual(record['info']['identity_status'], 'confirmed')
        self.assertEqual(record['info']['serial_source'], 'usb-link')
        self.assertEqual(record['last_path'], console.by_path)
        self.assertTrue(any(event['action'] == 'identified-usb-link'
                            for event in self.store.events('BPMIM102080640040')))

    def test_existing_confirmed_source_is_kept_when_console_reconnects(self):
        self.store.bind({'serial': 'SN-KEEP-001', 'serial_source': 'eeprom-onie', 'identity_status': 'confirmed',
                         'hostname': 'kept-host'}, '', '', 'kept-board')
        console = self.console('COM8')
        self.executor.echo_to = [console]
        self.probe(usb_candidate(serial='SN-KEEP-001', strong=True))
        record = self.store.get('SN-KEEP-001')
        self.assertEqual((record['label'], record['info']['serial_source'], record['info']['hostname']),
                         ('kept-board', 'eeprom-onie', 'kept-host'))
        self.assertEqual(console.serial, 'SN-KEEP-001')

    def test_different_board_on_identified_console_is_reported_not_rebound(self):
        console = self.console('COM5', 'OLD-BOARD-0001')
        self.executor.echo_to = [console]
        selected = self.probe(usb_candidate(serial='NEW-BOARD-0002', strong=True))
        self.assertEqual(selected['console_link']['state'], 'mismatch')
        self.assertEqual(selected['console_link']['usb_serial'], 'NEW-BOARD-0002')
        self.assertEqual(console.serial, 'OLD-BOARD-0001')
        self.assertIsNone(self.store.get('NEW-BOARD-0002')['last_path'] or None)

    def test_echo_on_two_consoles_is_ambiguous_and_binds_nothing(self):
        first, second = self.console('COM5'), self.console('COM6')
        self.executor.echo_to = [first, second]
        selected = self.probe(usb_candidate(serial='SN-TWO-CONSOLES', strong=True))
        self.assertEqual(selected['console_link']['state'], 'ambiguous')
        self.assertEqual((first.serial, second.serial), ('', ''))
        self.assertNotIn('SN-TWO-CONSOLES', self.manager._flash_presence.get('', {}))

    def test_unidentified_console_and_bootrom_are_only_paired(self):
        console = self.console('COM5')
        self.executor.echo_to = [console]
        selected = self.probe(usb_candidate())
        self.assertEqual(selected['console_link']['state'], 'paired')
        self.assertEqual(console.serial, '')
        row = self.discovery.get(selected['id'])
        self.assertEqual(row['document']['uart_link']['connection_id'], console.connection_id)
        self.assertEqual(self.discovery._linked_console(row['document']['uart_link']), console)

    def test_no_console_echo_leaves_no_link_and_retires_the_nonce(self):
        self.console('COM5', 'SN-SILENT-001')
        started = time.monotonic()
        selected = self.probe(usb_candidate())
        self.assertIsNone(selected['console_link'])
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.manager.console_links._pending, {})
        self.assertEqual(len(self.executor.link_tokens), 1)

    def test_reenumerated_attachment_drops_the_previous_link(self):
        console = self.console('COM5', 'SN-REPLUG-001')
        self.executor.echo_to = [console]
        selected = self.probe(usb_candidate())
        self.assertEqual(selected['console_link']['state'], 'linked')
        replugged = usb_candidate()
        replugged['device']['usb_address'] = 9
        self.executor.candidates = [replugged]
        target = self.discovery.refresh()['targets'][0]
        self.assertIsNone(target['console_link'])
        self.assertNotIn('SN-REPLUG-001', self.manager._flash_presence)


if __name__ == '__main__':
    unittest.main()
