import base64
import unittest
from seriald_core.storage_metadata import MetadataReader, read_inventory, MAX_READ, default_storage_for_priority
from seriald_core.tasks import TaskError
from seriald_core.os_contents import ImageError
from test_os_contents import ROOTFS


class StorageMetadataTests(unittest.TestCase):
    def test_eeprom_priority_matches_only_one_disk_of_the_configured_kind(self):
        disks = [{'id': 'nvme:0'}, {'id': 'scsi:0'}]
        self.assertEqual(default_storage_for_priority(disks, 'eeprom:UFS'), 'scsi:0')
        self.assertEqual(default_storage_for_priority(disks, 'eeprom:SSD'), 'nvme:0')
        for priority in ('eeprom:MMC', 'ram-policy', 'eeprom-unavailable'):
            self.assertEqual(default_storage_for_priority(disks, priority), '')
        self.assertEqual(default_storage_for_priority(disks + [{'id': 'scsi:1'}], 'eeprom:UFS'), '')
        self.assertEqual(default_storage_for_priority([], 'eeprom:UFS'), '')

    def fixture(self):
        self.reads = []
        base = 8 * 1024 ** 3
        disk = {'id': 'nvme:0', 'size': 64 * 1024 ** 3, 'sector_size': 512, 'state': 'ready',
                'partitions': [{'name': 'rootfs', 'offset': base, 'size': len(ROOTFS)}]}
        def fetch(extent):
            self.reads.append(extent)
            offset = extent['offset'] - base
            data = ROOTFS[offset:offset + extent['size']]
            return dict(extent, ok=True, data_b64=base64.b64encode(data).decode())
        return {'ok': True, 'storages': [disk], 'default_storage': 'nvme:0',
                'default_candidate': 'nvme:0', 'priority_source': 'eeprom:SSD'}, fetch

    def test_large_disk_metadata_uses_small_64_bit_reads_and_real_os_bytes(self):
        document, fetch = self.fixture()
        result = read_inventory(document, fetch)
        content = result['storages'][0]['partitions'][0]['content']
        self.assertEqual(content['version'], 'Bianbu 2.3.5 · 20260908194336')
        self.assertEqual(content['kernel_modules'], ['6.6.63'])
        self.assertNotIn('do-not-show', str(result))
        self.assertFalse(result['full_backup'])
        self.assertLess(result['metadata_bytes_read'], 1024 ** 2)
        self.assertTrue(all(value['offset'] >= 8 * 1024 ** 3 for value in self.reads))
        self.assertTrue(all(value['size'] <= 64 * 1024 for value in self.reads))
        self.assertEqual(result['priority_source'], 'eeprom:SSD')

    def test_unknown_default_is_preserved_instead_of_selecting_the_first_disk(self):
        document, fetch = self.fixture()
        document['default_storage'] = ''
        result = read_inventory(document, fetch)
        self.assertEqual(result['default_storage'], '')
        self.assertEqual(result['default_candidate'], 'nvme:0')

    def test_unavailable_mmc_remains_visible_without_becoming_a_target(self):
        document, fetch = self.fixture()
        document['storages'].append({'id': 'mmc:0', 'size': 0, 'sector_size': 0,
                                     'state': 'layout-unavailable', 'capacity_verified': False,
                                     'partitions': [], 'error': 'MMC did not respond'})
        result = read_inventory(document, fetch)
        self.assertEqual(result['storages'][1]['error'], 'MMC did not respond')
        self.assertFalse(result['storages'][1]['capacity_verified'])
        self.assertTrue(all(extent['storage_id'] == 'nvme:0' for extent in self.reads))
        self.assertEqual(default_storage_for_priority(result['storages'], 'eeprom:MMC'), '')
        document['default_storage'] = 'mmc:0'
        with self.assertRaisesRegex(TaskError, 'default storage identity'):
            read_inventory(document, fetch)

    def test_partition_ranges_and_disk_identity_are_strict(self):
        for mutation in [lambda d: d['storages'][0].update(id='nvme:0;erase'),
                         lambda d: d['storages'][0]['partitions'][0].update(offset=-1),
                         lambda d: d['storages'][0]['partitions'][0].update(offset='bad'),
                         lambda d: d['storages'][0]['partitions'][0].update(size=513),
                         lambda d: d['storages'][0]['partitions'][0].update(size=128 * 1024 ** 3),
                         lambda d: d.update(default_storage='scsi:0')]:
            document, fetch = self.fixture(); mutation(document)
            with self.assertRaises(TaskError): read_inventory(document, fetch)

    def test_short_wrong_identity_and_disconnected_reads_fail_without_retry(self):
        for kind in ('short', 'identity', 'disconnect'):
            document, original = self.fixture()
            def fetch(extent):
                if kind == 'disconnect': raise TaskError('USB disconnected')
                response = original(extent)
                response.update({'storage_id': 'scsi:0'} if kind == 'identity' else {'data_b64': 'YQ=='})
                return response
            with self.assertRaises(TaskError): read_inventory(document, fetch)
            self.assertLessEqual(len(self.reads), 1)

    def test_cache_and_read_budget_do_not_copy_the_disk(self):
        document, fetch = self.fixture(); disk = document['storages'][0]
        reader = MetadataReader(fetch, disk, disk['partitions'][0]['offset'], len(ROOTFS), [64 * 1024], lambda: None)
        reader.seek(1); first = reader.read(16); reader.seek(1); self.assertEqual(first, reader.read(16))
        self.assertEqual(len(self.reads), 1)
        reader.seek(64 * 1024)
        with self.assertRaisesRegex(ImageError, 'budget'): reader.read(1)
