from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from seriald_core.ramboot_tasks import K3_PICO_MODEL, _run, start
from seriald_core.tasks import TaskError


class RAMBootTaskTests(unittest.TestCase):
    def test_ram_boot_is_owner_only(self):
        actions = SimpleNamespace(manager=Mock())
        result = start(actions, {}, 'agent:external')
        self.assertEqual(result.status, 403)
        actions.manager.assert_not_called()

    def test_sealed_source_stays_claimed_if_helper_stop_is_unconfirmed(self):
        with tempfile.TemporaryDirectory() as root:
            manager = Mock()
            actions = SimpleNamespace(manager=manager, store=Mock())
            executor = Mock(root=Path(root))
            executor.image_guard.return_value = nullcontext()
            executor._directory.return_value = Path(root)
            executor.ram_boot.return_value = {'model': K3_PICO_MODEL, 'serial': 'verified', 'ram_bytes': 8 << 30}
            serial = 'verified'
            binding = SimpleNamespace(target_id='target', target_generation='generation',
                                      profile_data={}, device={'usb_address': 2})
            discovery = Mock()
            discovery.get.return_value = {'document': {'device': binding.device}}
            context = Mock(id='task')
            maintenance = Mock()
            row = {'id': 'sealed-source', 'sha256': '1' * 64}
            manager.artifacts.open.return_value = nullcontext()
            manager.store.has_conflict.return_value = False
            error = TaskError('helper is still running')
            error.cleanup_confirmed = False
            with patch('seriald_core.ramboot_tasks._discovery', return_value=discovery),                  patch('seriald_core.board_ownership.ownership_for'),                  patch('seriald_core.ramboot_tasks._prepare', side_effect=error):
                with self.assertRaisesRegex(TaskError, 'helper is still running'):
                    _run(actions, executor, binding, serial, row, context, 'attempt', maintenance)
            manager.artifacts.claim.assert_called_once_with(row['id'], context.id, 'web')
            manager.artifacts.open.assert_called_once_with(row['id'], 'web', task_id=context.id)
            manager.artifacts.finish_claim.assert_not_called()
            actions.store.update_details.assert_called_with(context.id, {'recovery_required': True})
            maintenance.release.assert_called_once_with(context.id, confirmed=False)
            executor.cleanup_plan.assert_not_called()
