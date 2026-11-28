import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from decklab_core import team_run
from decklab_core.team_cases import TeamCases
from decklab_core.tasks import TaskError


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.platform = TeamCases('https://platform.example.test', 'test-key')

    def test_disallows_external_authority_redirect_input(self):
        for url in ['http://platform.example.test', 'https://x@example.test', 'https://example.test:bad']:
            with self.subTest(url=url), self.assertRaises(TaskError):
                TeamCases(url, 'test-key')

    def test_group_keeps_order_and_rounds(self):
        group = [{'caseGroupId': 5, 'caseId': 12, 'caseName':'Sleep', 'caseCode':'S',
                  'times':3, 'executeOrder':1}]
        with patch.object(self.platform, 'request', return_value=group):
            result = self.platform.browse({'view':['members'], 'id':['5']})
            self.assertEqual(result['members'][0]['times'], 3)
            with self.assertRaises(TaskError):
                self.platform.browse({'view':['members'], 'id':['6']})

    def test_mismatched_steps_rejected(self):
        with patch.object(self.platform, 'request', side_effect=[
                {'id':1, 'caseName':'Smoke', 'caseCode':'S'}, [{'caseId':2}]]):
            with self.assertRaises(TaskError):
                self.platform.browse({'view':['detail'], 'id':['1']})

    def test_board_mapping_must_be_one_to_one(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / 'slots.json'
            p.write_text(json.dumps({'A':{'code':'A','device_id':'edge:0'},
                                     'B':{'code':'B','device_id':'edge:0'}}))
            with patch.dict(os.environ, DECKLAB_TEST_PLATFORM_BOARDS_FILE=str(p)), self.assertRaises(TaskError):
                team_run.mapping('A')


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.manager, self.board = MagicMock(), MagicMock(serial='S', connection_id='G')
        self.manager.tasks.store.find_request.return_value = None
        self.manager.tasks.store.request_digest.return_value = 'a'*64
        self.platform, self.context, self.maintenance = MagicMock(), MagicMock(), MagicMock()
        self.context.id = 'attempt'
        self.context.cancelled.is_set.return_value = False
        self.input = {'source':'titanx', 'serial':'S', 'connection_id':'G', 'case':5,
                      'rounds':1, 'request_id':'test-attempt'}
        self.row = {'code':'TEST', 'deviceId':'edge:0', 'serial':'usb:2-1', 'state':'idle', 'project':'K3'}
        self.platform.browse.return_value = {'case': {'id':5, 'name':'Smoke', 'automated':True, 'reviewed':True, 'review_status':2, 'runnable':True}}
        self.platform.request.side_effect = self.call
        self.started = 0
        self.bad_record = False
        self.manager.tasks.start_board.side_effect = lambda kind,actor,client,serial,run,**kw: run(self.context)
        for mocked in (
            patch.object(team_run, 'read_config', return_value={}),
            patch.object(TeamCases,'configured',return_value=self.platform),
            patch.object(team_run,'maintenance_for',return_value=self.maintenance),
            patch.object(team_run,'mapping',return_value={'code':'TEST','device_id':'edge:0'}),
            patch.object(team_run,'adb_binding',return_value={'serial':'S','connection_id':'G','usb_port':'2-1'}),
            patch.object(team_run,'FlashStore')):
            mocked.start()
            self.addCleanup(mocked.stop)

    def call(self, path, body=None):
        if path == '/task/record/job/15_999':
            return [self.failed_round]
        if path == '/taskInfo/15':
            return {'id': 15, 'status': self.original_status}
        if path == '/taskInfo/taskCase/list/15':
            return [{'id': 41, 'caseId': 5}]
        if path == '/deviceBoard/listVO':
            return {'list':[self.row], 'total':1}
        if path == '/userInfo/detail':
            return {'id':1}
        if path == '/taskInfo/saveOrUpdate':
            self.assertEqual(body['taskDevices'], [{'deviceId':'TEST'}])
            self.info = dict(body['taskInfo'], id=23, status=0)
            return {'id':23}
        if path == '/taskInfo/taskDevice/list/23':
            return [{'deviceId':'TEST'}]
        if path == '/taskInfo/taskCase/list/23':
            return [{'id':42,'caseId':5,'times':1}]
        if path == '/taskInfo/23':
            return self.info
        if path == '/taskInfo/start?taskId=23':
            self.started += 1
            self.info['status'] = 2
            return None
        if path == '/taskInfo/listJob/page':
            return {'total':1,'list':[{'taskId':23,'deviceId':'TEST','id':'job-1'}]}
        if path == '/task/record/job/job-1':
            return [{'jobId':'other' if self.bad_record else 'job-1','taskCaseId':42,'index':1,'status':1}]
        self.fail(path)

    def submit(self):
        return team_run.start(self.manager, self.board, self.input, 'web')

    def test_shared_agent_can_dispatch_and_is_recorded_as_task_owner(self):
        actor = 'shared-agent/0123456789abcdef'
        result = team_run.start(self.manager, self.board, self.input, actor)
        self.assertEqual(result['completed_rounds'], 1)
        self.assertEqual(self.manager.tasks.start_board.call_args.args[1], actor)
        with self.assertRaises(TaskError):
            team_run.start(self.manager, self.board, self.input, 'viewer')

    def test_selected_case_receives_its_own_board_round(self):
        self.assertEqual(self.submit()['completed_rounds'], 1)
        self.assertEqual(self.started, 1)
        self.maintenance.release.assert_called_once()

    def test_automation_and_review_status_at_plan_submission(self):
        for automated, completed, status, allowed in [
                (True, True, 0, True), (True, True, 1, True), (True, True, 2, True),
                (True, True, 3, False), (True, True, None, False),
                (False, True, 0, False), (True, False, 0, False)]:
            with self.subTest(automated=automated, completed=completed, status=status):
                api = TeamCases('https://platform.example.test', 'test-key')
                row = {'id':5, 'caseName':'Smoke', 'caseCode':'S', 'isAutomated':automated,
                       'isAutomationCompleted':completed, 'reviewStatus':status}
                with patch.object(api, 'request', side_effect=[row, []]):
                    detail = api.browse({'view':['detail'], 'id':['5']})
                self.platform.browse.return_value = detail
                before = self.started
                if allowed:
                    self.assertEqual(self.submit()['completed_rounds'], 1)
                else:
                    with self.assertRaises(TaskError):
                        self.submit()
                self.assertEqual(self.started - before, int(allowed))

    def test_wrong_usb_path_does_not_create_plan(self):
        self.row['serial'] = 'usb:2-2'
        with self.assertRaises(TaskError):
            self.submit()
        self.assertFalse(hasattr(self, 'info'))
        self.assertEqual(self.started, 0)

    def test_idle_board_with_dispatch_disabled_does_not_create_plan(self):
        self.row['noDispatch'] = True
        with self.assertRaisesRegex(TaskError, '禁止调度'):
            self.submit()
        self.manager.tasks.start_board.assert_not_called()
        self.assertFalse(hasattr(self, 'info'))
        self.assertEqual(self.started, 0)

    def test_ambiguous_start_retains_original_job(self):
        def lost(path, body=None):
            if path.startswith('/taskInfo/start?'):
                self.started += 1
                raise TaskError('lost response', 502)
            return self.call(path, body)
        self.platform.request.side_effect = lost
        with self.assertRaises(TaskError):
            self.submit()
        self.assertEqual(self.started, 1)
        self.maintenance.keep_recovery.assert_called_once_with('attempt')
        self.maintenance.release.assert_not_called()

    def test_different_job_cannot_report_success(self):
        self.bad_record = True
        with self.assertRaises(TaskError):
            self.submit()

    def test_decklab_board_tracks_plan_without_claiming_execution(self):
        self.row.update(decklabSerial='S', decklabSlot=0)
        self.assertEqual(self.submit()['completed_rounds'], 1)
        self.assertEqual(self.manager.tasks.start_board.call_args.args[0], 'team-plan')

    def test_registered_native_slot_runs_pinned_plan_and_tracks_edge_lease(self):
        station = {'api_url': 'https://example.test', 'lab_id': 3, 'node_id': 'edge', 'slots': {'1': 'S'}}
        self.platform.host, self.platform.port = 'example.test', 443
        self.row.update(nodeId=9, deviceId='edge:1', remoteComserver=100, remoteCom=1, noDispatch=True)
        def native(path, body=None):
            if path == '/deviceNode/listVO':
                self.assertEqual(body, {'namespaceId': 3})
                return [{'id': 9, 'originNodeId': 'edge', 'deviceBoards': [self.row]}]
            if path == '/deviceBoard/listVO':
                self.assertEqual(body['namespaceId'], 3)
            return self.call(path, body)
        self.platform.request.side_effect = native
        with patch.object(team_run, 'read_config', return_value=station):
            self.assertEqual(self.submit()['completed_rounds'], 1)
            self.assertEqual(self.manager.tasks.start_board.call_args.args[0], 'team-plan')
            self.assertEqual(self.started, 1)
            self.row['nodeId'] = 8
            self.assertIsNone(team_run.board_mapping(self.platform, 'S'))

    def test_archived_board_requires_exact_serial_and_slot(self):
        self.row.update(decklabSerial='S', decklabSlot=0)
        self.assertEqual(team_run.archived_mapping(self.platform, 'S')['device_id'], 'edge:0')
        self.assertIsNone(team_run.archived_mapping(self.platform, 'OTHER'))
        self.row['decklabSlot'] = 1
        self.assertIsNone(team_run.archived_mapping(self.platform, 'S'))

    def test_accepted_start_with_lost_ack_reads_original_plan(self):
        def lost(path, body=None):
            result = self.call(path, body)
            if path.startswith('/taskInfo/start?'):
                raise TaskError('lost response', 502)
            return result
        self.platform.request.side_effect = lost
        self.assertEqual(self.submit()['completed_rounds'], 1)
        self.assertEqual(self.started, 1)
        self.maintenance.release.assert_called_once()

    def failure_link(self):
        self.original_status = 2
        self.failed_round = {'id': 801, 'jobId': '15_999', 'taskId': 15, 'taskCaseId': 41,
                             'index': 3, 'status': -1, 'decklabUrl':
                             'https://decklab.example.test/#device=S&plan=15&job=15_999&record=801'}
        self.input['retry'] = {'plan_id': '15', 'job_id': '15_999', 'record_id': '801'}

    def test_retry_links_new_plan_to_the_verified_failed_round(self):
        self.failure_link()
        result = self.submit()
        self.assertEqual(result['retry_of'], {**self.input['retry'], 'round': 3, 'case_id': 5})
        self.assertEqual(result['platform_task_id'], 23)
        self.assertIn('失败记录 801', self.info['remark'])
        self.assertEqual(self.started, 1)

    def test_retry_for_a_different_board_or_case_never_starts_a_plan(self):
        self.failure_link()
        self.failed_round['decklabUrl'] = 'https://decklab.example.test/#device=OTHER'
        with self.assertRaisesRegex(TaskError, '不符'):
            self.submit()
        self.failure_link()
        self.input['case'] = 7
        with self.assertRaisesRegex(TaskError, '不一致'):
            self.submit()
        self.assertFalse(hasattr(self, 'info'))
        self.assertEqual(self.started, 0)

    def test_retry_is_rejected_while_the_original_plan_is_active(self):
        self.failure_link()
        self.original_status = 1
        with self.assertRaisesRegex(TaskError, '计划结束'):
            self.submit()
        self.assertEqual(self.started, 0)

    def test_existing_request_observes_original_task(self):
        self.manager.tasks.store.find_request.return_value = 'previous'
        self.submit()
        self.manager.tasks.store.get.assert_called_once_with('previous')
        self.platform.request.assert_not_called()


class RoundEvidenceTests(unittest.TestCase):
    def test_recorded_round_reads_only_matching_logs_and_board(self):
        store, platform = MagicMock(), MagicMock()
        store.get.return_value = {'kind': 'team-plan', 'actor':'runner', 'board_targets': ['S'], 'created': 1, 'updated': 2,
            'result': {'source': 'titanx', 'case_code': 'APT', 'recent_rounds': [
                {'job_id': 'job', 'round': 1, 'task_case_id': 7, 'status': 'passed'}]}}
        store.titanx_case_receipts.return_value = []
        device = MagicMock(serial='S')
        platform.request.return_value = [{'jobId': 'job', 'caseOnlyId': '7', 'round': 1, 'log': 'ok'}]
        evidence = team_run.round_evidence(store, device, platform, 'task', 1)
        self.assertEqual(evidence['logs'], ['ok'])
        with self.assertRaises(TaskError):
            team_run.round_evidence(store, device, platform, 'task', 1, 'unrelated-agent')
        platform.request.return_value[0]['round'] = 2
        self.assertNotIn('logs', team_run.round_evidence(store, device, platform, 'task', 1))
        with self.assertRaises(TaskError):
            team_run.round_evidence(store, MagicMock(serial='other'), platform, 'task', 1)
