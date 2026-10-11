"""Real spool and SQLite fixtures; no board IO or runtime data directories."""
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from seriald_core.storage import Store
from seriald_core.tasks import TaskStore, TaskError
from seriald_core.transfer_artifacts import TransferArtifacts, ArtifactPolicy


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.store = Store(str(self.path / 'registry.sqlite3'))
        self.addCleanup(self.store.close)
        self.policy = ArtifactPolicy(max_file=100, max_total=150, reserve_free=0,
                                     idle_seconds=10, ready_seconds=20)
        self.spool = TransferArtifacts(self.store, self.path / 'spool', self.policy)

    def task(self, actor='web'):
        return TaskStore(self.store).create('file-upload', actor, '', [
            {'serial': 'SN1', 'connection_id': 'connection'}])

    def ready(self, data=b'payload', actor='web', serial='SN1'):
        row = self.spool.create('upload', 'image.bin', len(data), actor, serial)
        if data:
            self.spool.append(row['id'], 0, data, actor, serial)
        return self.spool.seal(row['id'], actor, serial)

    def test_ready_upload_survives_reopen_with_digest_and_readable_file(self):
        row = self.ready()
        self.assertEqual(row['sha256'], hashlib.sha256(b'payload').hexdigest())
        reopened = Store(str(self.path / 'registry.sqlite3'))
        try:
            spool = TransferArtifacts(reopened, self.spool.root, self.policy)
            spool.recover()
            with spool.open(row['id'], serial='SN1') as file:
                self.assertEqual(file.read(), b'payload')
            self.assertEqual(spool.get(row['id'])['state'], 'ready')
        finally:
            reopened.close()

    def test_upload_retry_checks_bytes_and_incomplete_upload_cannot_seal(self):
        row = self.spool.create('upload', 'firmware', 7)
        artifact_id = row['id']
        self.spool.append(artifact_id, 0, b'abc')
        self.assertEqual(self.spool.append(artifact_id, 0, b'abc')['received'], 3)
        for offset, data in ((0, b'xyz'), (4, b'x'), (2, b'cdef')):
            with self.assertRaises(TaskError):
                self.spool.append(artifact_id, offset, data)
        with self.assertRaises(TaskError):
            self.spool.seal(artifact_id)
        self.spool.append(artifact_id, 3, b'defg')
        self.assertEqual(self.spool.seal(artifact_id)['state'], 'ready')
        self.assertEqual(self.spool.seal(artifact_id)['state'], 'ready')

    def test_claim_is_exclusive_and_failed_runner_can_return_unchanged_upload(self):
        row, task_id = self.ready(), self.task()
        self.spool.claim(row['id'], task_id, serial='SN1')
        with self.assertRaises(TaskError):
            self.spool.claim(row['id'], self.task())
        with self.assertRaises(TaskError):
            self.spool.remove(row['id'])
        with self.assertRaises(TaskError):
            self.spool.open(row['id'])
        with self.spool.open(row['id'], task_id=task_id) as file:
            self.assertEqual(file.read(), b'payload')
        self.spool.finish_claim(row['id'], task_id)
        self.assertEqual(self.spool.get(row['id'])['state'], 'ready')
        self.assertIsNone(self.spool.get(row['id'])['task_id'])
        self.spool.claim(row['id'], task_id)
        self.spool.finish_claim(row['id'], task_id, consumed=True)
        self.assertEqual(self.spool.get(row['id'])['state'], 'consumed')
        self.assertEqual(list(self.spool.root.iterdir()), [])

    def test_agent_and_board_scope_cannot_read_append_or_remove_another_artifact(self):
        row = self.ready(actor='agent-one')
        for actor, serial in (('agent-two', 'SN1'), ('agent-one', 'SN2')):
            for method in (self.spool.get, self.spool.open, self.spool.remove):
                with self.assertRaises(TaskError) as failure:
                    method(row['id'], actor=actor, serial=serial)
                self.assertEqual(failure.exception.status, 404)
        with self.spool.open(row['id'], actor='web', serial='SN1') as file:
            self.assertEqual(file.read(), b'payload')

    def test_download_requires_task_and_is_hidden_until_published(self):
        task_id = self.task()
        with self.assertRaises(TaskError):
            self.spool.create('download', 'dump', 3)
        row = self.spool.create('download', 'dump', 3, task_id=task_id)
        with self.assertRaises(TaskError):
            self.spool.open(row['id'])
        with self.assertRaises(TaskError):
            self.spool.append(row['id'], 0, b'abc')
        self.spool.append(row['id'], 0, b'abc', task_id=task_id)
        row = self.spool.seal(row['id'], task_id=task_id)
        self.assertEqual(row['state'], 'available')
        with self.spool.open(row['id']) as file:
            self.assertEqual(file.read(), b'abc')

    def test_quota_counts_declared_sizes_and_deletion_releases_reservation(self):
        row = self.spool.create('upload', 'one', 100)
        with self.assertRaises(TaskError) as error:
            self.spool.create('upload', 'two', 51)
        self.assertEqual(error.exception.status, 413)
        self.spool.remove(row['id'])
        self.assertEqual(self.spool.create('upload', 'two', 100)['size'], 100)
        self.assertEqual(len(list(self.spool.root.iterdir())), 1)

    def test_empty_files_still_reserve_an_artifact_slot(self):
        spool = TransferArtifacts(self.store, self.spool.root, ArtifactPolicy(max_files=1, reserve_free=0))
        row = spool.create('upload', 'empty', 0)
        with self.assertRaises(TaskError) as failure:
            spool.create('upload', 'another', 0)
        self.assertEqual(failure.exception.status, 413)
        spool.remove(row['id'])
        self.assertEqual(spool.create('upload', 'another', 0)['state'], 'receiving')

    def test_disk_space_and_invalid_names_reject_before_creating_files(self):
        for name in ('../secret', 'windows\\secret', 'bad\nname', ''):
            with self.assertRaises(TaskError):
                self.spool.create('upload', name, 10)
        with mock.patch('seriald_core.transfer_artifacts.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaises(TaskError) as error:
                self.spool.create('upload', 'file', 10)
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(list(self.spool.root.iterdir()), [])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM transfer_artifacts').fetchone()[0], 0)

    def test_symlinks_and_hardlinks_cannot_redirect_upload_writes(self):
        target = self.path / 'outside'
        target.write_bytes(b'original')
        for link in ('symlink', 'hardlink'):
            row = self.spool.create('upload', link, 8)
            path = self.spool.root / (row['id'] + '.part')
            path.unlink()
            try:
                path.symlink_to(target) if link == 'symlink' else path.hardlink_to(target)
            except OSError as error:
                if link == 'symlink':
                    continue  # Windows may lack symlink privilege; native gate tracks it.
                raise error
            with self.assertRaises(TaskError):
                self.spool.append(row['id'], 0, b'changed!')
            self.spool.remove(row['id'])
            self.assertEqual(target.read_bytes(), b'original')

    def test_expiry_removes_idle_files_but_never_active_task_files(self):
        receiving = self.spool.create('upload', 'partial', 3)
        ready = self.ready()
        claimed = self.ready(b'x')
        self.spool.claim(claimed['id'], self.task())
        self.spool.expire(now=max(receiving['updated'], ready['updated'], claimed['updated']) + 21)
        self.assertEqual(self.spool.get(receiving['id'])['state'], 'expired')
        self.assertEqual(self.spool.get(ready['id'])['state'], 'expired')
        self.assertEqual(self.spool.get(claimed['id'])['state'], 'claimed')
        self.assertEqual(len(list(self.spool.root.iterdir())), 1)

    def test_recovery_interrupts_writing_and_returns_claimed_upload_without_replay(self):
        claimed = self.ready()
        task_id = self.task()
        self.spool.claim(claimed['id'], task_id)
        writing = self.spool.create('download', 'partial', 3, task_id=self.task())
        self.spool.append(writing['id'], 0, b'a', task_id=writing['task_id'])
        TaskStore(self.store).recover()
        self.spool.recover()
        self.assertEqual(self.spool.get(claimed['id'])['state'], 'ready')
        self.assertEqual(self.spool.get(writing['id'])['state'], 'failed')
        self.assertEqual(list(self.spool.root.glob('*.part')), [])
        self.assertEqual(len(list(self.spool.root.glob('*.bin'))), 1)

    def test_open_readers_prevent_expiry_and_deletion_until_every_reader_closes(self):
        row = self.ready()
        first, second = self.spool.open(row['id']), self.spool.open(row['id'])
        try:
            self.spool.expire(now=row['updated'] + 100)
            self.assertEqual(self.spool.get(row['id'])['state'], 'ready')
            with self.assertRaises(TaskError):
                self.spool.remove(row['id'])
            first.close()
            first.close()
            self.spool.expire(now=row['updated'] + 100)
            self.assertEqual(self.spool.get(row['id'])['state'], 'ready')
            self.assertEqual(second.read(), b'payload')
        finally:
            first.close()
            second.close()
        self.spool.expire(now=row['updated'] + 100)
        self.assertEqual(self.spool.get(row['id'])['state'], 'expired')
        self.assertEqual(self.spool._readers, {})

    def test_missing_ready_file_is_failed_on_recovery_and_quota_is_released(self):
        row = self.ready()
        (self.spool.root / (row['id'] + '.bin')).unlink()
        self.spool.recover()
        self.assertEqual(self.spool.get(row['id'])['state'], 'failed')
        self.assertEqual(self.spool.create('upload', 'new', 100)['size'], 100)

    def test_manager_uses_registry_directory_and_recovers_without_creating_idle_spool(self):
        from seriald_core.devices import Manager
        manager = Manager(self.store)
        self.addCleanup(manager.close)
        self.assertEqual(manager.artifacts.root, self.path / 'transfers')
        self.assertFalse(manager.artifacts.root.exists())
        row = manager.artifacts.create('upload', 'one', 1)
        manager.artifacts.append(row['id'], 0, b'a')
        manager.close()
        restarted = Manager(self.store)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.artifacts.get(row['id'])['received'], 1)

    def test_committed_task_recovery_consumes_its_claimed_upload(self):
        row, task_id = self.ready(), self.task()
        self.spool.claim(row['id'], task_id)
        TaskStore(self.store).update(task_id, state='completed', result={'ok': True})
        self.spool.recover()
        self.assertEqual(self.spool.get(row['id'])['state'], 'consumed')
        self.assertEqual(list(self.spool.root.iterdir()), [])

    def test_partial_file_db_crash_gap_is_failed_instead_of_replaying_append(self):
        row = self.spool.create('upload', 'partial', 5)
        self.spool.append(row['id'], 0, b'a')
        with (self.spool.root / (row['id'] + '.part')).open('ab') as file:
            file.write(b'uncommitted')
        self.spool.recover()
        self.assertEqual(self.spool.get(row['id'])['state'], 'failed')
        self.assertEqual(list(self.spool.root.iterdir()), [])

    def test_create_commit_failure_does_not_leave_an_orphan_file_or_quota(self):
        original = self.store.db
        class FailedCommit:
            def __getattr__(self, name):
                return getattr(original, name)
            def __enter__(self):
                return original.__enter__()
            def __exit__(self, *error):
                original.rollback()
                raise OSError('commit failed')
        with mock.patch.object(self.store, 'db', FailedCommit()):
            with self.assertRaises(OSError):
                self.spool.create('upload', 'empty', 0)
        self.assertEqual(list(self.spool.root.iterdir()), [])
        self.assertEqual(original.execute('SELECT COUNT(*) FROM transfer_artifacts').fetchone()[0], 0)

    def test_sealing_does_not_replace_an_unexpected_existing_final_file(self):
        row = self.spool.create('upload', 'empty', 0)
        path = self.spool.root / (row['id'] + '.bin')
        path.write_bytes(b'preserve')
        with self.assertRaises(TaskError):
            self.spool.seal(row['id'])
        self.assertEqual(path.read_bytes(), b'preserve')
        self.assertEqual(self.spool.get(row['id'])['state'], 'receiving')

    def test_publish_failure_cleans_files_and_metadata_instead_of_leaving_sealing(self):
        row = self.spool.create('upload', 'empty', 0)
        with mock.patch('seriald_core.transfer_artifacts.os.link', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.spool.seal(row['id'])
        self.assertEqual(self.spool.get(row['id'])['state'], 'failed')
        self.assertEqual(list(self.spool.root.iterdir()), [])

    def test_environment_limits_are_validated_and_override_defaults(self):
        policy = ArtifactPolicy.from_env({'SERIALD_TRANSFER_MAX_FILE_BYTES': '100',
            'SERIALD_TRANSFER_MAX_TOTAL_BYTES': '200', 'SERIALD_TRANSFER_MIN_FREE_BYTES': '0',
            'SERIALD_TRANSFER_IDLE_SECONDS': '5', 'SERIALD_TRANSFER_RETENTION_SECONDS': '10'})
        self.assertEqual((policy.max_file, policy.max_total, policy.reserve_free,
                          policy.idle_seconds, policy.ready_seconds), (100, 200, 0, 5, 10))
        for value in ('-1', '1.5', 'bad', '', True):
            with self.assertRaises(ValueError):
                ArtifactPolicy.from_env({'SERIALD_TRANSFER_MAX_FILE_BYTES': value})
        self.assertEqual(ArtifactPolicy.from_env({}), ArtifactPolicy())

    def test_invalid_policy_and_path_identifiers_are_rejected(self):
        for change in ({'max_file': 0}, {'reserve_free': -1}, {'idle_seconds': True}, {'max_total': 1}, {'max_file': 2 ** 64}):
            with self.assertRaises(ValueError):
                ArtifactPolicy(**change)
        for artifact_id in ('../outside', '', 'A' * 24):
            with self.assertRaises(TaskError) as error:
                self.spool.get(artifact_id)
            self.assertEqual(error.exception.status, 404)
