"""Real local HTTP streams with isolated spools; no hardware or external network."""
from contextlib import closing, contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace

from seriald_core.image_download import ImageDownloads, _addresses, fetch_image, image_url
from seriald_core.storage import Store
from seriald_core.tasks import TaskError, TaskManager
from seriald_core.transfer_artifacts import ArtifactPolicy, TransferArtifacts

DATA = b'opensbi-test-payload' * 100


class ImageHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        route = self.path.split('?')[0]
        if route in ('/redirect', '/loop'):
            self.send_response(302)
            self.send_header('Location', '/image' if route == '/redirect' else '/loop')
            self.end_headers()
            return
        self.send_response(403 if route == '/denied' else 200)
        if route != '/unknown':
            self.send_header('Content-Length', str(len(DATA) + (5 if route == '/truncated' else 0)))
        self.end_headers()
        self.wfile.write(DATA)


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as directory, ThreadingHTTPServer(('127.0.0.1', 0), ImageHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with closing(Store(directory + '/registry.sqlite3')) as store:
            artifacts = TransferArtifacts(store, directory + '/images', ArtifactPolicy(
                max_file=8192, max_total=32768, reserve_free=0))
            manager = SimpleNamespace(store=store, artifacts=artifacts, tasks=TaskManager(store))
            address = server.server_address
            with mock.patch('seriald_core.image_download._addresses', return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, address)]):
                try:
                    yield manager, 'http://127.0.0.1:%d' % address[1]
                finally:
                    manager.tasks.close()
        server.shutdown()
        thread.join(3)


def wait_task(manager, task):
    for _ in range(200):
        current = manager.tasks.store.get(task['id'])
        if current['state'] not in ('queued', 'running'):
            return current
        time.sleep(.01)
    raise AssertionError('download task did not finish')


class ImageDownloadTests(unittest.TestCase):
    def start(self, manager, url, request='download-test-01', **values):
        payload = dict(url=url, request_id=request, name='opensbi.itb', **values)
        result = ImageDownloads(manager).start(payload, SimpleNamespace(serial=''))
        self.assertEqual(result.status, 202, result.payload)
        return wait_task(manager, result.payload['task'])

    def test_redirect_download_hash_and_idempotent_request_publish_reusable_payload(self):
        with fixture() as (manager, url):
            task = self.start(manager, url + '/redirect?secret=do-not-record', sha256=hashlib.sha256(DATA).hexdigest())
            self.assertEqual(task['state'], 'completed', task)
            row = task['result']['artifact']
            self.assertEqual(row['size'], len(DATA))
            self.assertEqual(row['sha256'], hashlib.sha256(DATA).hexdigest())
            with manager.artifacts.open(row['id']) as stream:
                self.assertEqual(stream.read(), DATA)
            again = self.start(manager, url + '/redirect?secret=do-not-record', sha256=row['sha256'])
            self.assertEqual(again['id'], task['id'])
            changed = ImageDownloads(manager).start({'url': url + '/other', 'request_id': 'download-test-01'},
                                                    SimpleNamespace(serial=''))
            self.assertEqual(changed.status, 409)
            self.assertNotIn('secret', str(task))
            manager.artifacts.claim(row['id'], task['id'])

    def test_unknown_length_reservation_shrinks_to_actual_size(self):
        with fixture() as (manager, url):
            task = self.start(manager, url + '/unknown')
            self.assertEqual(task['state'], 'completed', task)
            row = manager.artifacts.get(task['result']['artifact']['id'])
            self.assertEqual((row['size'], row['received'], row['task_id']), (len(DATA), len(DATA), None))

    def test_bad_checksum_truncated_denied_and_redirect_loop_never_publish(self):
        for path, checksum in (('/image', '0' * 64), ('/truncated', None), ('/denied?token=private', None), ('/loop', None)):
            with self.subTest(path=path), fixture() as (manager, url):
                task = self.start(manager, url + path, sha256=checksum)
                self.assertEqual(task['state'], 'failed', task)
                self.assertNotIn('private', task['error'])
                self.assertEqual(manager.store.db.execute("SELECT COUNT(*) FROM transfer_artifacts WHERE state='ready'").fetchone()[0], 0)
                self.assertEqual(list(manager.artifacts.root.glob('*.part')), [])

    def test_oversized_image_rejected_before_reserving_artifact(self):
        with fixture() as (manager, url):
            manager.artifacts.policy = ArtifactPolicy(max_file=32, max_total=64, reserve_free=0)
            task = self.start(manager, url + '/image')
            self.assertEqual(task['state'], 'failed')
            self.assertIn('size limit', task['error'])

    def test_cancel_discards_partial_file_and_restart_never_replays_download(self):
        with fixture() as (manager, url):
            from seriald_core.tasks import TaskContext
            task_id = manager.tasks.store.create('image-download', 'web', '', [{'serial': '', 'connection_id': 'images'}])
            context = TaskContext(task_id, manager.tasks.store)
            original = manager.artifacts.append
            def cancel_after_chunk(*args, **kwargs):
                result = original(*args, **kwargs)
                context.cancel()
                return result
            with mock.patch.object(manager.artifacts, 'append', side_effect=cancel_after_chunk):
                from seriald_core.tasks import TaskCancelled
                with self.assertRaises(TaskCancelled):
                    fetch_image(manager.artifacts, context, url + '/image', '', 'opensbi.itb')
            self.assertEqual(list(manager.artifacts.root.glob('*.part')), [])
            manager.tasks.store.recover()
            manager.artifacts.recover()
            self.assertEqual(manager.tasks.store.get(task_id)['state'], 'interrupted')

    def test_only_human_can_import_and_request_id_required(self):
        with fixture() as (manager, url):
            actions = ImageDownloads(manager)
            self.assertEqual(actions.start({'url': url}, SimpleNamespace(serial=''), 'agent').status, 403)
            self.assertEqual(actions.start({'url': url}, SimpleNamespace(serial='')).status, 400)

    def test_url_and_dns_policy_allows_lan_repositories_without_host_local_access(self):
        for url in ('file:///etc/passwd', 'http://user:pass@example.com/x', 'http://a/#x', 'https://a:99999/x', 'http://a/\nX'):
            with self.subTest(url=url), self.assertRaises(TaskError):
                image_url(url)
        for address in ('127.0.0.1', '169.254.169.254', '0.0.0.0', '::1', '::ffff:127.0.0.1'):
            with self.subTest(address=address), mock.patch('socket.getaddrinfo', return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 80))]), self.assertRaises(TaskError):
                _addresses('example.test', 80)
        with mock.patch('socket.getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.90.10', 80))]):
            self.assertEqual(_addresses('repo.lan', 80)[0][3][0], '10.0.90.10')

    def test_windows_tls_uses_native_store_without_global_ssl_injection(self):
        import ssl
        from seriald_core.image_download import image_ssl_context
        native=mock.Mock()
        with mock.patch('seriald_core.image_download.sys.platform','win32'), \
                mock.patch.dict('sys.modules',{'truststore':SimpleNamespace(SSLContext=native)}), \
                mock.patch('seriald_core.image_download.ssl.create_default_context') as default:
            self.assertIs(image_ssl_context(),native.return_value)
            native.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
            default.assert_not_called()
        with mock.patch('seriald_core.image_download.sys.platform','linux'), \
                mock.patch('seriald_core.image_download.ssl.create_default_context') as default:
            self.assertIs(image_ssl_context(),default.return_value)
