"""Graphical HTTP/MCP isolation with generated PNGs; never connects to a board."""
import asyncio
import base64
import json
import os
from pathlib import Path
import struct
import sys
import time
import unittest
from unittest import mock
import zlib

from seriald_core.desktop import DesktopBook, DesktopSessions, check_reservation, reservation_activity
from seriald_core.tasks import TaskError
from seriald_mcp import Adapter, Settings, MCPServer
from serialctl import ClientError
from seriald_core.device import Device
from seriald_core.agent_grants import attach_agent_grants
from seriald_core.transactions import transactions_for
import test_flash_discovery_integration as fixture


def png(width=20, height=12):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) + chunk(
        b'IDAT', zlib.compress((b'\x00' + b'\x00\xff\x00' * width) * height)) + chunk(b'IEND', b'')


class Screen:
    def __init__(self):
        self.width, self.height, self.generation = 20, 12, 'display-one'
        self.inputs, self.closed, self.fail = [], False, False

    def screenshot(self):
        return {'png_base64': base64.b64encode(png(self.width, self.height)).decode(), 'generation': self.generation}

    def input(self, request):
        self.inputs.append(request)
        if self.fail:
            raise TimeoutError('input may have been delivered')
        return {'ok': True}

    def close(self):
        self.closed = True


def setup(f):
    attach_agent_grants(f.manager, f.grants)
    config = {'provider': 'helper', 'argv': [sys.executable], 'allow_agent': True,
              'display': 'monitor-A', 'display_kind': 'virtual', 'scale': 1}
    screen = Screen()
    f.manager.desktop = DesktopSessions(f.manager, DesktopBook({'boards': {'GRAPHICS-SN': config}}),
                                       provider_factory=lambda _: screen)
    token, grant = f.grants.issue('device', serial='GRAPHICS-SN')
    return screen, Adapter(Settings(f.url, token)), grant


