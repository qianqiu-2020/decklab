"""Run an existing TitanX case on an explicitly commissioned, co-located slot."""
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote

from .board_cases import adb_binding
from .board_maintenance import maintenance_for
from .flash_store import FlashStore, validate_request
from .tasks import TaskError
from .team_cases import TeamCases


def mapping(serial):
    path = os.environ.get('SERIALD_TEST_PLATFORM_BOARDS_FILE')
    if not path:
        return None
    try:
        with Path(path).open(encoding='utf-8') as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError
        boards = json.loads(raw)
        if not isinstance(boards, dict):
            raise ValueError
        codes, slots = set(), set()
        for row in boards.values():
            code, slot = row['code'], row['device_id']
            if (not isinstance(code, str) or not code.strip() or len(code) > 128
                    or not isinstance(slot, str) or not re.fullmatch(r'[\w.-]+:[0-9]+', slot)
                    or code in codes or slot in slots):
                raise ValueError
            codes.add(code)
            slots.add(slot)
        return boards.get(serial)
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise TaskError('控制机的团队测试槽位配置无效', 503) from error


def match_board(platform, manager, board, serial, generation, configured):
    if board.serial != serial or board.connection_id != generation:
        raise TaskError('当前板卡连接已变化', 409)
    binding = adb_binding(manager, board)
    code, slot = configured['code'], configured['device_id']
    data = platform.request('/deviceBoard/listVO', {'deviceCode': code, 'deviceId': slot, 'pageNo': 1, 'pageSize': 2})
    rows = data['list']
    if (len(rows) != 1 or rows[0]['code'] != code or rows[0]['deviceId'] != slot
            or data['total'] != 1 or rows[0]['serial'] != 'usb:' + binding['usb_port']
            or binding['serial'] != serial or binding['connection_id'] != generation):
        raise TaskError('TitanX 槽位与本板当前的 USB 接线不一致', 409)
    row = rows[0]
    if row.get('state') != 'idle' or row.get('claimedByUserId') is not None:
        raise TaskError('TitanX 槽位当前不可用，请查看平台占用', 409)
    return row


def case_plan(platform, case_id):
    case = platform.browse({'view': ['detail'], 'id': [str(case_id)]})['case']
    if not case['automated'] or not case['reviewed']:
        raise TaskError('此用例尚未完成自动化与评审，无法下发', 409)
    return case


def verified_records(platform, tid, code, remote_case, repeats):
    jobs = platform.request('/taskInfo/listJob/page', {'taskId': tid, 'pageNo': 1, 'pageSize': 2})
    if jobs['total'] != 1 or len(jobs['list']) != 1:
        raise TaskError('平台没有返回唯一的本板执行记录', 502)
    job = jobs['list'][0]
    if job['taskId'] != tid or job['deviceId'] != code:
        raise TaskError('平台执行记录与计划设备不符', 502)
    records = platform.request('/task/record/job/' + quote(str(job['id']), safe=''))
    seen, rounds = set(), []
    for record in records:
        if (record['jobId'] != job['id'] or record['taskCaseId'] != remote_case
                or type(record['index']) is not int or record['index'] in seen):
            raise TaskError('平台逐轮回执与原执行不符', 502)
        seen.add(record['index'])
        status = {1: 'passed', -1: 'failed', -2: 'cancelled'}.get(record['status'], 'uncertain')
        rounds.append({'round': record['index'], 'status': status})
    if len(rounds) != repeats:
        raise TaskError('平台逐轮回执数量与计划不符', 502)
    return sorted(rounds, key=lambda r: r['round'])


