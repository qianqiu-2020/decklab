"""Real directory formats, bounded metadata, shared cache, no board activity."""
import json
import os
import unittest
from unittest import mock

from seriald_core import image_catalog as catalog
from seriald_core.tasks import TaskError
from test_image_download import fixture, wait_task, DATA
from seriald_core.image_download import ImageDownloads


class CatalogTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {
            'SERIALD_IMAGE_INTERNAL_BROWSE': 'https://images.example.test/service/rest/repository/browse/image/',
            'SERIALD_IMAGE_INTERNAL_DOWNLOAD': 'https://images.example.test/repository/image/'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_organization_source_is_opt_in_and_requires_safe_paired_urls(self):
        self.assertEqual(list(catalog.configured_sources({})), ['public'])
        for url in ('http://images.example.test/', 'https://[broken/', 'https://user:password@images.example.test/',
                    'https://images.example.test/?token=private', 'https://images.example.test'):
            with self.subTest(url=url), self.assertRaises(TaskError):
                catalog.configured_sources({'SERIALD_IMAGE_INTERNAL_BROWSE': url,
                                            'SERIALD_IMAGE_INTERNAL_DOWNLOAD': url})
        with mock.patch.dict(os.environ, {'SERIALD_IMAGE_INTERNAL_BROWSE': '', 'SERIALD_IMAGE_INTERNAL_DOWNLOAD': ''}):
            with self.assertRaisesRegex(TaskError, '尚未配置'):
                catalog.catalog('internal')

    def test_library_keeps_global_and_device_images_without_name_replacement(self):
        with fixture() as (manager, _):
            ids = []
            for serial in ('', 'BOARD-A', 'BOARD-B'):
                row = manager.artifacts.create('upload', 'same.tar.gz', len(DATA), 'web', serial)
                manager.artifacts.append(row['id'], 0, DATA, 'web', serial)
                manager.artifacts.seal(row['id'], 'web', serial)
                ids.append(row['id'])
            rows = catalog.library(manager)['artifacts']
            self.assertEqual({row['id'] for row in rows}, set(ids))
            self.assertEqual({row['serial'] for row in rows}, {'', 'BOARD-A', 'BOARD-B'})
            self.assertEqual(catalog.library(manager, 'agent')['artifacts'], [])

    def test_public_index_is_direct_sorted_and_escapes_cannot_select_other_hosts(self):
        raw = '''<a href="../">parent</a><a href="k3/">K3</a><a href="k1/">K1</a>
        <a href="scripts/">scripts</a><a href="https://evil.test/k1/">bad</a>
        <a href="k1/%2e%2e/">bad</a>'''
        self.assertEqual([r['path'] for r in catalog.parse_index('public', '', raw)], ['k1/', 'k3/'])
        raw = '''<a href="../">../</a>
<a href="firmware.tar.gz">firmware.tar.gz</a> 14-Nov-2026 12:00 1024
<a href="flash-all.zip">flash-all.zip</a> 14-Nov-2026 12:00 627
<a href="bad.sh">bad.sh</a><a href="sub/deep.img">deep.img</a>
<a href="file.img?token=x">token</a><a href="%252e%252e/">bad</a>'''
        result = catalog.parse_index('public', 'k3/', raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['url'], 'https://archive.spacemit.com/image/k3/firmware.tar.gz')
        self.assertEqual(result[0]['validation'], 'preview-required')
        self.assertEqual(result[0]['size_label'], '1024')

    def test_catalog_excludes_raw_disks_and_checksums_but_keeps_supported_payloads(self):
        names = ('sd.img', 'sd.img.gz', 'sd.img.zip', 'sd.img.zst', 'disk.raw.gz',
                 'image.iso', 'image.tar.gz.md5', 'SHA256SUMS', 'flash-all.zip',
                 'Bianbu-LXQt-K3.tar.gz', 'Buildroot-K3.zip', 'rootfs.ext4', 'opensbi.itb')
        raw = ''.join('<a href="' + name + '">' + name + '</a>' for name in names)
        result = catalog.parse_index('public', 'k3/', raw)
        self.assertEqual({row['name'] for row in result},
                         {'Bianbu-LXQt-K3.tar.gz', 'Buildroot-K3.zip', 'rootfs.ext4', 'opensbi.itb'})

    def test_nexus_download_uses_repository_url_and_row_metadata(self):
        raw = '''<table><tr><td><a href="daily/">daily</a></td></tr>
<tr><td><a href="https://images.example.test/repository/image/k3/opensbi.itb">opensbi.itb</a></td>
<td>2026-11-14 12:00</td><td>1024</td></tr>
<tr><td><a href="__probe__/">probe</a></td></tr></table>'''
        result = catalog.parse_index('internal', 'k3/', raw)
        self.assertEqual([r['kind'] for r in result], ['directory', 'partition-file'])
        self.assertEqual(result[1]['size'], 1024)
        self.assertEqual(result[1]['modified'], '2026-11-14 12:00')
        self.assertEqual(result[1]['url'], 'https://images.example.test/repository/image/k3/opensbi.itb')

