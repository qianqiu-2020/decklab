"""One board task, two existing sealed native plans; no new device protocol."""
import hashlib
import json

from .flash_runner import FlashRunnerError
from .tasks import TaskError


def assemble(steps, artifact):
    writes, warnings, erases, metadata, layouts, roots = [], [], [], [], [], set()
    for step in steps:
        result, label = step['result'], step['label']
        plan = result['plan']
        writes.extend(dict(value, storage=('NOR' if label == 'Firmware' else value.get('storage')),
                           component=label, image_name=plan.get('artifact', {}).get('name', '')) for value in plan.get('writes', []))
        erases.extend(dict(value, storage=('NOR' if label == 'Firmware' else value['storage']),
                           component=label) for value in plan.get('erase_ranges', []))
        layouts.extend(dict(value, id=('NOR' if label == 'Firmware' else value['id']), component=label)
                       for value in (result.get('inspection') or {}).get('storages', []))
        warnings.extend(label + ': ' + value for value in plan.get('warnings', []))
        metadata.extend(result.get('payload_metadata') or [])
        roots.add(result['workspace'])
        roots.update(result.get('source_workspaces') or [])
    ready = all(step['result']['plan'].get('ready') is not False for step in steps)
    plan = {'version':1, 'image_type':'bundle', 'artifact':artifact, 'mode':'full',
        'verify':steps[0]['result']['plan'].get('verify', 'if-supported'), 'ready':ready,
        'writes':writes, 'write_layout':any(s['result']['plan'].get('write_layout') for s in steps),
        'erase_ranges':erases, 'warnings':warnings, 'steps':steps, 'storage_layouts':layouts,
        'ram_loaders':steps[0]['result']['plan'].get('ram_loaders', []),
        'components':[dict({key:step['result']['plan'].get('artifact',{}).get(key) for key in ('id','name','size','sha256')}, label=step['label'],ready=step['result']['plan'].get('ready') is not False) for step in steps],
        'pending_checks':sorted({value for step in steps for value in step['result']['plan'].get('pending_checks',[])}),
        'confirm_after_prepare':any(s['result']['plan'].get('confirm_after_prepare') for s in steps)}
    plan['layout_digest'] = hashlib.sha256(json.dumps([s['result']['plan'].get('layout_digest') for s in steps]).encode()).hexdigest()
    first = steps[0]['result']
    return {'plan':plan, 'workspace':first['workspace'], 'source_workspaces':sorted(roots),
        'execution_profile':first.get('execution_profile'), 'payload_candidates':[], 'payload_metadata':metadata,
        'inspection':steps[-1]['result'].get('inspection'),
        'requires_prepare':not ready and any(s['result'].get('requires_prepare') for s in steps)}


def plan_bundle(executor, binding, artifact, firmware, mode, verify, check, **options):
    if mode != 'full' or artifact['id'] == firmware['id']:
        raise TaskError('固件＋系统镜像仅支持整包刷写，且必须选择两个不同镜像', 409)
    steps = []
    # Both native plans are previewed before one task may execute either one.
    for label, source in [('Firmware',firmware), ('OS',artifact)]:
        arguments = dict(options)
        if label == 'Firmware':
            arguments.pop('target_storage', None)
        result = executor.plan(binding, source, 'full', [], verify, check, **arguments)
        storages = (result.get('execution_profile') or binding.profile_data).get('storages') or []
        if label == 'Firmware' and not any(s.get('layout_source') == 'spacemit-nor' for s in storages):
            raise TaskError('配套 Firmware 镜像未提供 NOR 固件写入范围', 409)
        if label == 'OS' and not any(table.get('format') == 'gpt' and table.get('selected') for table in result['plan'].get('catalogue', [])):
            raise TaskError('OS 镜像必须包含刷机配置选中的 GPT 分区表', 409)
        if label == 'OS' and not any(s.get('physical_device') for s in (result.get('inspection') or {}).get('storages', [])) and not result.get('requires_prepare'):
            raise TaskError('OS 镜像未提供已核对的系统盘写入范围', 409)
        steps.append({'label':label, 'result':result})
    return assemble(steps, artifact)


def check_bundle(executor, binding, plan, check, previous_device=None):
    steps = []
    for step in plan['steps']:
        original = step['result']
        selected = executor.execution_binding(binding, original['execution_profile'], original['workspace'], check) if original.get('execution_profile') else binding
        result = executor.check_plan(selected, original['plan'], original.get('payload_candidates', []), check, **({'previous_device':previous_device} if previous_device is not None else {}))
        result.update(workspace=original['workspace'], execution_profile=original.get('execution_profile'),
                      source_workspaces=original.get('source_workspaces', []))
        steps.append({'label':step['label'], 'result':result})
    return assemble(steps, plan['artifact'])


def execute_bundle(executor, binding, plan, on_event, check, **options):
    steps = plan.get('steps')
    if (not isinstance(steps, list) or len(steps) != 2
            or [step.get('label') for step in steps] != ['Firmware','OS']):
        raise TaskError('invalid combined flash plan', 409)
    output, completed = {'ok':True, 'write_result':'completed', 'boot_result':'not-requested',
                         'written_bytes':0, 'verified_bytes':0, 'readback_bytes':0}, []
    for index, step in enumerate(steps):
        value = step['result']
        selected = executor.execution_binding(binding, value['execution_profile'], value['workspace'], check) if value.get('execution_profile') else binding
        arguments = dict(options, workspace=value['workspace'], reset_after=options.get('reset_after', False) and index == len(steps)-1)
        if index:
            arguments.pop('nor_baseline', None)
        def event(message):
            if on_event:
                on_event(dict(message, component=step['label']))
        try:
            result = executor.execute(selected, value['plan'], event, check, **arguments)
            if result.get('ok') is not True or result.get('write_result') != 'completed':
                raise FlashRunnerError('combined flash component did not complete')
        except BaseException as error:
            if completed:
                previous = getattr(error, 'outcome', {}) or {}
                error.outcome = dict(previous, write_result='unknown' if previous.get('write_result') == 'unknown' else 'partial',
                                     written_bytes=output['written_bytes'] + previous.get('written_bytes', 0))
            raise
        completed.append(result)
        for key in ('written_bytes','verified_bytes','readback_bytes'):
            output[key] += result.get(key, 0)
        for key in ('nor_before','nor_after'):
            if result.get(key):
                output[key] = result[key]
        if index == len(steps)-1:
            output['boot_result'] = result.get('boot_result', 'not-requested')
    verification = [item.get('verify_result', 'unavailable') for item in completed]
    output['verify_result'] = 'skipped' if 'skipped' in verification else 'verified' if all(v == 'verified' for v in verification) else 'not-requested' if all(v == 'not-requested' for v in verification) else 'unavailable'
    return output