def start(manager, device, payload, actor):
    if actor != 'web':
        raise TaskError('团队用例仅限主访问身份', 403)
    if set(payload) - {'kind', 'source', 'case', 'rounds', 'key', 'serial', 'connection_id', 'request_id', 'client_id', 'actor'}:
        raise TaskError('未知团队用例参数')
    serial, generation = payload['serial'], payload['connection_id']
    case_id, repeats = payload.get('case'), payload.get('rounds', 1)
    if type(case_id) != int or case_id <= 0 or type(repeats) != int or not 1 <= repeats <= 100:
        raise TaskError('请选择团队用例并填写 1 到 100 轮')
    request_id = payload.get('request_id')
    digest = manager.tasks.store.request_digest({'source':'titanx', 'serial':serial, 'generation':generation,
                                                 'case':case_id, 'rounds':repeats})
    validate_request(request_id, digest)
    previous = manager.tasks.store.find_request(actor, request_id, digest)
    if previous is not None:
        return manager.tasks.store.get(previous)
    platform, configured = TeamCases.configured(), mapping(serial)
    if not platform or not configured:
        raise TaskError('本板尚未关联 TitanX 设备编号和节点槽位', 409)
    case = case_plan(platform, case_id)
    coordinator = maintenance_for(manager)
    code = configured['code']

    def run(context):
        running = False
        result = {'source':'titanx', 'case_name':case['name'], 'platform_device':code,
                  'platform_plan':'DeckLab-' + context.id}
        try:
            row = match_board(platform, manager, device, serial, generation, configured)
            owner = platform.request('/userInfo/detail')['id']
            info = {'name':result['platform_plan'], 'owner':owner, 'follower':'', 'imageUrl':'',
                    'restartDeviceBeforeTest':False, 'aiAutoHeal':False, 'debugMode':False,
                    'autoCommitBug':False, 'projectLabel':row['project'], 'deviceType':'', 'timeoutMinutes':60}
            context.store.update(context.id, result=result)
            context.progress(0, repeats, 'creating-team-plan')
            saved = platform.request('/taskInfo/saveOrUpdate', {'taskInfo':info, 'taskDevices':[{'deviceId':code}],
                                      'taskCases':[{'caseId':case_id, 'executeOrder':0, 'times':repeats}]})
            tid = saved['id']
            if type(tid) != int or tid <= 0:
                raise TaskError('平台未返回可核对的任务编号', 502)
            result['platform_task_id'] = tid
            context.store.update(context.id, result=result)
            devices = platform.request(f'/taskInfo/taskDevice/list/{tid}')
            cases = platform.request(f'/taskInfo/taskCase/list/{tid}')
            actual = platform.request(f'/taskInfo/{tid}')
            if (actual.get('id') != tid or actual.get('name') != info['name'] or actual.get('status') != 0
                    or any(actual.get(key) is not False for key in ('aiAutoHeal','restartDeviceBeforeTest','debugMode','autoCommitBug'))
                    or actual.get('imageUrl') or [d['deviceId'] for d in devices] != [code]
                    or [(c['caseId'], c['times']) for c in cases] != [(case_id, repeats)]):
                raise TaskError('平台保存的计划与选择不一致', 409)
            match_board(platform, manager, device, serial, generation, configured)
            context.issued()
            running = True
            platform.request(f'/taskInfo/start?taskId={tid}')
            deadline = time.monotonic() + 65 * 60
            cancel_sent = False
            while time.monotonic() < deadline:
                if context.cancelled.is_set() and not cancel_sent:
                    cancel_sent = True
                    platform.request(f'/taskInfo/cancelTask/{tid}', {})
                state = platform.request(f'/taskInfo/{tid}')
                if state.get('id') != tid:
                    raise TaskError('平台返回了其他计划', 502)
                if state['status'] in (-3, -2, 2):
                    break
                if cancel_sent:
                    time.sleep(3)
                else:
                    context.cancelled.wait(3)
            else:
                raise TaskError('未能确认团队任务结束，请查看原平台计划', 504)
            running = False
            if state['status'] != 2:
                raise TaskError('团队任务在平台端已取消或中断', 409)
            rounds = verified_records(platform, tid, code, cases[0]['id'], repeats)
            result.update(recent_rounds=rounds, completed_rounds=sum(r['status'] == 'passed' for r in rounds))
            context.store.update(context.id, result=result)
            if any(r['status'] != 'passed' for r in rounds):
                raise TaskError('团队测试包含失败、取消或未执行的轮次', 409)
            context.progress(repeats, repeats, 'team-test-completed')
            return result
        finally:
            if running:
                coordinator.keep_recovery(context.id)
                FlashStore(manager.store).update_details(context.id, {'recovery_required':True})
            else:
                coordinator.release(context.id, confirmed=True)

    return manager.tasks.start_board('board-test', actor, payload.get('client_id', ''), serial, run,
                                     request_id=request_id, input_digest=digest, maintenance=coordinator,
                                     connection_id=generation, tracks_outcome=True)
