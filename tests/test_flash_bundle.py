"""Two existing native plans under one transaction; no hardware."""
import copy
from dataclasses import dataclass
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from seriald_core.flash_bundle import assemble, check_bundle, plan_bundle, execute_bundle
from seriald_core.flash_runner import LocalFlashExecutor, FlashRunnerError


class BundleTests(unittest.TestCase):
    def fixture(self, root):
        runner = LocalFlashExecutor(root)
        binding = SimpleNamespace(profile_data={'storages':[]}, argv=('fake-executor',), serial='BOARD', endpoint='usb', device={}, max_expanded_bytes=1024**3, max_cache_bytes=1024**3, target_id='')
        firmware = dict(id='f'*24, name='Debian-13-Firmware-K3.tar.gz', path='/source/fw', size=10, sha256='a'*64)
        os = dict(id='e'*24, name='Debian-13-GNOME-K3.tar.gz', path='/source/os', size=20, sha256='b'*64)
        for label in ('Firmware','OS'):
            (runner.root / 'images' / ('a'*64 if label=='Firmware' else 'b'*64)).mkdir(parents=True)
        steps = []
        for label, artifact in [('Firmware',firmware),('OS',os)]:
            steps.append({'label':label,'result':{'workspace':str(runner.root / 'images' / ('a'*64 if label=='Firmware' else 'b'*64)),
                'execution_profile':{'storages':[{'id':'active','layout_source':'spacemit-nor' if label=='Firmware' else 'spacemit-gpt'}]},
                'plan':{'artifact':artifact,'mode':'full','verify':'required','ready':True,
                    'writes':[{'storage':'active','partition':'fsbl' if label=='Firmware' else 'rootfs','size':artifact['size']}],
                    'layout_digest':label,'write_layout':False,'catalogue':[{'format':'mtd' if label=='Firmware' else 'gpt','selected':True}]},
                'inspection':{'storages':[{'id':'active','physical_device':'scsi:0'}] if label=='OS' else [{'id':'active'}]},
                'payload_candidates':[], 'requires_prepare':False}})
        return runner,binding,firmware,os,steps

    def test_combined_preview_retains_both_native_ranges_and_sources(self):
        with tempfile.TemporaryDirectory() as root:
            runner,binding,fw,os,steps=self.fixture(root)
            with mock.patch.object(runner,'plan',side_effect=[s['result'] for s in steps]) as native:
                result=plan_bundle(runner,binding,os,fw,'full','required',lambda:None,target_storage='scsi:0')
            self.assertEqual([w['component'] for w in result['plan']['writes']],['Firmware','OS'])
            self.assertEqual(len(result['source_workspaces']),2)
            self.assertEqual(native.call_args_list[0].kwargs.get('target_storage'),None)
            self.assertEqual(native.call_args_list[1].kwargs['target_storage'],'scsi:0')
            self.assertEqual(result['plan']['artifact']['id'],os['id'])

    def test_executes_both_once_and_resets_only_after_last_component(self):
        with tempfile.TemporaryDirectory() as root:
            runner,binding,fw,os,steps=self.fixture(root)
            native_results=[{'ok':True,'write_result':'completed','verify_result':'verified','written_bytes':10},
                {'ok':True,'write_result':'completed','verify_result':'verified','written_bytes':20,'boot_result':'reset-command-acknowledged'}]
            with mock.patch.object(runner,'execution_binding',return_value=binding),mock.patch.object(runner,'_run',side_effect=native_results) as native:
                result=execute_bundle(runner,binding,assemble(steps,os)['plan'],None,lambda:None,reset_after=True)
            self.assertEqual(native.call_count,2)
            requests=[c.args[2] for c in native.call_args_list]
            self.assertNotIn('reset_after',requests[0]);self.assertTrue(requests[1]['reset_after'])
            self.assertEqual([r['plan']['artifact']['id'] for r in requests],[fw['id'],os['id']])
            self.assertEqual(result['written_bytes'],30)
            self.assertEqual(result['verify_result'],'verified')

    def test_k1_combined_check_uses_original_binding_for_each_storage(self):
        @dataclass
        class Binding:
            profile_data: dict

        with tempfile.TemporaryDirectory() as root:
            runner, _, fw, os, steps = self.fixture(root)
            detected = {'chip_family': 'k1', 'storages': [{'layout_source': 'spacemit-gpt'}]}
            prepared = Binding(steps[0]['result']['execution_profile'])
            system = steps[1]['result']
            system['execution_profile'].update(chip_family='k1', system_storage='active')
            system['inspection']['storages'] = [{'id':'active', 'capacity_source':'fastboot-blk-size'}]
            system['plan']['write_layout'] = True
            outputs = [step['result'] for step in steps]
            with mock.patch.object(runner, 'plan', side_effect=outputs):
                preview = plan_bundle(runner, Binding(detected), os, fw, 'full', 'if-supported', lambda: None)
            with mock.patch.object(runner, 'execution_binding') as rebind, \
                    mock.patch.object(runner, 'check_plan', side_effect=outputs):
                checked = check_bundle(runner, prepared, preview['plan'], lambda: None)
            self.assertEqual([call.args[0].profile_data for call in rebind.call_args_list], [detected, detected])
            self.assertEqual([row['component'] for row in checked['plan']['writes']], ['Firmware', 'OS'])
            self.assertTrue(checked['plan']['write_layout'])

    def test_unknown_second_component_is_not_replayed_or_reported_complete(self):
        with tempfile.TemporaryDirectory() as root:
            runner,binding,fw,os,steps=self.fixture(root)
            error=FlashRunnerError('lost worker',cleanup_confirmed=False,outcome={'write_result':'unknown'})
            with mock.patch.object(runner,'execution_binding',return_value=binding),mock.patch.object(runner,'_run',side_effect=[
                    {'ok':True,'write_result':'completed','verify_result':'verified','written_bytes':10},error]) as native:
                with self.assertRaises(FlashRunnerError):execute_bundle(runner,binding,assemble(steps,os)['plan'],None,lambda:None,reset_after=True)
            self.assertEqual(native.call_count,2)
            self.assertEqual(error.outcome['write_result'],'unknown')
            self.assertEqual(error.outcome['written_bytes'],10)
            self.assertFalse(error.cleanup_confirmed)

    def test_first_component_failure_stops_before_os_or_reset(self):
        with tempfile.TemporaryDirectory() as root:
            runner,binding,fw,os,steps=self.fixture(root)
            with mock.patch.object(runner,'execution_binding',return_value=binding),mock.patch.object(runner,'_run',side_effect=FlashRunnerError('failed')) as native:
                with self.assertRaises(FlashRunnerError):execute_bundle(runner,binding,assemble(steps,os)['plan'],None,lambda:None,reset_after=True)
            self.assertEqual(native.call_count,1)
            self.assertNotIn('reset_after',native.call_args.args[2])


