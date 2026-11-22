"""Controller image imports and edits use isolated task registries."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

import test_flash_integration as integration
from seriald_core.image_workspaces import ImageWorkspaces, origin_path
from seriald_core.flash_runner import LocalFlashExecutor


class WorkspaceTests(unittest.TestCase):
    def directory(self, root):
        directory=Path(root)/'directory';directory.mkdir()
        (directory/'fastboot.yaml').write_text("version: 1.0\nsupport: [k3]\nactions:\n - multi_flash:\n     relate_partition: ['partition_{size1}.json']\n")
        (directory/'partition_universal.json').write_text(json.dumps({'format':'gpt','partitions':[{'name':'rootfs','offset':'1M','size':'-','image':'rootfs.ext4'}]}))
        (directory/'rootfs.ext4').write_bytes(b'fixture root filesystem')
        return directory

    def test_registers_directory_revision_and_does_not_trust_uploaded_descriptor(self):
        with integration.protected_flashing() as f,tempfile.TemporaryDirectory() as root:
            directory=self.directory(root)
            service=ImageWorkspaces(f.manager)
            result=service.request({'action':'import','directory':str(directory)})
            self.assertEqual(result.status,202,result.payload)
            row=result.payload['artifact'];source=f.manager.artifacts._path(row['id'],'.bin')
            self.assertTrue(origin_path(source).is_file())
            with f.manager.artifacts.open(row['id']) as stream:
                self.assertEqual(json.load(stream)['directory'],str(directory))
            listing=service.request({'id':row['id']}).payload
            self.assertTrue(listing['editable']);self.assertEqual(len(listing['files']),3)
            self.assertEqual(service.request({'id':row['id']},'agent').status,403)
            self.assertEqual(service.request({'action':'read','id':row['id'],'path':'../outside'}).status,400)
            f.manager.artifacts.remove(row['id'])
            self.assertFalse(origin_path(source).exists())

    def test_controller_directory_browser_is_owner_only_through_http_proxy(self):
        with integration.protected_flashing() as f,tempfile.TemporaryDirectory() as root:
            directory=self.directory(root)
            helper=integration.FlashHTTPIntegrationTests()
            code,listed=helper.request(f,'/api/image-workspace',{'action':'directories','directory':root})
            self.assertEqual(code,200,listed)
            self.assertEqual(listed['directories'],[{'name':'directory','path':str(directory)}])
            token,_=f.grants.issue('all')
            code,denied=helper.request(f,'/api/image-workspace',{'action':'directories','directory':root,'actor':'web'},token=token)
            self.assertEqual(code,403,denied)

    def test_browses_and_imports_controller_archive_without_changing_source(self):
        with integration.protected_flashing() as f, tempfile.TemporaryDirectory() as root:
            root = Path(root)
            directory = self.directory(root)
            archive = root / 'system.ZIP'
            with zipfile.ZipFile(archive, 'w') as package:
                for path in directory.iterdir():
                    package.write(path, arcname=path.name)
            content = archive.read_bytes()
            (root / 'raw.img').write_bytes(b'raw')
            helper = integration.FlashHTTPIntegrationTests()
            code, listed = helper.request(f, '/api/image-workspace', {'action':'directories', 'directory':str(root)})
            self.assertEqual(code, 200, listed)
            self.assertEqual(listed['archives'], [{'name':archive.name, 'path':str(archive)}])
            self.assertEqual(listed['directories'], [{'name':directory.name, 'path':str(directory)}])

            body = {'action':'import', 'path':str(archive), 'request_id':'controller-import-test-001'}
            token, _ = f.grants.issue('all')
            code, denied = helper.request(f, '/api/image-workspace', dict(body, actor='web'), token=token)
            self.assertEqual(code, 403, denied)
            service = ImageWorkspaces(f.manager)
            self.assertEqual(service.request(dict(body, action='import-archive', path=str(directory))).status, 400)
            self.assertEqual(service.request(dict(body, path=str(root / 'raw.img'))).status, 400)
            code, started = helper.request(f, '/api/image-workspace', body)
            self.assertEqual(code, 202, started)
            final = helper.terminal(f, started['task']['id'])
            self.assertEqual(final['state'], 'completed', final)
            artifact = final['result']['artifact']
            self.assertEqual(artifact['sha256'], hashlib.sha256(content).hexdigest())
            self.assertEqual(artifact['name'], archive.name)
            with f.manager.artifacts.open(artifact['id']) as source:
                self.assertEqual(source.read(), content)
            self.assertEqual(archive.read_bytes(), content)

            archive.unlink()
            code, replay = helper.request(f, '/api/image-workspace', body)
            self.assertEqual(code, 202, replay)
            self.assertEqual(replay['task']['id'], final['id'])
            self.assertEqual(service.request(dict(body, path=str(root / 'other.zip'))).status, 409)

    def test_save_creates_new_sealed_revision_and_updates_checksum_manifest(self):
        with integration.protected_flashing() as f,tempfile.TemporaryDirectory() as root:
            directory=self.directory(root)
            member=directory/'partition_universal.json'
            digest=hashlib.sha256(member.read_bytes()).hexdigest()
            (directory/'SHA256SUMS').write_text(digest+'  partition_universal.json\n')
            service=ImageWorkspaces(f.manager)
            original=service.register(str(directory))['artifact']
            f.manager.flash_executor=LocalFlashExecutor(Path(f.manager.artifacts.root).parent/'flashing')
            f.manager.flash_executor.supports_image_preparation=False
            content=json.dumps({'format':'gpt','partitions':[{'name':'rootfs','offset':'2M','size':'-','image':'rootfs.ext4'}]})
            body={'action':'save','id':original['id'],'path':'partition_universal.json','text':content,'request_id':'directory-edit-once-001','revision':service.request({'id':original['id']}).payload['revision']}
            response=service.request(body)
            self.assertEqual(response.status,202,response.payload)
            final=integration.FlashHTTPIntegrationTests().terminal(f,response.payload['task']['id'])
            self.assertEqual(final['state'],'completed',final)
            self.assertNotEqual(final['result']['artifact']['id'],original['id'])
            self.assertEqual(service.request(body).payload['task']['id'], final['id'])
            self.assertEqual(service.request(dict(body,text='{}')).status,409)
            self.assertEqual(member.read_text(),content)
            self.assertTrue((directory/'SHA256SUMS').read_text().startswith(hashlib.sha256(content.encode()).hexdigest()))

    def test_choose_table_uses_sealed_geometry_and_keeps_original_directory(self):
        from seriald_core.image_tables import describe
        from seriald_core.image_preparation import for_manager
        with integration.protected_flashing() as f, tempfile.TemporaryDirectory() as tmp:
            root = self.directory(tmp)
            (root / 'fastboot.yaml').write_text(json.dumps({'support':['k1x','k1pro']}))
            default = 'partition_universal.json'
            alternate = 'partition_alternate.json'
            original = (root / default).read_bytes()
            (root / alternate).write_bytes(original.replace(b'1M', b'2M'))
            service = ImageWorkspaces(f.manager)
            row = service.register(str(root))['artifact']
            runner = LocalFlashExecutor(Path(f.manager.artifacts.root).parent / 'flashing')
            runner.supports_image_preparation = False
            f.manager.flash_executor = runner
            info = {'root_path':str(root), 'files':[], 'catalogue':[]}
            for file in root.iterdir():
                data = file.read_bytes()
                info['files'].append({'name':file.name, 'size':len(data), 'sha256':hashlib.sha256(data).hexdigest()})
            for name in (default, alternate):
                info['catalogue'].append({'name':name, 'format':'gpt', 'selected':name == default,
                    'partitions':[{'name':'rootfs','payload':'rootfs.ext4','available':True}]})
            describe(info)
            self.assertEqual(info['chip_families'], ['k1'])
            part = info['catalogue'][1]['partitions'][0]
            self.assertEqual((part['offset'], part['remaining'], part['payload_size']), (2**21, True, (root / 'rootfs.ext4').stat().st_size))
            body = {'action':'select-table','id':row['id'],'table_id':row['id'],'table':alternate,'path':default}
            latest = {'state':'completed','result':{'preparation':info}}
            with mock.patch.object(for_manager(f.manager), 'latest', return_value=latest):
                self.assertEqual(service.request(dict(body,table='partition_flash.json')).status,400)
                response = service.request(body)
                self.assertEqual(response.status,202,response.payload)
                final = integration.FlashHTTPIntegrationTests().terminal(f,response.payload['task']['id'])
            self.assertEqual(final['state'],'completed',final)
            updated = service.locate(final['result']['artifact']['id'])[2]
            sidecar = origin_path(f.manager.artifacts._path(final['result']['artifact']['id'], '.bin'))
            self.assertEqual(json.loads(sidecar.read_bytes())['table_choice'],
                             {'name':alternate,'destination':default,'original_id':row['id']})
            self.assertEqual((root / default).read_bytes(),original)
            self.assertEqual((updated / default).read_bytes(),(root / alternate).read_bytes())

    def test_archive_cache_is_copied_before_web_edit_and_original_stays_unchanged(self):
        with integration.protected_flashing() as f,tempfile.TemporaryDirectory() as root:
            helper=integration.FlashHTTPIntegrationTests();row=helper.image(f)
            runner=LocalFlashExecutor(Path(f.manager.artifacts.root).parent/'flashing');runner.supports_image_preparation=False
            f.manager.flash_executor=runner
            workspace=runner.image_workspace(row);workspace.mkdir(parents=True)
            directory=workspace/'payload';directory.mkdir();(directory/'fastboot.yaml').write_text('fixture original')
            (workspace/('prepared-'+row['sha256']+'.json')).write_text(json.dumps({'root_path':str(directory)}))
            service=ImageWorkspaces(f.manager)
            response=service.request({'action':'save','id':row['id'],'path':'fastboot.yaml','text':'fixture changed'})
            self.assertEqual(response.status,202,response.payload)
            final=helper.terminal(f,response.payload['task']['id'])
            self.assertEqual(final['state'],'completed',final)
            self.assertEqual((directory/'fastboot.yaml').read_text(),'fixture original')
            updated=service.locate(final['result']['artifact']['id'])[2]
            self.assertEqual((updated/'fastboot.yaml').read_text(),'fixture changed')
