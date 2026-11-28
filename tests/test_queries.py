"""Archive/query contracts use real temporary files and no devices."""
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest import mock

from decklab_core.queries import ArchiveQueries, ArchiveDownload, SessionQueries
from decklab_core.archive_io import open_archive
from decklab_core import web


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = b'U-Boot\nready\n' + b'line\n' * 15000
        self.file = self.root / 'serial-20261001.log'
        self.file.write_bytes(self.data)
        self.boot = {'id': 'boot1', 'number': 1, 'size': len(self.data), 'started': '2026-10-01',
                     'parts': [{'file': self.file.name, 'start': 0, 'end': len(self.data)}]}
        self.device = mock.Mock(key='board', label='board', serial='SN1', dev='/fake/tty', terminal=None)
        self.device.log_files.return_value = [{'name': self.file.name, 'size': len(self.data)}]
        self.device.boot_epochs.return_value = [self.boot]
        self.device.stable_dir.return_value = str(self.root)
        self.manager = types.SimpleNamespace(resolve=lambda key: self.device, store=mock.Mock())
        self.queries = ArchiveQueries(self.manager, log_root=str(self.root.parent))

    def http(self, action, params):
        from urllib.parse import urlencode
        handler = web.Handler.__new__(web.Handler)
        handler.path = '/api/' + action + '?' + urlencode(dict(params, key='board', serial='SN1'))
        handler.headers = {}
        handler.wfile = io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.object(web, 'MGR', self.manager), mock.patch.object(web, 'LOG_ROOT', str(self.root.parent)):
            handler.do_GET()
        payload = json.loads(handler.wfile.getvalue())
        payload.pop('key', None)
        return handler.send_response.call_args.args[0], payload

    def test_listing_does_not_mutate_cached_boot_parts(self):
        result = self.queries.execute('logs', {}, self.device)
        self.assertNotIn('parts', result.payload['boots'][0])
        self.assertIn('parts', self.boot)
        self.assertEqual(self.http('logs', {}), (result.status, result.payload))
        self.assertIn('parts', self.boot)

    def test_task_window_checks_board_and_returns_only_the_task_time_range(self):
        def stamp(second):
            return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(second))

        now = time.mktime((2026, 10, 1, 12, 0, 0, 0, 0, -1))
        data = ''.join(f'{stamp(at)}  {label}\n' for at, label in
                       [(now - 40, 'before'), (now - 10, 'during'), (now + 10, 'after')])
        self.file.write_text(data, encoding='utf-8')
        self.device.log_files.return_value = [{'name': self.file.name, 'kind': 'serial', 'date': stamp(now)[:10]}]
        self.manager.tasks = mock.Mock()
        store = self.manager.tasks.store
        store.get.return_value = {'actor': 'web', 'created': now - 20, 'updated': now}
        store.visible_serial.return_value = True
        params = {'task_window': '1' * 24}
        result = self.queries.execute('logs', params, self.device)
        self.assertIn('during', result.payload['data'])
        self.assertNotIn('before', result.payload['data'])
        self.assertNotIn('after', result.payload['data'])
        self.assertEqual(self.queries.execute('logs', params, self.device, actor='other').status, 404)
        self.assertEqual(self.queries.execute('logs', params, self.device, allow_connection_history=False).status, 403)
        store.visible_serial.return_value = False
        self.assertEqual(self.queries.execute('logs', params, self.device).status, 404)

    def test_preview_full_read_and_boot_read_match_http(self):
        for action, params in [('log', {'file': self.file.name}),
                               ('log', {'file': self.file.name, 'full': '1'}),
                               ('boot-log', {'id': 'boot1'})]:
            result = self.queries.execute(action, params, self.device)
            self.assertEqual(self.http(action, params), (result.status, result.payload))
            expected = self.data[-65536:] if action == 'log' and not params.get('full') else self.data
            self.assertEqual(base64.b64decode(result.payload['data_b64']), expected)

    def test_search_pages_actual_boot_ranges_and_preserves_cached_parts(self):
        result = self.queries.execute('boot-search', {'q': 'line', 'offset': 3, 'limit': 2}, self.device)
        self.assertEqual(result.status, 200)
        self.assertEqual(len(result.payload['results']), 2)
        self.assertTrue(result.payload['more'])
        self.assertEqual(self.http('boot-search', {'q': 'line', 'offset': 3, 'limit': 2}),
                         (result.status, result.payload))

    def test_invalid_queries_and_traversal_are_rejected(self):
        for action, params in [('log', {'file': '../registry.sqlite3'}),
                               ('log', {'file': self.file.name, 'off': -2}),
                               ('boot-search', {'q': '', 'limit': 0}),
                               ('activity', {'event_offset': -1})]:
            result = self.queries.execute(action, params, self.device)
            self.assertIn(result.status, (400, 404))
            self.assertEqual(self.http(action, params), (result.status, result.payload))
        self.assertEqual(self.queries.execute('logs', {'off': []}, self.device).status, 400)

    def test_download_is_a_streaming_descriptor_not_file_contents(self):
        result = self.queries.execute('log', {'file': self.file.name, 'download': 1}, self.device)
        self.assertIsInstance(result, ArchiveDownload)
        with open_archive(result.path) as source:
            self.assertEqual(source.read(), self.data)

    def test_symlink_replacement_is_rejected_for_preview_boot_and_search(self):
        self.file.unlink()
        self.file.symlink_to(self.root / 'target.log')
        (self.root / 'target.log').write_bytes(b'secret')
        for action, params in [('log', {'file': self.file.name}),
                               ('boot-log', {'id': 'boot1'}), ('boot-search', {'q': 'secret'})]:
            self.assertEqual(self.queries.execute(action, params, self.device).status, 404)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'Unix FIFO fixture')
    def test_nonregular_archive_is_rejected_before_opening(self):
        fifo = self.root / 'fifo.log'
        os.mkfifo(fifo)
        with mock.patch('decklab_core.archive_io.os.open') as opener:
            with self.assertRaises(OSError):
                open_archive(str(fifo))
        opener.assert_not_called()

    def test_replaced_file_identity_closes_descriptor(self):
        before = os.lstat(self.file)
        changed = types.SimpleNamespace(st_mode=before.st_mode, st_dev=before.st_dev, st_ino=before.st_ino + 1)
        with mock.patch('decklab_core.archive_io.os.fstat', return_value=changed), \
                mock.patch('decklab_core.archive_io.os.close', wraps=os.close) as close:
            with self.assertRaisesRegex(OSError, 'changed'):
                open_archive(str(self.file))
        close.assert_called_once()

    def test_compare_requires_pinned_serials_before_reading(self):
        params = {'left_key': 'board', 'left_serial': 'wrong', 'left_id': 'boot1',
                  'right_key': 'board', 'right_serial': 'SN1', 'right_id': 'boot1'}
        with mock.patch.object(self.queries, 'read_epoch') as reader:
            self.assertEqual(self.queries.compare(params).status, 404)
        reader.assert_not_called()

    def test_session_cursors_are_independent_and_invalid_offsets_never_read(self):
        stream = SessionQueries()
        self.device.read.return_value = (12, b'\xffhello', True)
        first = stream.execute('session', {'off': 3}, self.device)
        second = stream.execute('session', {'off': 7}, self.device)
        self.assertEqual(self.device.read.call_args_list, [mock.call(3), mock.call(7)])
        self.assertEqual(first.payload['data_b64'], second.payload['data_b64'])
        self.assertEqual(stream.execute('session', {'off': -1}, self.device).status, 400)
        self.assertEqual(self.device.read.call_count, 2)

    def test_bounded_serial_window_starts_at_the_cursor_without_waiting(self):
        stream = SessionQueries()
        self.device.read.return_value = (19, b'abcdefghi', True)
        data = stream.execute('serial', {'off': 10, 'wait': 0, 'max_bytes': 4}, self.device).payload
        self.device.read.assert_called_once_with(10, wait=0)
        self.assertEqual((data['data'], data['off'], data['more']), ('abcd', 14, True))
        self.assertEqual(stream.execute('serial', {'off': 10, 'max_bytes': 65537}, self.device).status, 400)


if __name__ == '__main__':
    unittest.main()
