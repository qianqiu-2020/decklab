"""Temporary files and SQLite only; no USB, serial port or board commands."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from seriald_core.nor_backups import publish_backup
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
        with self.artifacts.open(backup['artifact']['id'], 'web', 'NOR-1') as source:
            self.assertEqual(source.read(), self.data)
            if os.name == 'posix':
                self.assertEqual(os.fstat(source.fileno()).st_mode & 0o077, 0)
        for actor, serial in [('agent-one', 'NOR-1'), ('web', 'other-board')]:
            with self.assertRaises(TaskError):
                self.artifacts.get(backup['artifact']['id'], actor, serial)

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
