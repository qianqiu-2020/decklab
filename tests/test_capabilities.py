"""Public capability discovery is read-only and does not overstate availability."""
import io
import json
import os
import unittest
from unittest import mock

from seriald_core.actions import capabilities
from seriald_core.api_contract import API_VERSION, CONTRACT_NAME, CONTRACT_REVISION, OUTCOMES
from seriald_core.hardware import ACTIONS
from seriald_core.task_actions import TaskActions
from seriald_core import web


class CapabilitiesTests(unittest.TestCase):
    def test_http_discovery_works_without_a_collector_manager(self):
        handler = web.Handler.__new__(web.Handler)
        handler.path = '/api/capabilities'
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        with mock.patch.object(web, 'MGR', None):
            handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        document = json.loads(handler.wfile.getvalue())
        self.assertEqual(document['api_version'], API_VERSION)
        self.assertEqual(document['contract']['name'], CONTRACT_NAME)
        self.assertEqual(document['contract']['revision'], CONTRACT_REVISION)
        self.assertEqual(document, capabilities().payload)

    def test_task_submission_and_produced_kinds_are_distinct(self):
        tasks = capabilities().payload['tasks']
        self.assertEqual(set(tasks['kinds']), TaskActions.kinds)
        self.assertEqual(tasks['submission']['/api/task-start'], tasks['kinds'])
        self.assertIn('nor-backup', tasks['kinds'])
        self.assertNotIn('nor-backup', tasks['produced_kinds'])
        self.assertEqual(tasks['kind_aliases']['nor-backup'], 'flash')
        for kind in ('desktop-input', 'image-download'):
            self.assertIn(kind, tasks['produced_kinds'])
            self.assertNotIn(kind, tasks['kinds'])
        self.assertEqual(tasks['submission']['/api/desktop/input'], ['desktop-input'])
        self.assertEqual(tasks['submission']['/api/image-download'], ['image-download'])
        self.assertIn('flash-inspect', tasks['produced_kinds'])
        self.assertEqual(len(tasks['kinds']), len(set(tasks['kinds'])))

    def test_protocol_support_does_not_grant_permissions_or_board_capabilities(self):
        document = capabilities().payload
        self.assertEqual(document['scope']['discovery'], 'protocol')
        self.assertEqual(document['scope']['authorization'], 'not-a-grant')
        self.assertEqual(document['scope']['availability'], 'per-target')
        for field in ('cross_platform_exclusive', 'fleet_scheduling', 'high_availability'):
            self.assertIs(document['scope'][field], False)
        lease = document['execution_lease']
        self.assertIs(lease['supported'], True)
        self.assertEqual((lease['scope'], lease['resource']), ('collector', 'board'))
        self.assertIs(lease['cross_platform_exclusive'], False)
        self.assertEqual(lease['generation_type'], 'integer')
        self.assertEqual(lease['ttl_seconds'], {'min': 30, 'max': 3600})
        self.assertEqual(document['hardware']['actions'], list(ACTIONS))
        self.assertEqual(document['hardware']['default'], 'disabled')
        self.assertEqual(document['hardware']['availability_endpoint'], '/api/hardware')
        self.assertEqual(document['desktop']['availability_endpoint'], '/api/desktop/sessions')
        self.assertIs(document['desktop']['requires_configuration'], True)
        self.assertIs(document['desktop']['continuous_browser_stream'], True)
        self.assertEqual(document['desktop']['stream_endpoint'], '/api/desktop/stream')
        self.assertEqual(document['desktop']['stream_providers'], ['vnc', 'rdp'])
        self.assertIs(document['desktop']['server_credentials_only'], True)
        flashing = document['flashing']
        self.assertEqual(flashing['availability_endpoint'], '/api/flash-capabilities')
        self.assertEqual(flashing['readback'], 'per-target')
        self.assertEqual(flashing['nor_backup']['availability_field'], 'nor_workspace_supported')
        self.assertEqual(flashing['nor_backup']['task_kind'], 'flash')
        self.assertIs(flashing['nor_backup']['requires_supported_k3_usb_target'], True)

    def test_dedup_and_outcomes_never_promise_replay_or_automatic_verification(self):
        tasks = capabilities().payload['tasks']
        dedup = tasks['request_dedup']
        self.assertEqual(dedup['scope'], ['actor', 'request_id'])
        self.assertEqual(dedup['same_input'], 'return-existing-task')
        self.assertEqual(dedup['changed_input_http_status'], 409)
        self.assertTrue(set(tasks['kinds']).issubset(dedup['kinds']))
        self.assertEqual(set(dedup['required_kinds']),
                         {'flash', 'nor-backup', 'desktop-input', 'image-download', 'board-test', 'adb-command'})
        self.assertEqual(tasks['request_lookup'], {
            'supported': True, 'endpoint': '/api/task', 'parameter': 'request_id', 'scope': 'actor'})
        self.assertEqual(tasks['outcome']['values'], list(OUTCOMES))
        self.assertIs(tasks['outcome']['independent_of_state'], True)
        self.assertIs(tasks['outcome']['completed_is_verified'], False)
        self.assertIs(tasks['automatic_replay'], False)
        self.assertIs(tasks['cancel']['request_is_stop_confirmation'], False)
        self.assertIs(tasks['cancel']['rollback'], False)

    def test_each_response_is_independent_and_contains_no_host_configuration(self):
        with mock.patch.dict(os.environ, {'SERIALD_TOKEN': 'private-token-fixture',
                                         'SERIALD_DESKTOP': 'private-desktop-fixture'}):
            first = capabilities().payload
        first['tasks']['kinds'].append('unsupported-task')
        first['tasks']['outcome']['values'].clear()
        first['flashing']['nor_backup']['task_kind'] = 'changed'
        second = capabilities().payload
        self.assertNotIn('unsupported-task', second['tasks']['kinds'])
        self.assertEqual(second['tasks']['outcome']['values'], list(OUTCOMES))
        self.assertEqual(second['flashing']['nor_backup']['task_kind'], 'flash')
        encoded = json.dumps(second, allow_nan=False)
        self.assertNotIn('private-token-fixture', encoded)
        self.assertNotIn('private-desktop-fixture', encoded)


if __name__ == '__main__':
    unittest.main()
