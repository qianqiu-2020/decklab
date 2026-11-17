"""Directory owner operations use ordinary files and isolated task registries."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

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
