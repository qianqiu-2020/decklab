"""Run an existing TitanX case on an explicitly commissioned, co-located slot."""
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, quote, urlsplit

from .board_cases import adb_binding
from .board_maintenance import maintenance_for
from .flash_store import FlashStore, validate_request
from .tasks import TaskError
from .team_cases import TeamCases
from .titanx_enroll import archive_matches
from .titanx_workbench import read_config


def mapping(serial):
    path = os.environ.get('DECKLAB_TEST_PLATFORM_BOARDS_FILE')
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


def board_mapping(platform, serial):
    try:
        station = read_config()
    except FileNotFoundError:
        station = {}
    if station.get('lab_id') and station.get('api_url'):
        endpoint = urlsplit(station['api_url'])
        if (endpoint.hostname, endpoint.port or 443) == (platform.host, platform.port or 443):
            slots = station.get('slots')
            if slots is None:
                from .config import BASE
                try:
                    slots = json.loads((Path(BASE) / 'titanx-worker.json').read_text())['slots']
                except FileNotFoundError:
                    slots = {}
            for key, selected in slots.items():
                if selected == serial:
                    if key in station.get('paused_slots', []):
                        raise TaskError('此板的 TitanX 接入已暂停，请在用例库的工位窗口恢复后执行', 409)
                    slot = int(key)
                    device_id = f"{station['node_id']}:{slot}"
                    com = station.get('first_com', 1) + slot - 1
                    nodes = platform.request('/deviceNode/listVO', {'namespaceId': station['lab_id']})
                    nodes = [n for n in nodes if n.get('originNodeId') == station['node_id']]
                    if len(nodes) != 1:
                        return None
                    node = nodes[0]
                    matches = [b for b in node.get('deviceBoards') or []
                               if archive_matches(b, node['id'], device_id, serial, slot, com)]
                    if len(matches) != 1 or not matches[0].get('code'):
                        return None
                    return {'code': matches[0]['code'], 'device_id': device_id,
                            'lab': station['lab_id'], 'node': node['id'], 'com': com}
    return mapping(serial) or archived_mapping(platform, serial)


def archived_mapping(platform, serial):
    data = platform.request('/deviceBoard/listVO', {'decklabSerial': serial, 'pageNo': 1, 'pageSize': 2})
    rows = data['list']
    if data['total'] != 1 or len(rows) != 1:
        return None
    row = rows[0]
    code, slot = row.get('code'), row.get('deviceId')
    if (row.get('decklabSerial') != serial or type(row.get('decklabSlot')) is not int
            or not isinstance(code, str) or not code or not isinstance(slot, str)
            or not re.fullmatch(r'[\w.-]+:[0-9]+', slot)
            or int(slot.rsplit(':', 1)[1]) != row['decklabSlot']):
        return None
    return {'code': code, 'device_id': slot, 'require_decklab': True}


def uses_decklab_reservation(row, serial, configured):
    reported_serial, reported_slot = row.get('decklabSerial'), row.get('decklabSlot')
    if 'lab' in configured:
        slot = int(configured['device_id'].rsplit(':', 1)[1])
        if not archive_matches(row, configured['node'], configured['device_id'], serial, slot, configured['com']):
            raise TaskError('TitanX 节点、板号或 COM 与已登记工位不符', 409)
        return True
    if reported_serial is None and reported_slot is None and not configured.get('require_decklab'):
        return False
    if (reported_serial != serial or type(reported_slot) is not int
            or reported_slot != int(configured['device_id'].rsplit(':', 1)[1])):
        raise TaskError('TitanX 的 DeckLab 板号或槽位与本板不符', 409)
    return True


def match_board(platform, manager, board, serial, generation, configured):
    if board.serial != serial or board.connection_id != generation:
        raise TaskError('当前板卡连接已变化', 409)
    binding = adb_binding(manager, board)
    code, slot = configured['code'], configured['device_id']
    query = {'deviceCode': code, 'deviceId': slot, 'pageNo': 1, 'pageSize': 2}
    if 'lab' in configured:
        query.update(namespaceId=configured['lab'], nodeIds=[configured['node']])
    data = platform.request('/deviceBoard/listVO', query)
    rows = data['list']
    if (len(rows) != 1 or rows[0]['code'] != code or rows[0]['deviceId'] != slot
            or data['total'] != 1 or rows[0]['serial'] != 'usb:' + binding['usb_port']
            or binding['serial'] != serial or binding['connection_id'] != generation):
        raise TaskError('TitanX 槽位与本板当前的 USB 接线不一致', 409)
    row = rows[0]
    # TitanX excludes noDispatch boards from automatic matching. An explicitly
    # pinned plan on a commissioned station uses TitanX's pinned-device policy.
    if row.get('noDispatch') is True and 'lab' not in configured:
        raise TaskError('TitanX 已禁止调度此工位，请在设备档案中允许调度后再试', 409)
    if row.get('state') != 'idle' or row.get('claimedByUserId') is not None:
        raise TaskError('TitanX 槽位当前不可用，请查看平台占用', 409)
    return row


