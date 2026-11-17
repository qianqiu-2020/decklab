import unittest

from seriald_core.flash_progress import PHASES, PreviewProgress
from seriald_core.flash_store import FlashStore
from seriald_core.storage import Store
from seriald_core.tasks import TaskStore


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
