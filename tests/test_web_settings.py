"""Web settings use temporary storage and never start a board collector."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from seriald_core.web_settings import WebSettings, SettingsError, REDACTED, DEFAULTS, load_environment
from seriald_core.control_server import ControlHandler
from seriald_core.agent_grants import AgentGrantStore


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'web-settings.json'
        self.settings = WebSettings(self.path, {'SERIALD_BASE': self.folder.name})

    def patch(self, **environment):
        return {'version': 1, 'environment': environment}

    def save(self, document, preview=False):
        return self.settings.save(document, self.settings.snapshot()['revision'], preview)

    def test_redacted_round_trip_preserves_all_secrets_and_changes_other_values(self):
        self.save({'version': 1, 'environment': {'AUTH_TOKEN': 'owner-password'}, 'documents': {
            'agent_tokens': {'codex': 'x' * 32}, 'profiles': {'profiles': {'test': {'ssh_password': 'private'}}}}})
        public = self.settings.snapshot()
        self.assertEqual(public['configuration']['environment']['AUTH_TOKEN'], REDACTED)
        self.assertEqual(public['configuration']['documents']['agent_tokens'], {'codex': REDACTED})
        self.assertNotIn('owner-password', json.dumps(public))
        self.assertNotIn('private', json.dumps(public))
        public['configuration']['environment']['PORT'] = '9000'
        self.save(public['configuration'])
        private = self.settings.snapshot(True)['configuration']
        self.assertEqual(private['environment']['AUTH_TOKEN'], 'owner-password')
        self.assertEqual(private['documents']['agent_tokens']['codex'], 'x' * 32)
        self.assertEqual(private['documents']['profiles']['profiles']['test']['ssh_password'], 'private')
        self.assertEqual(private['environment']['PORT'], '9000')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_patch_and_explicit_empty_clear_are_distinct(self):
        self.save({'version': 1, 'environment': {'PORT': '9000'}, 'documents': {'profiles': {'profiles': {'one': {}}}}})
        self.save(self.patch(BAUD='9600'))
        private = self.settings.effective()
        self.assertEqual(private['environment']['PORT'], '9000')
        self.assertIn('one', private['documents']['profiles']['profiles'])
        self.save({'version': 1, 'documents': {'profiles': {}}})
        self.assertEqual(self.settings.effective()['documents']['profiles'], {})

    def test_invalid_input_and_preview_leave_original_file_unchanged(self):
        self.save(self.patch(PORT='9000'))
        before = self.path.read_bytes()
        for document in [self.patch(PORT='0'), self.patch(BAUD='49'), self.patch(BASE='relative'),
                         self.patch(STOP_BITS='3'), self.patch(MCP_ENABLED='no'),
                         self.patch(PROBE='custom', PROBE_COMMAND='echo x'),
                         self.patch(SSH_USER='-bad'), self.patch(LOG_RETENTION_DAYS='36501'),
                         self.patch(TRANSFER_MAX_FILES='0'), self.patch(UNKNOWN='x'),
                         {'version': 1, 'documents': {'hardware': {'boards': {'test': {'unknown': {}}}}}},
                         {'version': 1, 'documents': {'flash': {'boards': {'test': {}}}}},
                         {'version': 1, 'documents': {'profiles': {'default': 'missing'}}}]:
            with self.subTest(document=document), self.assertRaises((ValueError, SettingsError)):
                self.save(document)
            self.assertEqual(self.path.read_bytes(), before)
        result = self.save(self.patch(PORT='9001'), preview=True)
        self.assertEqual(result['changes'], ['PORT'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_proposed_environment_validates_profiles_without_mutating_process(self):
        with mock.patch.dict(os.environ, {'SERIALD_PROBE': 'linux', 'SERIALD_BAUD': '115200'}):
            with self.assertRaises(ValueError):
                self.save({'version': 1, 'environment': {'PROBE': 'manual', 'AUTO_CONFIGURE_SSH': '0'},
                           'documents': {'profiles': {'profiles': {'automatic': {'auto_configure_ssh': True}}}}})
            self.save(self.patch(PROBE='manual', AUTO_CONFIGURE_SSH='0'))
            self.assertEqual(os.environ['SERIALD_PROBE'], 'linux')
            self.assertEqual(os.environ['SERIALD_BAUD'], '115200')

    def test_stale_revision_cannot_overwrite_newer_configuration(self):
        revision = self.settings.snapshot()['revision']
        self.save(self.patch(PORT='9000'))
        with self.assertRaises(SettingsError) as context:
            self.settings.save(self.patch(PORT='9001'), revision)
        self.assertEqual(context.exception.status, 409)
        self.assertEqual(self.settings.effective()['environment']['PORT'], '9000')

    def test_external_document_changes_invalidate_preview_revision(self):
        profile = Path(self.folder.name) / 'profiles.json'
        profile.write_text('{}')
        settings = WebSettings(self.path, {'SERIALD_BASE': self.folder.name, 'SERIALD_PROFILES': str(profile)})
        revision = settings.snapshot()['revision']
        profile.write_text('{"profiles":{"new":{}}}')
        with self.assertRaises(SettingsError) as context:
            settings.save(self.patch(PORT='9000'), revision)
        self.assertEqual(context.exception.status, 409)

    def test_parallel_processes_cannot_both_commit_the_same_revision(self):
        revision = self.settings.snapshot()['revision']
        source = '''import sys,time
from seriald_core.web_settings import WebSettings,SettingsError
class DelayedSettings(WebSettings):
    def validate(self, document):
        time.sleep(0.2)
        return super().validate(document)
settings=DelayedSettings(sys.argv[1], {'SERIALD_BASE':sys.argv[2]})
sys.stdin.read(1)
try:
    settings.save({'version':1,'environment':{'PORT':sys.argv[4]}},sys.argv[3])
    print(200)
except SettingsError as error:
    print(error.status)
'''
        processes = [subprocess.Popen([sys.executable, '-c', source, str(self.path), self.folder.name, revision, port],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for port in ('9011', '9012')]
        try:
            for process in processes:
                process.stdin.write('x')
                process.stdin.flush()
            results = [process.communicate(timeout=5) for process in processes]
            self.assertEqual(sorted(int(result[0]) for result in results), [200, 409])
            self.assertTrue(all(process.returncode == 0 for process in processes))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_saved_documents_override_external_paths_at_startup(self):
        self.save({'version': 1, 'documents': {'profiles': {'profiles': {'demo': {'ssh_password': 'keep-private'}}}}})
        with mock.patch.dict(os.environ, {}, clear=True):
            load_environment(self.path)
            target = Path(os.environ['SERIALD_PROFILES'])
            self.assertEqual(json.loads(target.read_bytes())['profiles']['demo']['ssh_password'], 'keep-private')
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_duplicate_keys_and_corrupt_record_fail_closed(self):
        for value in ('{"version":1,"version":1}', '{"version":2}', '{"version":true}', '{"version":1,"environment":{"UNKNOWN":"x"}}', '{"version":1,"environment":{"AUTH_TOKEN":null}}'):
            self.path.write_text(value)
            with mock.patch.dict(os.environ, {'SERIALD_AUTH_TOKEN': 'existing'}):
                with self.assertRaises(ValueError):
                    load_environment(self.path)
                self.assertEqual(os.environ['SERIALD_AUTH_TOKEN'], 'existing')

    def test_new_redacted_secret_has_to_be_supplied(self):
        with self.assertRaises(SettingsError):
            self.save({'version': 1, 'documents': {'profiles': {'profiles': {'new': {'ssh_password': REDACTED}}}}})

    def test_startup_overlay_applies_before_config_constants(self):
        self.save(self.patch(PORT='9123', BASE=str(Path(self.folder.name) / 'new-data'), BAUD='9600'))
        environment = dict(os.environ, SERIALD_BASE=self.folder.name)
        source = ('from seriald_core.config import BASE, SETTINGS_PATH; '
                  'from seriald_core.profiles import SerialSettings; import os,json; '
                  'print(json.dumps([BASE,SETTINGS_PATH,os.environ["SERIALD_PORT"],SerialSettings.from_env().baud]))')
        result = subprocess.run([sys.executable, '-c', source], env=environment, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), [str(Path(self.folder.name) / 'new-data'), str(self.path), '9123', 9600])


class SettingsHTTPTests(unittest.TestCase):
    setUp = SettingsTests.setUp
    patch = SettingsTests.patch
    def handler(self, action=None, authorization='Bearer owner', content_type='application/json'):
        handler = ControlHandler.__new__(ControlHandler)
        handler.settings, handler.auth_token = self.settings, 'owner'
        handler.grant_store = AgentGrantStore(':memory:')
        self.addCleanup(handler.grant_store.close)
        handler.client_address = ('192.0.2.5', 12345)
        handler.server = SimpleNamespace(settings_revision='active')
        handler.path = '/api/settings'
        body = json.dumps(action).encode() if action is not None else b''
        handler.headers = {'Authorization': authorization, 'Content-Type': content_type, 'Content-Length': str(len(body))}
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        return handler

    def result(self, handler):
        return handler.send_response.call_args.args[0], json.loads(handler.wfile.getvalue())

    def test_owner_can_preview_save_export_and_see_restart_state(self):
        handler = self.handler()
        handler.do_GET()
        code, data = self.result(handler)
        self.assertEqual(code, 200)
        self.assertEqual(data['active_revision'], 'active')
        for action in ('preview', 'save'):
            handler = self.handler({'action': action, 'revision': data['revision'], 'configuration': self.patch(PORT='9012')})
            handler.do_POST()
            self.assertEqual(self.result(handler)[0], 200)
        handler = self.handler({'action': 'export', 'include_secrets': True})
        handler.do_POST()
        self.assertEqual(self.result(handler)[1]['configuration']['environment']['PORT'], '9012')

    def test_agents_and_share_collaborators_cannot_read_or_write_settings(self):
        handler = self.handler()
        token, _ = handler.grant_store.issue('all')
        share = handler._new_share()
        for credential in ('Bearer ' + token, 'Bearer ' + share):
            for write in (False, True):
                candidate = self.handler({'action': 'export', 'include_secrets': True}, authorization=credential)
                candidate.grant_store = handler.grant_store
                candidate.do_POST() if write else candidate.do_GET()
                self.assertIn(self.result(candidate)[0], (401, 403))
                self.assertNotIn(b'bianbu', candidate.wfile.getvalue())

    def test_non_json_write_and_oversized_body_are_rejected(self):
        handler = self.handler({'action': 'export'}, content_type='text/plain')
        handler.do_POST()
        self.assertEqual(self.result(handler)[0], 400)
        handler = self.handler({'action': 'export'})
        handler.headers['Content-Length'] = str(2 * 1024 ** 2 + 1)
        handler.do_POST()
        self.assertEqual(self.result(handler)[0], 400)
