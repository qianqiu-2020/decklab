import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from seriald_core.firmware_versions import describe_firmware
from seriald_core.flash_tasks import FlashActions
from seriald_core.storage_metadata import read_inventory
from seriald_core.tasks import TaskError


class FirmwareMediaTests(unittest.TestCase):
    def test_ec_bound_to_receipt_and_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = bytes(range(128))
            files = {}

            def put(name, data):
                (root / name).write_bytes(data)
                files[name] = dict(name=name, size=len(data), sha256=hashlib.sha256(data).hexdigest())

            put('ec.bin', raw)
            put('firmware-bundle.json', json.dumps(dict(scope='k3_nor_firmware_flash_bundle',
                                                       ec_included=True)).encode())
            release = dict(scope='csce2010_ec_firmware_release', schema_version=1, board='spacemit,k3-pico-itx',
                           controller='csce2010', guid='d1b9c82f-97c4-4b1c-8085-c193fea3c813',
                           image=dict(file='ec.bin', sha256=files['ec.bin']['sha256'], bytes=len(raw)),
                           version=36, version_text='V00.24')
            put('ec-release.json', json.dumps(release).encode())
            result = dict(root_path=folder)
            describe_firmware(result, files)
            self.assertEqual(result['ec']['version'], 36)
            row, = result['firmware_versions']
            self.assertNotIn('cab_digests', row)
            put('ec.bin', raw[::-1])
            with self.assertRaises(TaskError):
                describe_firmware(dict(root_path=folder), files)

    def test_ec_is_optional_and_only_accepts_a_boolean(self):
        intent = dict(artifact_id='0' * 24, mode='partitions', verify='none')
        self.assertFalse(FlashActions._intent(intent).get('skip_ec', False))
        self.assertTrue(FlashActions._intent(dict(intent, skip_ec=True))['skip_ec'])
        with self.assertRaises(TaskError):
            FlashActions._intent(dict(intent, skip_ec='false'))

    def test_disk_ids_are_independent_of_ec(self):
        disks = [dict(id=d, size=4096, sector_size=512, partitions=[]) for d in ('scsi:0','nvme:0','mmc:0')]
        ec = dict(state='ready', version='V00.24', copy='RW')
        result = read_inventory(dict(ok=True, storages=disks, ec=ec), None)
        self.assertEqual(result['ec'], ec)
        self.assertEqual([disk['id'] for disk in result['storages']], ['scsi:0','nvme:0','mmc:0'])
        self.assertFalse(result['default_storage'])
