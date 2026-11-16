"""Temporary files and SQLite only; no USB, serial port or board commands."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seriald_core.nor_backups import compare_payloads, publish_backup
from seriald_core.storage import Store
from seriald_core.tasks import TaskCancelled, TaskError, TaskStore
from seriald_core.transfer_artifacts import ArtifactPolicy, TransferArtifacts


class NORBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / 'plan-test'
        self.workspace.mkdir()
        self.store = Store(str(self.root / 'registry.sqlite3'))
        self.addCleanup(self.store.close)
        self.artifacts = TransferArtifacts(self.store, self.root / 'artifacts',
            ArtifactPolicy(max_file=8 << 20, max_total=16 << 20, reserve_free=0))
        self.task = TaskStore(self.store).create('file-download', 'web', '',
            [{'serial': 'NOR-1', 'connection_id': 'test-no-reader'}])
        self.attempt = 'a' * 24
        self.data = bytearray(b'\xff' * (8 << 20))
        self.data[131072:131072 + 25] = b'U-Boot 2022.10-test\0'.ljust(25, b'\0')
        self.data[655360:655360 + 32] = b'password=private-value\0'.ljust(32, b'\0')
        self.path = self.workspace / ('nor-%s-%s-before.bin' % (self.task, self.attempt))
        self.path.write_bytes(self.data)
        self.snapshot = {'path': str(self.path), 'size': len(self.data),
            'sha256': hashlib.sha256(self.data).hexdigest(), 'layout_digest': 'f' * 64,
            'partitions': [{'name': name, 'offset': offset, 'size': size,
                            'sha256': hashlib.sha256(self.data[offset:offset + size]).hexdigest()}
                for name, offset, size in [('bootinfo', 0, 128 << 10), ('fsbl', 128 << 10, 512 << 10),
                    ('env', 640 << 10, 64 << 10), ('esos', 704 << 10, 1024 << 10),
                    ('opensbi', 1728 << 10, 384 << 10), ('uboot', 2112 << 10, 4096 << 10)]]}

    def publish(self, snapshot=None, check=lambda: None):
        return publish_backup(self.artifacts, self.snapshot if snapshot is None else snapshot,
            self.workspace, self.task, self.attempt, 'NOR-1', check=check)

    def test_whole_private_backup_is_downloadable_with_partition_versions_but_no_env_values(self):
        backup = self.publish()
        self.assertEqual(backup['artifact']['state'], 'available')
        self.assertEqual(backup['artifact']['sha256'], self.snapshot['sha256'])
        self.assertEqual(backup['partitions'][1]['content']['version'], 'U-Boot 2022.10-test')
        self.assertEqual(backup['partitions'][2]['content']['version_state'], 'not-applicable')
        self.assertNotIn('private-value', json.dumps(backup))
        self.assertNotIn(str(self.workspace), json.dumps(backup))
        self.assertEqual(backup['partitions'][0]['content']['sha256_scope'], 'partition')
        with self.artifacts.open(backup['artifact']['id'], 'web', 'NOR-1') as source:
            self.assertEqual(source.read(), self.data)
            if os.name == 'posix':
                self.assertEqual(os.fstat(source.fileno()).st_mode & 0o077, 0)
        for actor, serial in [('agent-one', 'NOR-1'), ('web', 'other-board')]:
            with self.assertRaises(TaskError):
                self.artifacts.get(backup['artifact']['id'], actor, serial)

    def test_after_readback_compares_exact_payload_length_without_trimming_erase_fill(self):
        payload = b'bootinfo-payload\x00\xff'
        self.data[:len(payload)] = payload
        path = self.workspace / ('nor-%s-%s-after.bin' % (self.task, self.attempt))
        path.write_bytes(self.data)
        snapshot = copy.deepcopy(self.snapshot)
        snapshot.update(path=str(path), sha256=hashlib.sha256(self.data).hexdigest())
        snapshot['partitions'][0]['sha256'] = hashlib.sha256(self.data[:128 << 10]).hexdigest()
        digest = hashlib.sha256(payload).hexdigest()
        write = {'storage': 'active', 'partition': 'bootinfo', 'kind': 'partition',
                 'offset': 0, 'size': len(payload), 'sha256': digest}
        backup = publish_backup(self.artifacts, snapshot, self.workspace, self.task, self.attempt,
                                'NOR-1', phase='after', writes=[write])
        content = backup['partitions'][0]['content']
        self.assertNotEqual(content['sha256'], digest)
        self.assertEqual(content['size'], 128 << 10)
        self.assertEqual(content['payload_comparison'], {'size': len(payload), 'sha256': digest,
            'expected_sha256': digest, 'matches': True})
        self.assertNotIn('payload_comparison', backup['partitions'][1]['content'])

    def test_mismatched_payload_is_reported_without_changing_the_whole_partition_digest(self):
        path = self.workspace / ('nor-%s-%s-after.bin' % (self.task, self.attempt))
        path.write_bytes(self.data)
        snapshot = dict(self.snapshot, path=str(path))
        backup = publish_backup(self.artifacts, snapshot, self.workspace, self.task, self.attempt,
            'NOR-1', phase='after', writes=[{'storage': 'active', 'partition': 'bootinfo',
                'kind': 'partition', 'size': 80, 'sha256': '0' * 64}])
        content = backup['partitions'][0]['content']
        self.assertFalse(content['payload_comparison']['matches'])
        self.assertEqual(content['payload_comparison']['sha256'], hashlib.sha256(self.data[:80]).hexdigest())
        self.assertEqual(content['sha256'], self.snapshot['partitions'][0]['sha256'])

    def test_payload_comparison_never_uses_other_storage_or_out_of_partition_ranges(self):
        path = self.workspace / ('nor-%s-%s-after.bin' % (self.task, self.attempt))
        path.write_bytes(self.data)
        writes = [{'storage': 'scsi:0', 'partition': 'bootinfo', 'kind': 'partition', 'size': 80, 'sha256': '0' * 64},
                  {'storage': 'active', 'partition': 'fsbl', 'kind': 'partition', 'size': (512 << 10) + 1, 'sha256': '0' * 64},
                  {'storage': 'active', 'partition': 'env', 'kind': 'partition', 'offset': 0, 'size': 80, 'sha256': '0' * 64}]
        backup = publish_backup(self.artifacts, dict(self.snapshot, path=str(path)), self.workspace,
                                self.task, self.attempt, 'NOR-1', phase='after', writes=writes)
        self.assertTrue(all('payload_comparison' not in part['content'] for part in backup['partitions']))

    def test_before_preview_compares_exact_file_length_and_keeps_original_snapshot(self):
        backup = self.publish()
        original = copy.deepcopy(backup)
        payload = bytes(self.data[131072:131072 + 29])  # Includes NUL and 0xff; never trim fill.
        digest = hashlib.sha256(payload).hexdigest()
        metadata = [{'storage': 'active', 'partition': 'fsbl', 'sha256': expected,
                     'content': {'size': len(payload), 'sha256': expected}} for expected in (digest, '0' * 64)]
        compared = compare_payloads(self.artifacts, backup, metadata, serial='NOR-1')
        same, different = [item['board_comparison'] for item in compared]
        self.assertEqual((same['size'], same['sha256'], same['matches']), (29, digest, True))
        self.assertEqual(same['backup_id'], backup['artifact']['id'])
        self.assertEqual(same['phase'], 'before')
        self.assertFalse(different['matches'])
        self.assertEqual(different['sha256'], digest)
        self.assertNotEqual(backup['partitions'][1]['content']['sha256'], digest)
        self.assertEqual(backup, original)
        self.assertTrue(all('board_comparison' not in item for item in metadata))

    def test_before_comparison_rejects_tampered_backup_and_respects_owner_and_cancellation(self):
        backup = self.publish()
        metadata = [{'storage': 'active', 'partition': 'fsbl', 'sha256': '0' * 64,
                     'content': {'size': 29, 'sha256': '0' * 64}}]
        for kwargs in ({'actor': 'other-agent'}, {'serial': 'other-board'}):
            with self.assertRaises(TaskError):
                compare_payloads(self.artifacts, backup, metadata, **dict({'serial': 'NOR-1'}, **kwargs))
        with self.assertRaises(TaskCancelled):
            compare_payloads(self.artifacts, backup, metadata, serial='NOR-1',
                check=lambda: (_ for _ in ()).throw(TaskCancelled('cancelled')))
        path = self.artifacts._path(backup['artifact']['id'], '.bin')
        # Tamper outside the selected prefix; the complete backup must still verify.
        with path.open('r+b') as source:
            source.seek(len(self.data) - 1)
            source.write(b'\0')
        with self.assertRaisesRegex(TaskError, 'checksum changed'):
            compare_payloads(self.artifacts, backup, metadata, serial='NOR-1')

    def test_before_comparison_never_treats_other_media_or_oversized_files_as_equal(self):
        backup = self.publish()
        metadata = [{'storage': storage, 'partition': part, 'sha256': '0' * 64,
                     'content': {'size': size, 'sha256': '0' * 64}} for storage, part, size in
                    [('scsi:0', 'fsbl', 29), ('active', 'fsbl', (512 << 10) + 1), ('active', 'unknown', 29)]]
        with mock.patch.object(self.artifacts, 'open', side_effect=AssertionError('no comparable NOR payload')):
            self.assertEqual(compare_payloads(self.artifacts, backup, metadata), metadata)

    def test_foreign_or_linked_paths_and_changed_partition_extents_are_rejected(self):
        for changes in ({'path': str(self.root / 'other.bin')}, {'size': (8 << 20) - 1},
                        {'sha256': 'invalid'}, {'partitions': self.snapshot['partitions'][:-1]}):
            with self.subTest(changes=changes):
                with self.assertRaises((TaskError, OSError)):
                    self.publish(dict(self.snapshot, **changes))
        wrong = copy.deepcopy(self.snapshot)
        wrong['partitions'][2]['offset'] += 4096
        with self.assertRaises(TaskError):
            self.publish(wrong)
        if os.name != 'nt':
            self.path.unlink()
            other = self.root / 'foreign.bin'
            other.write_bytes(self.data)
            self.path.symlink_to(other)
            with self.assertRaises(TaskError):
                self.publish()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM transfer_artifacts').fetchone()[0], 0)

    def test_corrupt_whole_digest_never_publishes_an_available_file(self):
        with self.assertRaises(TaskError):
            self.publish(dict(self.snapshot, sha256='0' * 64))
        rows = self.store.db.execute('SELECT state FROM transfer_artifacts').fetchall()
        self.assertEqual([row['state'] for row in rows], ['failed'])
        self.assertEqual(list(self.artifacts.root.iterdir()), [])

    def test_cancellation_during_copy_removes_partial_download(self):
        original = self.artifacts.append
        calls = 0
        def append(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TaskCancelled('cancelled')
            return original(*args, **kwargs)
        with mock.patch.object(self.artifacts, 'append', side_effect=append):
            with self.assertRaises(TaskCancelled):
                self.publish()
        self.assertEqual(list(self.artifacts.root.iterdir()), [])
        self.assertEqual(self.store.db.execute('SELECT state FROM transfer_artifacts').fetchone()['state'], 'failed')

    def test_quota_failure_leaves_the_original_snapshot_intact(self):
        self.artifacts.policy = ArtifactPolicy(max_file=1, max_total=1, reserve_free=0)
        with self.assertRaises(TaskError):
            self.publish()
        self.assertEqual(self.path.read_bytes(), self.data)