def retry_reference(value):
    if not isinstance(value, dict) or set(value) != {'plan_id', 'job_id', 'record_id'}:
        raise TaskError('复测链接缺少原计划、作业或失败记录')
    for key in ('plan_id', 'record_id'):
        item = value[key]
        if not isinstance(item, str) or not re.fullmatch(r'[1-9][0-9]{0,17}', item):
            raise TaskError('复测链接中的计划或失败记录编号无效')
    job = value['job_id']
    if not isinstance(job, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', job):
        raise TaskError('复测链接中的作业编号无效')
    return dict(value)


def retry_context(platform, serial, reference):
    ref = retry_reference(reference)
    plan_id = int(ref['plan_id'])
    rows = platform.request('/task/record/job/' + quote(ref['job_id'], safe=''))
    if not isinstance(rows, list):
        raise TaskError('TitanX 未返回原作业记录', 502)
    matches = [row for row in rows if isinstance(row, dict) and str(row.get('id')) == ref['record_id']]
    if len(matches) != 1:
        raise TaskError('未找到唯一的 TitanX 原失败记录', 409)
    row = matches[0]
    try:
        board = parse_qs(urlsplit(row.get('decklabUrl') or '').fragment).get('device')
    except (AttributeError, ValueError, TypeError):
        board = None
    task_case, round_number = row.get('taskCaseId'), row.get('index')
    if (row.get('jobId') != ref['job_id'] or row.get('taskId') != plan_id
            or row.get('status') != -1 or board != [serial]
            or type(task_case) is not int or task_case <= 0
            or type(round_number) is not int or round_number < 1):
        raise TaskError('原失败记录与这块板或本次复测不符', 409)
    plan = platform.request(f'/taskInfo/{plan_id}')
    if not isinstance(plan, dict) or plan.get('id') != plan_id or plan.get('status') not in (2, -2, -3):
        raise TaskError('请等 TitanX 原计划结束后再复测', 409)
    cases = platform.request(f'/taskInfo/taskCase/list/{plan_id}')
    if not isinstance(cases, list):
        raise TaskError('TitanX 未返回原计划用例', 502)
    selected = [case.get('caseId') for case in cases if isinstance(case, dict) and case.get('id') == task_case]
    if len(selected) != 1 or type(selected[0]) is not int or selected[0] <= 0:
        raise TaskError('原失败记录未关联到唯一的团队用例', 409)
    return {**ref, 'round': round_number, 'case_id': selected[0]}


def case_plan(platform, case_id):
    case = platform.browse({'view': ['detail'], 'id': [str(case_id)]})['case']
    if not case['runnable']:
        reason = ('此用例尚未自动化。' if not case['automated'] else
                  '此用例评审未通过。' if case['review_status'] == 3 else '无法确认用例的评审状态。')
        raise TaskError(reason, 409)
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
        rounds.append({'round': record['index'], 'status': status, 'job_id': job['id'],
                       'task_case_id': remote_case, 'record_id': record.get('id'),
                       'started_at': record.get('startTime'), 'ended_at': record.get('endTime')})
    if len(rounds) != repeats:
        raise TaskError('平台逐轮回执数量与计划不符', 502)
    return sorted(rounds, key=lambda r: r['round'])


def shell_step(command):
    if command.strip() == 'echo __CH_PROBE__':
        return 'probe'
    if command.strip() == 'systemctl start NetworkManager' or command.startswith('ping -c '):
        return 'network'
    if command.startswith("file='/tmp/shell-cases/"):
        return 'upload'
    if command.startswith("mkdir -p '/tmp/shell-cases'"):
        return 'prepare'
    if command.startswith("sh -n '/tmp/shell-cases/"):
        return 'syntax'
    if command.startswith('CASE_CODE=') and ' CASE_RUN_ID=' in command:
        return 'execute'
    if command.startswith("rm -f '/tmp/shell-cases/"):
        return 'cleanup'
    return 'command'


def round_evidence(store, device, platform, task_id, number, actor='web'):
    from .queries import ArchiveQueries
    task = store.get(task_id)
    result = task.get('result') or {}
    if (task['kind'] not in ('board-test', 'team-plan') or result.get('source') != 'titanx'
            or task['board_targets'] != [device.serial] or actor != 'web' and task['actor'] != actor):
        raise TaskError('该板卡没有此团队任务', 404)
    row = next((r for r in result.get('recent_rounds', []) if r['round'] == number), None)
    if not row:
        raise TaskError('该任务尚无此轮回执', 404)
    code, url = result.get('case_code'), result.get('script_url')
    if not row.get('job_id'):
        # Historical tasks stored only round/status. Resolve their original plan.
        if not platform:
            raise TaskError('读取历史轮次需要原 TitanX 平台连接', 503)
        tid = result['platform_task_id']
        plan = platform.request(f'/taskInfo/{tid}')
        cases = platform.request(f'/taskInfo/taskCase/list/{tid}')
        if (plan.get('id') != tid or plan.get('name') != 'DeckLab-' + task_id
                or len(cases) != 1 or cases[0].get('taskId') != tid):
            raise TaskError('无法核对原计划和用例', 409)
        case = cases[0]
        records = verified_records(platform, tid, result['platform_device'], case['id'], case['times'])
        row = next((r for r in records if r['round'] == number and r['status'] == row['status']), None)
        if not row:
            raise TaskError('原平台轮次已变化', 409)
        code = case['caseCode']
        try:
            source = platform.request('/caseInfo/' + str(case['caseId']))
        except TaskError:
            source = {}
        if isinstance(source, dict) and source.get('id') == case['caseId'] and source.get('caseCode') == code:
            url = source.get('scriptUrl')
    job = row['job_id']
    evidence = {'commands': [], 'round': row, 'script_url': url, 'script': {}, 'warnings': []}
    paths, reservations = {}, set()
    kinds = ('adb-command', 'ssh-command', 'serial-command')

    def add_receipts(kind, entries, shared=False):
        if len(entries) == 20:
            evidence['warnings'].append('每个通道最多显示最近 20 条命令。')
        for entry in entries:
            command_task = store.get(entry['id'])
            receipt = command_task['result'][kind.replace('-command', '_receipt')]
            command = receipt['command']
            evidence['commands'].append({**entry, 'kind': kind, 'command': command, 'phase': shell_step(command),
                'scope': 'reservation' if shared else 'round' if entry['round'] else 'job',
                'outcome': command_task.get('outcome'), 'error': command_task.get('error'),
                'identity_channel': receipt.get('identity_channel'),
                **{key: receipt.get(key) for key in ('stdout', 'stderr', 'stdout_truncated_bytes', 'stderr_truncated_bytes')}})
            if not shared:
                if receipt.get('reservation_id'):
                    reservations.add((receipt['reservation_id'], command_task['actor']))
                path = re.search(r"'(/tmp/shell-cases/[A-Za-z0-9_.-]+\.sh)'", command)
                if path and entry['round'] == number:
                    paths[path[1]] = command_task['actor']

    for kind in kinds:
        add_receipts(kind, store.titanx_case_receipts(device.serial, job, code, number, kind))
    for lease, owner in reservations:
        for kind in kinds:
            add_receipts(kind, store.titanx_case_receipts(device.serial, job, code, number, kind,
                reservation=(lease, owner, task['created'], task['updated'])), shared=True)
    known = {step['id'] for step in evidence['commands']}
    for path, owner in paths.items():
        script = store.case_script_upload(device.serial, owner, path, task['created'], task['updated'])
        if script:
            upload = script['upload']
            if upload['id'] not in known:
                evidence['commands'].append({**upload, 'round': number, 'phase':'upload'})
                known.add(upload['id'])
            if not evidence['script'] or upload['created'] > evidence['script']['upload']['created']:
                evidence['script'] = script
    evidence['commands'].sort(key=lambda r: (r['created'], r['id']))
    try:
        begin, end = (time.mktime(time.strptime(row.get(key), '%Y-%m-%d %H:%M:%S'))
                      for key in ('started_at', 'ended_at'))
        if not task['created'] - 5 <= begin <= end <= task['updated'] + 5:
            raise ValueError
        evidence['serial'] = ArchiveQueries.serial_interval(device, max(begin, end - 300), end)
        if end - begin > 300:
            evidence['warnings'].append('串口预览显示本轮最后 5 分钟。')
    except (OSError, ValueError, TypeError):
        evidence['warnings'].append('无法读取原轮次时段的本板串口归档。')
    if platform:
        try:
            logs = platform.request('/deviceLog/testJobLog', {'jobId': job, 'caseOnlyId': row['task_case_id'],
                                    'round': number, 'size': 101, 'order': 0})
            if not isinstance(logs, list) or any(not isinstance(log, dict) or log.get('jobId') != job
                    or str(log.get('caseOnlyId')) != str(row['task_case_id']) or log.get('round') != number for log in logs):
                raise TaskError('平台日志与本轮不符', 502)
            evidence['logs'] = [str(log.get('log') or '')[:8192] for log in logs[:100]]
            if len(logs) > 100 or any(len(str(log.get('log') or '')) > 8192 for log in logs):
                evidence['warnings'].append('平台日志预览最多显示前 100 条，每条最多 8192 字符。')
        except TaskError as error:
            evidence['warnings'].append(str(error))
    return evidence


def start(manager, device, payload, actor):
    if actor != 'web' and (not isinstance(actor, str) or not re.fullmatch(r'shared-agent/[0-9a-f]{16}', actor) or 'retry' in payload):
        raise TaskError('运行团队用例需要主访问身份或已登记板卡的授权', 403)
    if set(payload) - {'kind', 'source', 'case', 'rounds', 'key', 'serial', 'connection_id', 'request_id', 'client_id', 'actor', 'retry'}:
        raise TaskError('未知团队用例参数')
    serial, generation = payload['serial'], payload['connection_id']
    case_id, repeats = payload.get('case'), payload.get('rounds', 1)
    if type(case_id) != int or case_id <= 0 or type(repeats) != int or not 1 <= repeats <= 100:
        raise TaskError('请选择团队用例并填写 1 到 100 轮')
    request_id = payload.get('request_id')
    identity = {'source':'titanx', 'serial':serial, 'generation':generation, 'case':case_id, 'rounds':repeats}
    reference = retry_reference(payload['retry']) if 'retry' in payload else None
    if reference is not None:
        identity['retry'] = reference
    digest = manager.tasks.store.request_digest(identity)
    validate_request(request_id, digest)
    previous = manager.tasks.store.find_request(actor, request_id, digest)
    if previous is not None:
        return manager.tasks.store.get(previous)
    platform = TeamCases.configured()
    configured = board_mapping(platform, serial) if platform else None
    if not configured:
        raise TaskError('本板尚未关联 TitanX 设备编号和节点槽位', 409)
    origin = retry_context(platform, serial, reference) if reference is not None else None
    if origin is not None and origin['case_id'] != case_id:
        raise TaskError('所选用例与 TitanX 原失败用例不一致', 409)
    case = case_plan(platform, case_id)
    coordinator = maintenance_for(manager)
    code = configured['code']
    initial = match_board(platform, manager, device, serial, generation, configured)
    delegated = uses_decklab_reservation(initial, serial, configured)

    def current_board():
        row = match_board(platform, manager, device, serial, generation, configured)
        if uses_decklab_reservation(row, serial, configured) != delegated:
            raise TaskError('TitanX 板卡托管方式已变化，请重新选择设备', 409)
        return row

    def run(context):
        running = False
        result = {'source':'titanx', 'case_id':case_id, 'case_code': case.get('code'), 'script_url': case.get('script_url'), 'case_name':case['name'], 'platform_device':code,
                  'platform_plan':'DeckLab-' + context.id}
        try:
            row = current_board()
            owner = platform.request('/userInfo/detail')['id']
            info = {'name':result['platform_plan'], 'owner':owner, 'follower':'', 'imageUrl':'',
                    'restartDeviceBeforeTest':False, 'aiAutoHeal':False, 'debugMode':False,
                    'autoCommitBug':False, 'projectLabel':row['project'], 'deviceType':'', 'timeoutMinutes':60}
            if origin is not None:
                result['retry_of'] = origin
                info['remark'] = ('DeckLab 复测 TitanX 计划 {plan_id} / 作业 {job_id} / '
                                  '第 {round} 轮 / 失败记录 {record_id}').format(**origin)
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
                    or (origin is not None and actual.get('remark') != info['remark'])
                    or any(actual.get(key) is not False for key in ('aiAutoHeal','restartDeviceBeforeTest','debugMode','autoCommitBug'))
                    or actual.get('imageUrl') or [d['deviceId'] for d in devices] != [code]
                    or [(c['caseId'], c['times']) for c in cases] != [(case_id, repeats)]):
                raise TaskError('平台保存的计划与选择不一致', 409)
            current_board()
            context.check()
            context.issued()
            running = True
            try:
                platform.request(f'/taskInfo/start?taskId={tid}')
            except TaskError as error:
                if error.status < 500:
                    raise
                original = platform.request(f'/taskInfo/{tid}')
                if original.get('id') != tid or original.get('status') not in (1, 2, -2, -3):
                    raise error
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
            if delegated:
                # Record a verified platform result even if Edge still holds the board.
                context.stage('team-test-completed', done=repeats, total=repeats)
            else:
                context.progress(repeats, repeats, 'team-test-completed')
            return result
        finally:
            if running:
                coordinator.keep_recovery(context.id)
                FlashStore(manager.store).update_details(context.id, {'recovery_required':True})
            else:
                coordinator.release(context.id, confirmed=True)

    return manager.tasks.start_board('team-plan' if delegated else 'board-test', actor, payload.get('client_id', ''),
                                     serial, run, request_id=request_id, input_digest=digest,
                                     details={'attempt_id': request_id}, maintenance=coordinator,
                                     connection_id=generation, tracks_outcome=True)
