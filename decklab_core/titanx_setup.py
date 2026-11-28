"""Owner-only initialization of a standalone TitanX station."""
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile

from . import titanx_workbench as worker
from .tasks import TaskError
from .titanx_enroll import archive_matches
from .team_cases import TeamCases
from .web_settings import SettingsError


def available():
    return Path('/usr/libexec/decklab/titanx/edge').is_file() and worker.GUI_CONFIG.parent.is_dir()


def platform(settings):
    url, token = settings.get('api_url'), settings.get('api_token')
    if not isinstance(url, str) or not isinstance(token, str):
        raise SettingsError('请填写 TitanX HTTPS 地址和平台令牌')
    try:
        return TeamCases(url, token)
    except TaskError as error:
        raise SettingsError(str(error)) from error


def labs(settings):
    try:
        rows = platform(settings).request('/deviceNamespace/list', {})
    except TaskError as error:
        raise SettingsError(str(error), 502) from error
    if not isinstance(rows, list):
        raise SettingsError('TitanX 返回的实验室列表无效', 502)
    return [{'id': r['id'], 'name': r['name']} for r in rows if isinstance(r, dict)
            and type(r.get('id')) is int and isinstance(r.get('name'), str)]


def inventory(connect):
    connection = connect(5)
    try:
        connection.request('GET', '/api/devices')
        response = connection.getresponse()
        data = response.read(4 * 1024 * 1024 + 1)
        if response.status != 200 or len(data) > 4 * 1024 * 1024:
            raise SettingsError('无法读取本机板卡，请稍后刷新', 503)
        rows = json.loads(data)['devices']
    finally:
        connection.close()
    devices = {}
    for item in rows:
        serial = item.get('serial')
        if not serial or (item.get('info') or {}).get('identity_status') != 'confirmed':
            continue
        if serial in devices:
            raise SettingsError('板号重复，请先在 DeckLab 核对设备身份', 409)
        soc = re.match(r'(K[0-9]+)\b', str(item['info'].get('soc_id', '')), re.I)
        devices[serial] = {'serial': serial, 'label': item.get('label') or item.get('key') or serial,
                           'online': bool(item.get('alive')), 'project': soc[1].upper() if soc else '',
                           'busy': bool(item.get('busy') or item.get('input_owner') or item.get('operation'))}
    return [devices[s] for s in sorted(devices)]


