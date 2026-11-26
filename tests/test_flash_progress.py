import unittest

from unittest.mock import patch

from decklab_core.flash_progress import PHASES, PreviewProgress, FlashWriteProgress
from decklab_core.flash_store import FlashStore
from decklab_core.storage import Store
from decklab_core.tasks import TaskStore


class PreviewProgressTests(unittest.TestCase):
    def setUp(self):
        self.registry = Store(':memory:')
        self.addCleanup(self.registry.close)
        self.tasks = TaskStore(self.registry)
        self.store = FlashStore(self.registry)
        self.now = 0

    def progress(self, size=1024**3, endpoint='test-executor'):
        task = self.tasks.create('flash-preview', 'web', '', [{'serial': '', 'connection_id': 'test-preview'}])
        self.store.initialize_details(task, {})
        return task, PreviewProgress(self.store, task, {'size': size}, endpoint, True, lambda: self.now)

    def test_whole_pipeline_has_initial_estimate_and_measured_stage_history(self):
        task, progress = self.progress()
        for phase in PHASES:
            progress.event({'phase': phase})
            self.now += 4
        progress.finish(True)
        saved = self.store.details(task)['preview_progress']
        self.assertEqual(saved['state'], 'completed')
        self.assertTrue(all(step['elapsed_seconds'] == 4 for step in saved['stages']))
        self.tasks.update(task, state='completed')
        next_id, next_progress = self.progress(size=2*1024**3)
        next_progress.event({'phase': PHASES[0], 'done': 0, 'total': 2*1024**3})
        current = self.store.details(next_id)['preview_progress']
        self.assertEqual(current['sample_count'], 1)
        self.assertEqual([row['estimated_seconds'] for row in current['stages']], [8, 8, 8, 8, 4, 4])
        other_id, other = self.progress(endpoint='another-control-node')
        other.event({'phase': PHASES[0]})
        self.assertEqual(self.store.details(other_id)['preview_progress']['sample_count'], 0)

    def test_skipped_stages_and_failure_are_not_marked_complete(self):
        task, progress = self.progress()
        progress.event({'phase': 'checking-upload', 'done': 0, 'total': 100})
        self.now = 2
        progress.event({'phase': 'checking-upload', 'done': 20, 'total': 100})
        self.assertEqual(self.store.details(task)['preview_progress']['stages'][0]['estimated_seconds'], 10)
        self.now = 3
        progress.event({'phase': 'checking-layout'})
        progress.finish(False)
        saved = self.store.details(task)['preview_progress']
        self.assertFalse(saved['active'])
        self.assertEqual(saved['stages'][2]['state'], 'skipped')
        self.assertEqual(saved['stages'][4]['state'], 'failed')
        self.assertEqual(saved['stages'][5]['state'], 'pending')

class FlashWriteProgressTests(unittest.TestCase):
    def test_ec_has_a_durable_step_start_but_does_not_count_as_written_bytes(self):
        progress = FlashWriteProgress({'ec': {'state': 'selected'}, 'writes': [
            dict(storage='active', partition='bootinfo', size=512)]})
        ec = {'event':'phase', 'storage':'EC', 'partition':'EC'}
        with patch('decklab_core.flash_progress.time.time', return_value=20):
            done, total, detail = progress.measure(ec)
        self.assertEqual((done, total, detail['partition'], detail['step_started_ms']), (0, 512, 'EC', 20_000))
        with patch('decklab_core.flash_progress.time.time', return_value=40):
            self.assertEqual(progress.measure(ec)[2]['step_started_ms'], 20_000)
            self.assertEqual(progress.measure({'storage':'active', 'partition':'bootinfo', 'total':512})[2]['step_started_ms'], 40_000)
        skipped = FlashWriteProgress({'ec': {'state': 'not-applicable'}})
        self.assertEqual(skipped.measure(ec), (0, 0, {}))

    def test_combined_progress_tracks_native_partition_offsets_and_speed(self):
        def step(label, writes):
            return {'label': label, 'result': {'plan': {'writes': [
                dict(storage='active', partition=part, size=size) for part, size in writes]}}}
        progress = FlashWriteProgress({'steps': [step('Firmware', [('boot', 10)]),
                                                step('OS', [('ESP', 20), ('rootfs', 100)])]})
        def measure(done, name, component, total):
            return progress.measure(dict(component=component, storage='active', partition=name, done=done, total=total))
        with patch('decklab_core.flash_progress.time.monotonic', side_effect=[0, 1, 2, 4]):
            self.assertEqual(measure(5, 'boot', 'Firmware', 10)[:2], (5, 130))
            self.assertEqual(progress.measure(dict(component='OS', storage='active', partition='ESP', total=120))[:2], (10, 130))
            self.assertNotIn('bytes_per_second', measure(20, 'rootfs', 'OS', 120)[2])
            done, total, part = measure(60, 'rootfs', 'OS', 120)
        self.assertEqual((done, total, part['partition_done'], part['partition_total'], part['bytes_per_second']),
                         (70, 130, 40, 100, 20))
        self.assertEqual(measure(20, 'unknown', 'OS', 120), (20, 120, {}))
