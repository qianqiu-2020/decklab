"""Attachment history stays segmented; no serial device or board input is used."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from decklab_core import device as device_module
from decklab_core.queries import ArchiveDownload, ArchiveQueries
from test_connection_access import api_request, protected_connection


OLD = 'a' * 24
NAME = 'serial-20261116.log'


def write_capture(root, connection, text):
    path = root / connection / NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    data = ('2026-11-16 10:00:00  U-Boot 2022.10\n2026-11-16 10:00:01  ' + text + '\n').encode()
    path.write_bytes(data)
    return data


class ArchiveConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = mock.patch.object(device_module, 'LOG_ROOT', str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.device = device_module.Device('fake-usb-port', '')
        self.addCleanup(self.device.terminal.screen.close)
        self.attachment = Path(self.device.stable_dir()).parent
        self.old = write_capture(self.attachment, OLD, 'previous capture only')
        self.current = write_capture(self.attachment, self.device.connection_id, 'current capture only')
        self.query = ArchiveQueries(None, log_root=str(self.root))

    def test_default_capture_stays_current_and_history_is_explicit(self):
        result = self.query.execute('logs', {}, self.device).payload
        self.assertEqual(result['archive_connection'], self.device.connection_id)
        self.assertEqual({item['id'] for item in result['connections']}, {OLD, self.device.connection_id})
        self.assertEqual(sum(item['current'] for item in result['connections']), 1)
        self.assertEqual(result['files'][0]['size'], len(self.current))
        old = self.query.execute('logs', {'archive_connection': OLD}, self.device).payload
        self.assertEqual(old['files'][0]['size'], len(self.old))
        self.assertTrue(old['archive'].endswith('/' + OLD))
        self.assertEqual(self.query.execute('log', {'file': NAME, 'full': 1}, self.device).payload['data'].encode(), self.current)

    def test_read_download_boot_and_search_remain_in_the_selected_capture(self):
        params = {'archive_connection': OLD}
        listed = self.query.execute('logs', params, self.device).payload
        result = self.query.execute('log', dict(params, file=NAME, full=1), self.device)
        self.assertEqual(result.payload['data'].encode(), self.old)
        download = self.query.execute('log', dict(params, file=NAME, download=1), self.device)
        self.assertIsInstance(download, ArchiveDownload)
        self.assertEqual(Path(download.path).read_bytes(), self.old)
        boot = self.query.execute('boot-log', dict(params, id=listed['boots'][0]['id']), self.device)
        self.assertIn('previous capture only', boot.payload['data'])
        searched = self.query.execute('boot-search', dict(params, q='previous capture'), self.device)
        self.assertEqual(len(searched.payload['results']), 1)
        # The same date and boot offset exist in both captures; caches must not alias.
        current = self.query.execute('boot-log', {'id': listed['boots'][0]['id']}, self.device)
        self.assertIn('current capture only', current.payload['data'])

    def test_a_quiet_current_connection_remains_selectable(self):
        (self.attachment / self.device.connection_id / NAME).unlink()
        listed = self.query.execute('logs', {}, self.device).payload
        current = next(item for item in listed['connections'] if item['current'])
        self.assertEqual(current['size'], 0)
        self.assertEqual(listed['files'], [])
        selected = self.query.execute('logs', {'archive_connection': current['id']}, self.device)
        self.assertEqual(selected.status, 200)
        self.assertEqual(selected.payload['files'], [])

    def test_other_attachments_and_symlink_directories_are_not_history(self):
        other = self.root / 'other-attachment'
        write_capture(other, 'b' * 24, 'unrelated')
        (self.attachment / ('b' * 24)).symlink_to(other / ('b' * 24), target_is_directory=True)
        for identifier in ('b' * 24, '../other-attachment', '/etc', OLD + '/..'):
            with self.subTest(identifier=identifier):
                result = self.query.execute('log', {'archive_connection': identifier, 'file': NAME}, self.device)
                self.assertEqual(result.status, 404)
        self.assertEqual(len(self.device.archive_connections()), 2)

    def test_connection_grant_cannot_enumerate_or_read_previous_captures_over_http(self):
        with protected_connection() as f:
            attachment = Path(f.device.stable_dir()).parent
            write_capture(attachment, f.device.connection_id, 'current')
            old = write_capture(attachment, OLD, 'private previous connection')
            query = {'key': f.device.key}
            code, listed = api_request(f.url, '/api/logs', f.token, query=query)
            self.assertEqual(code, 200)
            self.assertEqual(listed['connections'], [])
            code, _ = api_request(f.url, '/api/log', f.token,
                query=dict(query, archive_connection=OLD, file=NAME))
            self.assertEqual(code, 403)
            code, listed = api_request(f.url, '/api/logs', f.owner_token,
                query=dict(query, archive_connection=OLD))
            self.assertEqual(code, 200)
            self.assertEqual(listed['files'][0]['size'], len(old))
            f.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