def initialize(settings, connect):
    if worker.config_path().exists():
        raise SettingsError('此工位已有接入配置，请刷新查看状态', 409)
    if not isinstance(settings, dict) or settings.keys() - {
            'node_id', 'broker', 'username', 'password', 'api_url', 'api_token', 'lab_id', 'project',
            'projects', 'web_origin', 'accept_dispatch', 'slots', 'first_com'}:
        raise SettingsError('工位配置字段无效')
    try:
        if not isinstance(settings.get('broker'), str):
            raise ValueError
        worker.connection_settings(settings)
    except (TypeError, ValueError):
        raise SettingsError('请检查节点 ID、MQTT 地址（含端口）和认证信息') from None
    try:
        slots = worker.assignments({'slots': settings.get('slots', {})}, {}, lambda: [])
    except (TypeError, ValueError):
        raise SettingsError('请选择板卡，并为每块板分配唯一槽位') from None
    if set(slots.values()) - {b['serial'] for b in inventory(connect)}:
        raise SettingsError('板卡身份已变化，请关闭并重新打开初始化窗口', 409)
    first = settings.get('first_com', 1)
    if type(first) is not int or first < 1 or first + max(slots) - 1 > 65535:
        raise SettingsError('分配的 COM 超出 1 至 65535')
    if type(settings.get('lab_id')) is not int or settings['lab_id'] < 1:
        raise SettingsError('请选择目标实验室')
    project = settings.get('project')
    overrides = settings.get('projects', {})
    if (not isinstance(overrides, dict) or overrides.keys() - {str(slot) for slot in slots}
            or any(not isinstance(value, str) or not value.strip() or len(value) > 80
                   for value in [project, *overrides.values()])):
        raise SettingsError('请填写板卡项目类型')
    if type(settings.get('accept_dispatch', False)) is not bool:
        raise SettingsError('自动调度设置无效')
    if settings.get('web_origin'):
        platform({'api_url': settings['web_origin'], 'api_token': 'validate-origin'})
    if settings['lab_id'] not in {lab['id'] for lab in labs(settings)}:
        raise SettingsError('平台账号无法访问所选实验室', 403)
    try:
        nodes = platform(settings).request('/deviceNode/listVO', {'namespaceId': settings['lab_id']})
    except TaskError as error:
        raise SettingsError(str(error), 502) from error
    selected = set(slots.values())
    ports = {first + slot - 1 for slot in slots}
    for node in nodes:
        if node.get('originNodeId') == settings['node_id']:
            if ((settings.get('web_origin') and node.get('decklabWebOrigin') not in (None, settings['web_origin']))
                    or str(node.get('state', '')).lower() == 'online'):
                raise SettingsError('节点 ID 已被其他工位使用，或原执行器仍在线，请核对工位交接', 409)
            for board in node.get('deviceBoards') or []:
                if not any(archive_matches(board, node['id'], f"{settings['node_id']}:{slot}",
                                           serial, slot, first + slot - 1) for slot, serial in slots.items()):
                    raise SettingsError('该节点还有未匹配的板卡，请核对已有槽位和 COM，或选用独立节点', 409)
        elif any(b.get('decklabSerial') in selected or (b.get('remoteComserver') == 100
                 and b.get('remoteCom') in ports) for b in node.get('deviceBoards') or []):
            raise SettingsError('所选板号或 COM 已分配给其他节点，请核对板卡与起始 COM', 409)
    contents = json.dumps(settings, ensure_ascii=False).encode()
    if len(contents) > 65536:
        raise SettingsError('工位配置过大')
    # Link the complete private file once: a retried or concurrent request cannot replace a running node.
    target = worker.GUI_CONFIG
    with tempfile.NamedTemporaryFile(dir=target.parent) as f:
        f.write(contents)
        f.flush()
        os.fsync(f.fileno())
        try:
            os.link(f.name, target)
        except FileExistsError:
            raise SettingsError('此工位已初始化，请刷新查看状态', 409) from None


def applied_slots():
    from .config import BASE
    state = Path(BASE) / 'titanx-worker.json'
    try:
        return json.loads(state.read_text())['slots']
    except FileNotFoundError:
        return {}


def current_slots(settings):
    return settings['slots'] if 'slots' in settings else applied_slots()


def affected_boards(settings):
    paused = settings.get('paused_slots', [])
    return set(applied_slots().values()) | {serial for key, serial in current_slots(settings).items() if key not in paused}


def next_slot(settings, slots, nodes, reports, minimum=1):
    first = settings.get('first_com', 1)
    rows = [b for n in nodes for b in n.get('deviceBoards') or []] + reports
    used = {str(b.get('remoteCom')) for b in rows if str(b.get('remoteComserver')) == '100'}
    identifiers = {b.get(key) for b in rows for key in ('deviceId', 'code')}
    for slot in range(max(minimum, max(map(int, slots), default=0) + 1), 65537 - first):
        if str(first + slot - 1) not in used and f"{settings['node_id']}:{slot}" not in identifiers:
            return slot
    return None


def revision(settings):
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()


def idle_station(settings, boards, affected):
    if any(b['busy'] for b in boards if b['serial'] in affected):
        raise SettingsError('板卡正在使用中，请在作业释放后再应用', 409)
    if not settings.get('lab_id'):
        raise SettingsError('请先在维护工位连接中选择实验室')
    client = platform(settings)
    lab = {'namespaceId': settings['lab_id']}
    try:
        nodes = client.request('/deviceNode/listVO', lab)
        reports = client.request('/deviceBoard/unregistered', lab)
    except TaskError as error:
        raise SettingsError(str(error), 502) from error
    own = [n for n in nodes if n.get('originNodeId') == settings['node_id']]
    if len(own) > 1:
        raise SettingsError('实验室存在重复节点，请核对节点 ID', 409)
    coms = {str(settings.get('first_com', 1) + int(key) - 1)
            for key, serial in current_slots(settings).items() if serial in affected}
    if any(b.get('state') not in ('offline', 'idle') or b.get('claimedByUserId')
           for node in own for b in node.get('deviceBoards') or []
           if str(b.get('remoteComserver')) == '100' and str(b.get('remoteCom')) in coms):
        raise SettingsError('TitanX 工位正在使用中，请在作业释放后再应用', 409)
    return nodes, reports, own