class BundleHTTPTests(unittest.TestCase):
    def setup(self, fixture):
        import dataclasses
        original = fixture.executor.plan
        def plan(binding, artifact, *args, **kwargs):
            table = kwargs.pop('package_scope', None)
            result=original(binding,artifact,*args,**kwargs)
            firmware=(table != 'system' if table else 'Firmware' in artifact['name'])
            profile=copy.deepcopy(binding.profile_data)
            profile['storages']=[{'id':'active','layout_source':'spacemit-nor' if firmware else 'spacemit-gpt'}]
            result['execution_profile']=profile
            result['workspace']=str(fixture.manager.artifacts.root.parent/'flashing'/'images'/artifact['sha256'])
            result['plan']['catalogue']=[{'format':'mtd' if firmware else 'gpt','selected':True}]
            result['inspection']['storages'][0]['physical_device']='' if firmware else 'scsi:0'
            return result
        fixture.executor.plan=plan
        fixture.executor.execution_binding=lambda binding,profile,workspace,check=lambda:None: dataclasses.replace(binding,profile_data=profile)

    def test_one_public_task_claims_both_images_and_executes_firmware_then_os(self):
        import test_flash_integration as integration
        helper=integration.FlashHTTPIntegrationTests()
        with integration.protected_flashing() as f:
            self.setup(f)
            os=helper.image(f)
            row=f.manager.artifacts.create('upload','Debian-13-Firmware-K3.tar.gz',4,'web','FLASH-A')
            f.manager.artifacts.append(row['id'],0,b'boot');fw=f.manager.artifacts.seal(row['id'])
            code,result=helper.request(f,'/api/flash-plan',{'key':'offline-board','serial':'FLASH-A','artifact_id':os['id'],'firmware_artifact_id':fw['id'],'mode':'full','verify':'required'})
            self.assertEqual(code,200,result)
            self.assertEqual([c['label'] for c in result['plan']['components']],['Firmware','OS'])
            self.assertNotIn('steps',result['plan'])
            code,started=helper.request(f,'/api/task-start',{'kind':'flash','key':'offline-board','serial':'FLASH-A',
                'plan_id':result['plan']['id'],'binding_revision':7,'request_id':'combined-flash-request-001'})
            self.assertEqual(code,202,started)
            final=helper.terminal(f,started['task']['id'])
            self.assertEqual(final['state'],'completed',final)
            self.assertEqual([item[0]['artifact']['id'] for item in f.executor.executions],[fw['id'],os['id']])
            for artifact in (os,fw):self.assertEqual(f.manager.artifacts.get(artifact['id'])['state'],'ready')
            self.assertEqual(final['result']['write_result'],'completed')

    def test_embedded_nor_and_os_share_one_artifact_through_confirmation(self):
        import test_flash_integration as integration
        helper = integration.FlashHTTPIntegrationTests()
        with integration.protected_flashing() as f:
            self.setup(f)
            image = helper.image(f)
            scope = dict(key='offline-board', serial='FLASH-A', artifact_id=image['id'],
                         mode='full', verify='none', firmware_table='partition_4M.json')
            code, response = helper.request(f, '/api/flash-plan', scope)
            self.assertEqual(code, 200, response)
            plan = response['plan']
            self.assertEqual(plan['firmware_table'], scope['firmware_table'])
            self.assertEqual([(c['label'], c['id']) for c in plan['components']],
                             [('Firmware', image['id']), ('OS', image['id'])])
            code, started = helper.request(f, '/api/task-start', dict(kind='flash', key='offline-board',
                serial='FLASH-A', plan_id=plan['id'], binding_revision=7, request_id='same-archive-bundle'))
            self.assertEqual(code, 202, started)
            self.assertEqual(helper.terminal(f, started['task']['id'])['state'], 'completed')
            self.assertEqual([p['artifact']['id'] for p, _ in f.executor.executions], [image['id']]*2)
            self.assertEqual(f.manager.artifacts.get(image['id'])['state'], 'ready')