    def test_paths_and_unknown_sources_are_rejected_before_network(self):
        for path in ('../', '/k3/', 'k3//dir', 'k3/../', 'k3/%2f', 'k3/?x', 'scripts/', 'k3/\\x', 'k3/\n'):
            with self.subTest(path=path), self.assertRaises(TaskError): catalog.catalog_path(path)
        with mock.patch.object(catalog, '_fetch') as fetch:
            with self.assertRaises(TaskError): catalog.catalog('arbitrary')
            fetch.assert_not_called()

    def test_cache_cannot_be_mutated_by_caller_and_refresh_refetches(self):
        with mock.patch.object(catalog, '_CACHE', {}), mock.patch.object(catalog, '_fetch', return_value='<a href="k3/">K3</a>') as fetch:
            first = catalog.catalog(); first['entries'].clear()
            self.assertEqual(len(catalog.catalog()['entries']), 1)
            self.assertEqual(fetch.call_count, 1)
            catalog.catalog(refresh=True); self.assertEqual(fetch.call_count, 2)
            with mock.patch.object(catalog, '_fetch', side_effect=OSError('private details')):
                with self.assertRaisesRegex(TaskError, '内部镜像源暂不可访问') as result:
                    catalog.catalog('internal', refresh=True)
                self.assertNotIn('private details', str(result.exception))

    def test_global_download_can_be_reused_by_human_only_and_deleted(self):
        with fixture() as (manager, url):
            result = ImageDownloads(manager).start({'url':url+'/image', 'name':'opensbi.itb', 'request_id':'global-image-001'}, None)
            self.assertEqual(result.status, 202, result.payload)
            task = wait_task(manager, result.payload['task'])
            self.assertEqual(task['state'], 'completed', task)
            items = catalog.library(manager)
            self.assertEqual(len(items['artifacts']), 1)
            row = items['artifacts'][0]
            self.assertEqual(manager.artifacts.get(row['id'], 'web', 'SN-OTHER')['size'], len(DATA))
            with manager.artifacts.open(row['id'], 'web', 'SN-OTHER') as stream:
                self.assertEqual(stream.read(), DATA)
            with self.assertRaises(TaskError): manager.artifacts.get(row['id'], 'agent', 'SN-OTHER')
            manager.artifacts.claim(row['id'], task['id'], 'web', 'SN-OTHER')
            self.assertEqual(catalog.library(manager)['artifacts'][0]['state'], 'claimed')
            with self.assertRaises(TaskError): manager.artifacts.remove(row['id'])
            manager.artifacts.finish_claim(row['id'], task['id'])
            manager.artifacts.remove(row['id'])
            self.assertEqual(catalog.library(manager)['artifacts'], [])

    def test_invalid_board_request_does_not_turn_into_global_download(self):
        with fixture() as (manager, url):
            for field in ('key','serial','connection_id'):
                result = ImageDownloads(manager).start({'url':url+'/image','request_id':'scoped-image-001',field:'missing'}, None)
                self.assertEqual(result.status, 400)

    def test_protected_http_catalog_owner_and_agent_grant_boundaries(self):
        from test_connection_access import protected_connection, api_request
        with protected_connection() as fixture, mock.patch.object(catalog, 'catalog', return_value={'entries':[]}) as fetch:
            code, data = api_request(fixture.url,'/api/image-catalog',fixture.owner_token)
            self.assertEqual((code,data),(200,{'entries':[]}))
            for route in ('/api/image-catalog','/api/image-library'):
                code, data = api_request(fixture.url,route,fixture.token)
                self.assertEqual(code,403,(route,data))
                code, data = api_request(fixture.url,route,fixture.grant_store.issue('all')[0])
                self.assertEqual(code,200,(route,data))
            self.assertEqual(fetch.call_count, 2)
            code, data = api_request(fixture.url,'/api/image-library',fixture.owner_token)
            self.assertEqual(code,200,data)
            self.assertEqual(data['artifacts'],[])
            fixture.send.assert_not_called()

    def test_certificate_error_is_actionable_without_disabling_validation(self):
        import ssl
        with mock.patch.object(catalog,'_fetch',side_effect=ssl.SSLCertVerificationError('private detail')):
            with self.assertRaisesRegex(TaskError,'组织 CA') as result:catalog.catalog('internal',refresh=True)
            self.assertEqual(result.exception.status,502)
            self.assertNotIn('private detail',str(result.exception))