def update_station(payload, connect):
    import fcntl
    with worker.additions_path().with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        settings = worker.read_config()
        if payload.get('revision') != revision(settings):
            raise SettingsError('工位配置已变化，请刷新后重新确认', 409)
        base = worker.read_config(worker.config_path())
        extra = worker.board_additions(base) or {
            'station': worker.station_key(base), 'original_slots': current_slots(settings), 'boards': {}}
        if payload.get('action') == 'titanx-add-board':
            add_board(payload, connect, settings, extra)
        else:
            maintain(payload, connect, settings, extra)
        data = json.dumps(extra, ensure_ascii=False)
        if len(data.encode()) > 65536:
            raise SettingsError('工位配置过大')
        worker.write_private(worker.additions_path(), data)


def maintain(payload, connect, settings, extra):
    operation = payload.get('operation')
    slots = current_slots(settings)
    affected = affected_boards(settings)
    if operation in ('pause', 'resume'):
        key = str(payload.get('slot'))
        if key not in slots or slots[key] != payload.get('serial'):
            raise SettingsError('板卡分配已变化，请刷新后重新选择', 409)
        affected.add(slots[key])
        paused = set(extra.get('paused_slots', []))
        if operation == 'pause':
            from .config import BASE
            pending = Path(BASE) / 'titanx-enroll-pending.json'
            if pending.exists():
                record = json.loads(pending.read_text()).get('record', {})
                if record.get('remoteCom') == settings.get('first_com', 1) + int(key) - 1:
                    raise SettingsError('此板的档案提交正在回查，确认后可暂停', 409)
            paused.add(key)
        else:
            paused.discard(key)
        extra['paused_slots'] = sorted(paused, key=int)
    elif operation == 'connection':
        changes = payload.get('connection')
        if not isinstance(changes, dict) or changes.keys() - {
                'broker', 'username', 'password', 'api_token', 'api_url', 'lab_id', 'project', 'accept_dispatch'}:
            raise SettingsError('工位连接字段无效')
        if any(not isinstance(v, str) or len(v) > 4096 for k, v in changes.items()
               if k not in ('lab_id', 'accept_dispatch')):
            raise SettingsError('工位连接字段无效')
        if changes.get('api_url', settings.get('api_url')) != settings.get('api_url') and not changes.get('api_token'):
            raise SettingsError('更换平台地址时请填写该平台的令牌')
        if (changes.get('broker', settings['broker']) != settings['broker'] and settings.get('password')
                and 'password' not in changes):
            raise SettingsError('更换 MQTT 地址时请重新填写该地址的密码')
        candidate = dict(settings, **changes)
        try:
            worker.connection_settings(candidate)
        except (TypeError, ValueError):
            raise SettingsError('请检查 MQTT 地址及认证信息') from None
        if (type(candidate.get('lab_id')) is not int or candidate['lab_id'] < 1
                or type(candidate.get('accept_dispatch', False)) is not bool
                or not isinstance(candidate.get('project'), str) or not candidate['project'].strip()
                or len(candidate['project']) > 80):
            raise SettingsError('请检查实验室、项目类型和调度设置')
        if candidate['lab_id'] not in {lab['id'] for lab in labs(candidate)}:
            raise SettingsError('平台账号无法访问所选实验室', 403)
        if ((candidate['lab_id'], candidate['api_url']) != (settings.get('lab_id'), settings.get('api_url'))
                and settings.get('lab_id') and settings.get('api_url')):
            raise SettingsError('已绑定实验室的工位不能直接更换实验室或平台', 409)
        extra.setdefault('connection', {}).update(changes)
        settings = candidate
    elif operation != 'retry':
        raise SettingsError('未知的工位操作')
    idle_station(settings, inventory(connect), affected)


