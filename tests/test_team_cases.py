import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from seriald_core import team_run
from seriald_core.team_cases import TeamCases
from seriald_core.tasks import TaskError


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
            with patch.dict(os.environ, SERIALD_TEST_PLATFORM_BOARDS_FILE=str(p)), self.assertRaises(TaskError):
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
        self.platform.browse.return_value = {'case': {'id':5, 'name':'Smoke', 'automated':True, 'reviewed':True}}
        self.platform.request.side_effect = self.call
        self.started = 0
        self.bad_record = False
        self.manager.tasks.start_board.side_effect = lambda kind,actor,client,serial,run,**kw: run(self.context)
        for mocked in (
            patch.object(TeamCases,'configured',return_value=self.platform),
            patch.object(team_run,'maintenance_for',return_value=self.maintenance),
            patch.object(team_run,'mapping',return_value={'code':'TEST','device_id':'edge:0'}),
            patch.object(team_run,'adb_binding',return_value={'serial':'S','connection_id':'G','usb_port':'2-1'}),
            patch.object(team_run,'FlashStore')):
            mocked.start()
            self.addCleanup(mocked.stop)

    def call(self, path, body=None):
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

    def test_selected_case_receives_its_own_board_round(self):
        self.assertEqual(self.submit()['completed_rounds'], 1)
        self.assertEqual(self.started, 1)
        self.maintenance.release.assert_called_once()

    def test_wrong_usb_path_does_not_create_plan(self):
        self.row['serial'] = 'usb:2-2'
        with self.assertRaises(TaskError):
            self.submit()
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

    def test_existing_request_observes_original_task(self):
        self.manager.tasks.store.find_request.return_value = 'previous'
        self.submit()
        self.manager.tasks.store.get.assert_called_once_with('previous')
        self.platform.request.assert_not_called()