class DesktopTests(unittest.TestCase):
    request = fixture.FlashDiscoveryHTTPIntegrationTests.request
    terminal = fixture.FlashDiscoveryHTTPIntegrationTests.terminal

    def test_rdp_text_mode_is_explicit_and_validated(self):
        config = {'provider': 'rdp', 'secret_url': 'rdp+plain://example',
                  'display': 'monitor', 'display_kind': 'virtual', 'text_input': 'clipboard'}
        DesktopBook({'boards': {'SN': config}})
        for value in ('shell', None, []):
            with self.assertRaises(ValueError):
                DesktopBook({'boards': {'SN': dict(config, text_input=value)}})
        with self.assertRaises(ValueError):
            DesktopBook({'boards': {'SN': dict(config, provider='vnc')}})

    def test_freerdp_engine_and_certificate_pin_are_explicit(self):
        config = {'provider': 'rdp', 'secret_url': 'rdp+ntlm-password://example',
                  'display': 'monitor', 'display_kind': 'virtual'}
        DesktopBook({'boards': {'SN': config}})
        DesktopBook({'boards': {'SN': dict(config, engine='freerdp', certificate_sha256='A1' * 32)}})
        for changed in ({'engine': 'other'}, {'engine': None}, {'engine': 'freerdp', 'provider': 'vnc'},
                {'certificate_sha256': 'a' * 64}, {'engine': 'aardwolf', 'certificate_sha256': 'a' * 64},
                {'engine': 'freerdp', 'certificate_sha256': 'a' * 63},
                {'engine': 'freerdp', 'certificate_sha256': None}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                DesktopBook({'boards': {'SN': dict(config, **changed)}})

    def test_uart_attachment_changes_require_observation_and_reclaim_current_device(self):
        with fixture.automatic_flashing([('GRAPHICS-SN', 'graphics')]) as f:
            screen, adapter, _ = setup(f)
            sid = adapter.desktop('GRAPHICS-SN', 'open')['session']['id']
            frame = adapter.desktop('GRAPHICS-SN', 'frame', sid)['frame']
            result = adapter.desktop_input('GRAPHICS-SN', sid, frame['id'], 'first-attachment-input',
                'click', {'x': 1, 'y': 1, 'button': 'left'}, screenshot=False)
            self.assertEqual(result['task']['state'], 'completed')
            desktop = f.manager.desktop
            session = desktop.sessions[sid]
            previous = session['ownership'][0]
            for index in range(2):
                current = Device('fake-uart-' + str(index), '', f.manager, board=previous.board)
                def send(frame_id, request_id):
                    task = desktop.input(current, dict(sid=sid, frame_id=frame_id, request_id=request_id,
                        action='click', x=1, y=1, button='left'), session['actor'], '')['task']
                    end = time.monotonic() + 3
                    while task['state'] not in ('failed', 'completed') and time.monotonic() < end:
                        time.sleep(.01)
                        task = f.manager.tasks.store.get(task['id'])
                    return task
                self.assertEqual(send(frame['id'], 'stale-attachment-' + str(index))['state'], 'failed')
                frame = desktop.frame(current, sid, session['actor'])['frame']
                competitor = transactions_for(current)
                owner = competitor.claim('another-operator')
                self.assertEqual(send(frame['id'], 'occupied-attachment-' + str(index))['state'], 'failed')
                self.assertEqual(competitor.ownership()['id'], owner['id'])
                competitor.release(owner['id'], 'another-operator')
                self.assertEqual(send(frame['id'], 'fresh-attachment-' + str(index))['state'], 'completed')
                self.assertIsNone(transactions_for(previous).ownership())
                self.assertEqual(competitor.ownership()['actor'], session['actor'])
                previous = current
            desktop.close_session(session)
            self.assertIsNone(transactions_for(previous).ownership())
            self.assertTrue(screen.closed)

    def test_refresh_requires_explicit_human_disconnect_of_original_session(self):
        with fixture.automatic_flashing([('GRAPHICS-SN', 'graphics')]) as f:
            setup(f)
            code, opened = fixture.api_request(f.url, '/api/desktop/open', f.token,
                {'key': 'graphics', 'serial': 'GRAPHICS-SN', 'client_id': 'browser-a'})
            self.assertEqual(code, 200)
            sid = opened['session']['id']
            code, _ = fixture.api_request(f.url, '/api/desktop/open', f.token,
                {'key': 'graphics', 'serial': 'GRAPHICS-SN', 'client_id': 'browser-b'})
            self.assertEqual(code, 423)
            for explicit, expected in ((False, 403), (True, 200)):
                code, _ = fixture.api_request(f.url, '/api/desktop/close', f.token,
                    {'key': 'graphics', 'serial': 'GRAPHICS-SN', 'client_id': 'browser-b', 'sid': sid, 'takeover': explicit})
                self.assertEqual(code, expected)
            code, opened = fixture.api_request(f.url, '/api/desktop/open', f.token,
                {'key': 'graphics', 'serial': 'GRAPHICS-SN', 'client_id': 'browser-b'})
            self.assertEqual(code, 200)
            self.assertNotEqual(opened['session']['id'], sid)

    def test_tasks_observations_unknown_input_and_modes_are_not_replayed(self):
        with fixture.automatic_flashing([('GRAPHICS-SN', 'graphics')]) as f:
            screen, adapter, grant = setup(f)
            self.assertTrue(adapter.desktop('GRAPHICS-SN')['supported'])
            sid = adapter.desktop('GRAPHICS-SN', 'open')['session']['id']
            frame = adapter.desktop('GRAPHICS-SN', 'frame', sid)['frame']
            for _ in range(2):
                result = adapter.desktop_input('GRAPHICS-SN', sid, frame['id'], 'desktop-click-001',
                    'click', {'x': 2, 'y': 3, 'button': 'left'}, screenshot=False)
                self.assertEqual(result['task']['state'], 'completed', result)
            self.assertEqual(len(screen.inputs), 1)
            screen.width = 30
            rejected = adapter.desktop_input('GRAPHICS-SN', sid, frame['id'], 'desktop-click-002',
                'click', {'x': 2, 'y': 3, 'button': 'left'}, screenshot=False)
            self.assertEqual(rejected['task']['state'], 'failed')
            self.assertEqual(len(screen.inputs), 1)
            frame = adapter.desktop('GRAPHICS-SN', 'frame', sid)['frame']
            screen.fail = True
            lost = adapter.desktop_input('GRAPHICS-SN', sid, frame['id'], 'desktop-input-lost-001',
                'text', {'text': 'do-not-record-a-password'}, screenshot=False)
            self.assertEqual(lost['task']['state'], 'failed')
            self.assertNotIn('do-not-record', json.dumps(lost))
            for request in ('desktop-input-lost-001', 'desktop-input-lost-002'):
                adapter.desktop_input('GRAPHICS-SN', sid, frame['id'], request, 'text',
                    {'text': 'do-not-record-a-password'}, screenshot=False)
            self.assertEqual(len(screen.inputs), 2)
            code, revoked = fixture.api_request(f.url, '/api/agent-revoke', f.token, {'id': grant['id']})
            self.assertEqual(code, 200, revoked)
            self.assertTrue(revoked['sessions_closed'])
            self.assertTrue(screen.closed)
            with self.assertRaises(ClientError):
                adapter.desktop('GRAPHICS-SN', 'frame', sid)

    def test_new_capture_rejects_old_frame_without_replaying_original_request(self):
        with fixture.automatic_flashing([('GRAPHICS-SN', 'graphics')]) as f:
            screen, adapter, _ = setup(f)
            sid = adapter.desktop('GRAPHICS-SN', 'open')['session']['id']
            original = adapter.desktop('GRAPHICS-SN', 'frame', sid)['frame']['id']
            parameters = {'x': 2, 'y': 3, 'button': 'left'}
            sent = adapter.desktop_input('GRAPHICS-SN', sid, original, 'observed-click-001',
                'click', parameters)
            self.assertEqual(sent['task']['state'], 'completed')
            self.assertNotEqual(sent['frame']['id'], original)
            stale = adapter.desktop_input('GRAPHICS-SN', sid, original, 'stale-click-001',
                'click', parameters, screenshot=False)
            self.assertEqual(stale['task']['state'], 'failed')
            self.assertEqual(stale['task']['outcome'], 'not-issued')
            self.assertIn('重新截图', stale['task']['error'])
            latest = adapter.desktop('GRAPHICS-SN', 'frame', sid)['frame']['id']
            repeated = adapter.desktop_input('GRAPHICS-SN', sid, original, 'observed-click-001',
                'click', parameters, screenshot=False)
            self.assertEqual(repeated['task']['id'], sent['task']['id'])
            self.assertEqual(repeated['task']['state'], 'completed')
            with self.assertRaises(ClientError):
                adapter.desktop_input('GRAPHICS-SN', sid, latest, 'observed-click-001',
                    'click', parameters, screenshot=False)
            self.assertEqual(len(screen.inputs), 1)
            fresh = adapter.desktop_input('GRAPHICS-SN', sid, latest, 'latest-click-001',
                'click', parameters, screenshot=False)
            self.assertEqual(fresh['task']['state'], 'completed')
            self.assertEqual(len(screen.inputs), 2)

    def test_queued_input_rechecks_frame_after_new_capture(self):
        with fixture.automatic_flashing([('GRAPHICS-SN', 'graphics')]) as f:
            screen, adapter, _ = setup(f)
            sid = adapter.desktop('GRAPHICS-SN', 'open')['session']['id']
            desktop, device = f.manager.desktop, f.manager.resolve('graphics')
            session = desktop.sessions[sid]
            actor = session['actor']
            original = desktop.frame(device, sid, actor)['frame']['id']
            with session['lock']:
                submitted = desktop.input(device, dict(sid=sid, frame_id=original,
                    request_id='queued-old-observation', action='click', x=1, y=1,
                    button='left'), actor, '')['task']
                desktop.frame(device, sid, actor)
            task = self.terminal(f, submitted['id'])
            self.assertEqual(task['state'], 'failed')
            self.assertEqual(task['outcome'], 'not-issued')
            self.assertEqual(screen.inputs, [])

    def test_web_observations_keep_only_each_callers_latest_frame(self):
        with fixture.automatic_flashing([('GRAPHICS-SN', 'graphics')]) as f:
            screen, _, _ = setup(f)
            desktop, device = f.manager.desktop, f.manager.resolve('graphics')
            sid = desktop.open(device, 'web', 'browser-a')['session']['id']
            original = desktop.frame(device, sid, 'web', 'browser-a')['frame']['id']
            other = desktop.frame(device, sid, 'web', 'browser-b')['frame']['id']
            desktop.frame(device, sid, 'web', 'browser-a')
            for frame_id, client_id, expected in (
                    (original, 'browser-a', 'failed'), (other, 'browser-b', 'completed')):
                task = desktop.input(device, dict(sid=sid, frame_id=frame_id,
                    request_id='web-latest-' + client_id, action='click', x=1, y=1,
                    button='left'), 'web', client_id)['task']
                self.assertEqual(self.terminal(f, task['id'])['state'], expected)
            self.assertEqual(len(screen.inputs), 1)

    def test_other_observer_cannot_restore_frame_after_unknown_input(self):
        with fixture.automatic_flashing([('GRAPHICS-SN', 'graphics')]) as f:
            screen, _, _ = setup(f)
            desktop, device = f.manager.desktop, f.manager.resolve('graphics')
            sid = desktop.open(device, 'web', 'browser-a')['session']['id']
            original = desktop.frame(device, sid, 'web', 'browser-a')['frame']['id']
            payload = dict(sid=sid, frame_id=original, request_id='unknown-observed-input',
                action='click', x=1, y=1, button='left')
            screen.fail = True
            lost = desktop.input(device, payload, 'web', 'browser-a')['task']
            lost = self.terminal(f, lost['id'])
            self.assertEqual((lost['state'], lost['outcome']), ('failed', 'unknown'))
            desktop.frame(device, sid, 'web', 'browser-b')
            screen.fail = False
            stale = desktop.input(device, dict(payload, request_id='unknown-old-frame'),
                'web', 'browser-a')['task']
            self.assertEqual(self.terminal(f, stale['id'])['outcome'], 'not-issued')
            desktop.frame(device, sid, 'web', 'browser-a')
            repeated = desktop.input(device, payload, 'web', 'browser-a')['task']
            self.assertEqual((repeated['id'], repeated['outcome']), (lost['id'], 'unknown'))
            self.assertEqual(len(screen.inputs), 1)

    def test_other_grants_and_unknown_connection_cannot_access_session(self):
        with fixture.automatic_flashing([('GRAPHICS-SN', 'graphics'), ('OTHER-SN', 'other')]) as f:
            screen, adapter, grant = setup(f)
            sid = adapter.desktop('GRAPHICS-SN', 'open')['session']['id']
            other_token, _ = f.grants.issue('all')
            other = Adapter(Settings(f.url, other_token))
            with self.assertRaises(ClientError):
                other.desktop('GRAPHICS-SN', 'open')
            with self.assertRaises(ClientError):
                other.desktop('GRAPHICS-SN', 'frame', sid)
            self.assertFalse(other.desktop('OTHER-SN')['supported'])
            raw_token, _ = f.grants.issue('connection', key='raw', connection_id='raw-connection')
            code, _ = fixture.api_request(f.url, '/api/desktop/open', raw_token,
                {'key': 'fake', 'serial': 'GRAPHICS-SN'})
            self.assertEqual(code, 403)
            adapter.desktop('GRAPHICS-SN', 'close', sid)
            self.assertTrue(screen.closed)

    @unittest.skipUnless(MCPServer, 'optional MCP SDK not installed')
    def test_official_mcp_returns_image_blocks_before_and_after_input(self):
        from mcp import Client, StdioServerParameters
        with fixture.automatic_flashing([('GRAPHICS-SN', 'graphics')]) as f:
            screen, adapter, _ = setup(f)
            async def check():
                params = StdioServerParameters(command=sys.executable,
                    args=[str(Path(__file__).resolve().parents[1] / 'seriald_mcp.py')],
                    env=dict(os.environ, SERIALD_URL=f.url, SERIALD_TOKEN=adapter.settings.token))
                async with Client(params) as client:
                    opened = await client.call_tool('open_desktop', {'device': 'GRAPHICS-SN'})
                    self.assertFalse(opened.is_error, str(opened))
                    sid = opened.structured_content['session']['id']
                    capture = await client.call_tool('capture_desktop', {'device': 'GRAPHICS-SN', 'sid': sid})
                    self.assertFalse(capture.is_error, str(capture))
                    self.assertEqual([c.type for c in capture.content], ['text', 'image'])
                    frame = json.loads(capture.content[0].text)['frame']
                    self.assertEqual((frame['width'], frame['height']), (20, 12))
                    self.assertNotIn('png_base64', capture.content[0].text)
                    result = await client.call_tool('desktop_input', {'device': 'GRAPHICS-SN', 'sid': sid,
                        'frame_id': frame['id'], 'request_id': 'mcp-desktop-click-001', 'action': 'click', 'x': 3, 'y': 2})
                    self.assertFalse(result.is_error, str(result))
                    self.assertEqual([c.type for c in result.content], ['text', 'image'])
                    sent = json.loads(result.content[0].text)['task']
                    self.assertEqual(sent['state'], 'completed')
                    stale = await client.call_tool('desktop_input', {'device': 'GRAPHICS-SN', 'sid': sid,
                        'frame_id': frame['id'], 'request_id': 'mcp-stale-click-001', 'action': 'click',
                        'x': 3, 'y': 2, 'screenshot': False})
                    self.assertFalse(stale.is_error, str(stale))
                    rejected = json.loads(stale.content[0].text)['task']
                    self.assertEqual((rejected['state'], rejected['outcome']), ('failed', 'not-issued'))
                    repeated = await client.call_tool('desktop_input', {'device': 'GRAPHICS-SN', 'sid': sid,
                        'frame_id': frame['id'], 'request_id': 'mcp-desktop-click-001', 'action': 'click',
                        'x': 3, 'y': 2, 'screenshot': False})
                    self.assertFalse(repeated.is_error, str(repeated))
                    self.assertEqual(json.loads(repeated.content[0].text)['task']['id'], sent['id'])
                    self.assertEqual(len(screen.inputs), 1)
            asyncio.run(check())


class ReservationTests(unittest.TestCase):
    def test_original_activity_update_is_bound_and_a_lost_response_is_not_retried(self):
        expected = dict(origin='https://console.example', credential_file='/private/console.json',
            device_id='test-board-a', task='current-session', requester='tester', lease_id='original-lease')
        credential = dict(origin=expected['origin'], username='tester', password='private')
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps({'data': {
            'id': 'original-lease', 'task': 'current-session', 'requester': 'tester', 'activity': 'running'}}).encode()
        with mock.patch('seriald_core.desktop.Path.read_text', return_value=json.dumps(credential)), \
                mock.patch('seriald_core.desktop.build_opener', return_value=opener):
            reservation_activity({'reservation': expected}, 'running')
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://console.example/api/ops/v1/devices/test-board-a/reservations/original-lease')
            self.assertEqual(json.loads(request.data)['action'], 'running')
            opener.open.reset_mock()
            opener.open.side_effect = TimeoutError()
            with self.assertRaises(TaskError):
                reservation_activity({'reservation': expected}, 'running')
            self.assertEqual(opener.open.call_count, 1)

    def test_console_is_queried_and_forged_expired_or_other_task_reservations_fail(self):
        config = {'reservation': {'origin': 'https://console.example', 'credential_file': '/private/console.json',
            'device_id': 'test-board-a', 'task': 'DECKLAB-1', 'requester': 'tester', 'lease_id': 'original-lease'}}
        credential = dict(origin='https://console.example', username='tester', password='private')
        lease = {'id': 'original-lease', 'task': 'DECKLAB-1', 'requester': 'tester', 'state': 'reserved',
                 'actions': ['desktop'], 'activity': 'idle', 'overdue': True}
        opener = mock.MagicMock()
        incoming = opener.open.return_value.__enter__.return_value
        with mock.patch('seriald_core.desktop.Path.read_text', return_value=json.dumps(credential)), \
                mock.patch('seriald_core.desktop.build_opener', return_value=opener):
            incoming.read.return_value = json.dumps({'data': [{'id': 'test-board-a', 'lease': lease}]}).encode()
            self.assertEqual(check_reservation(config)['source'], 'console')
            self.assertEqual(opener.open.call_count, 1)
            self.assertEqual(check_reservation(config, for_input=True)['source'], 'console')
            for activity in ('running', 'unknown'):
                incoming.read.return_value = json.dumps({'data': [{'id': 'test-board-a', 'lease': dict(lease, activity=activity)}]}).encode()
                self.assertEqual(check_reservation(config)['source'], 'console')
                with self.assertRaises(TaskError):
                    check_reservation(config, for_input=True)
            for key, value in (('state', 'released'), ('task', 'OTHER-1'), ('requester', 'another'), ('id', 'forged')):
                incoming.read.return_value = json.dumps({'data': [{'id': 'test-board-a', 'lease': dict(lease, **{key: value})}]}).encode()
                with self.assertRaises(TaskError):
                    check_reservation(config)
            opener.open.side_effect = TimeoutError()
            with self.assertRaises(TaskError):
                check_reservation(config)