def add_board(payload, connect, settings, extra):
    if (not settings.get('lab_id') or payload.get('node_id') != settings['node_id']
            or payload.get('lab_id') != settings['lab_id']):
        raise SettingsError('工位配置已变化，请刷新后重新选择', 409)
    slots = current_slots(settings)
    serial, project = payload.get('serial'), payload.get('project')
    if not isinstance(project, str) or not project.strip() or len(project) > 80 or not project.isprintable():
        raise SettingsError('请填写板卡项目类型')
    if not slots:
        raise SettingsError('请等待原工位完成首次板号绑定后再添加板卡', 409)
    boards = inventory(connect)
    if not any(b['serial'] == serial for b in boards):
        raise SettingsError('板卡身份已变化，请刷新后重新选择', 409)
    if serial in slots.values():
        raise SettingsError('此板已分配，请刷新查看登记状态', 409)
    nodes, reports, own = idle_station(settings, boards, affected_boards(settings) | {serial})
    if not own:
        raise SettingsError('请等待原工位完成节点登记', 409)
    if any(b.get('decklabSerial') == serial for b in reports + [
            b for n in nodes for b in n.get('deviceBoards') or []]):
        raise SettingsError('TitanX 中已有此板号，请核对原登记', 409)
    if type(payload.get('slot')) is not int or type(payload.get('com')) is not int:
        raise SettingsError('请填写有效的 COM')
    slot = next_slot(settings, slots, nodes, reports, minimum=payload['slot'])
    if (slot is None or payload['slot'] != slot
            or payload.get('com') != settings.get('first_com', 1) + slot - 1):
        raise SettingsError('可用槽位或 COM 已变化，请刷新后重新确认', 409)
    extra['boards'][str(slot)] = {'serial': serial, 'project': project.strip()}



def status(connect, check_platform=False):
    try:
        settings = worker.read_config()
    except FileNotFoundError:
        return {'configured': False, 'node_id': socket.gethostname(), 'boards': inventory(connect)}
    result = {'configured': True, 'node_id': settings.get('node_id'), 'lab_id': settings.get('lab_id'),
              'slots': current_slots(settings), 'first_com': settings.get('first_com', 1)}
    result.update(applied_slots=applied_slots(), revision=revision(settings), paused_slots=settings.get('paused_slots', []),
                  connection={k: settings.get(k) for k in ('api_url', 'broker', 'username', 'lab_id', 'project', 'accept_dispatch')})
    try:
        proc = subprocess.run(['systemctl', 'show', 'decklab-titanx.service', '-p', 'ActiveState', '--value'],
                              capture_output=True, text=True, timeout=3)
        result['worker_state'] = proc.stdout.strip() if proc.returncode == 0 else 'unknown'
    except (OSError, subprocess.TimeoutExpired):
        result['worker_state'] = 'unknown'
    if check_platform and settings.get('lab_id'):
        try:
            result['lab_name'] = next((lab['name'] for lab in labs(settings) if lab['id'] == settings['lab_id']), '')
        except SettingsError:
            pass
        try:
            nodes = platform(settings).request('/deviceNode/listVO', {'namespaceId': settings['lab_id']})
            matches = [n for n in nodes if n.get('originNodeId') == settings['node_id']]
            result['registered'] = []
            result['board_states'] = {}
            if len(matches) == 1:
                node = matches[0]
                if result['slots']:
                    reports = platform(settings).request('/deviceBoard/unregistered', {'namespaceId': settings['lab_id']})
                    result['next_slot'] = next_slot(settings, result['slots'], nodes, reports)
                    result['default_project'] = settings.get('project', '')
                    result['available_boards'] = [b for b in inventory(connect) if b['serial'] not in result['slots'].values()]
                result['platform_state'] = node.get('state')
                for key, serial in result['slots'].items():
                    slot = int(key)
                    matches = [board for board in node.get('deviceBoards') or []
                               if archive_matches(board, node['id'], f"{settings['node_id']}:{slot}",
                                                  serial, slot, result['first_com'] + slot - 1)]
                    if len(matches) == 1:
                        result['registered'].append(serial)
                        result['board_states'][serial] = matches[0].get('state')
        except (TaskError, SettingsError):
            result['platform_error'] = '暂时无法读取 TitanX 档案，请检查平台连接与账号权限'
    return result


def wait_for_idle():
    from .config import SOCKET
    from .control_server import UnixHTTPConnection
    import time

    while True:
        try:
            settings = worker.read_config()
            boards = inventory(lambda timeout: UnixHTTPConnection(SOCKET, timeout=timeout))
            idle_station(settings, boards, affected_boards(settings))
            return
        except (OSError, SettingsError) as error:
            print('TitanX station waiting to apply:', type(error).__name__, flush=True)
            time.sleep(5)


if __name__ == '__main__':
    wait_for_idle()
