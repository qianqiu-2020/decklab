"""Owner-only initialization of a standalone TitanX station."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile

from . import titanx_workbench as worker
from .tasks import TaskError
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
        devices[serial] = {'serial': serial, 'label': item.get('label') or item.get('key') or serial,
                           'online': bool(item.get('alive'))}
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
    platform({'api_url': settings.get('web_origin'), 'api_token': 'validate-origin'})
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
            if (node.get('decklabWebOrigin') != settings['web_origin']
                    or str(node.get('state', '')).lower() == 'online'):
                raise SettingsError('节点 ID 已被其他工位使用，或原执行器仍在线，请核对工位交接', 409)
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


def status(connect, check_platform=False):
    try:
        settings = worker.read_config()
    except FileNotFoundError:
        return {'configured': False, 'node_id': socket.gethostname(), 'boards': inventory(connect)}
    result = {'configured': True, 'node_id': settings.get('node_id'), 'lab_id': settings.get('lab_id'),
              'slots': settings.get('slots', {}), 'first_com': settings.get('first_com', 1)}
    try:
        proc = subprocess.run(['systemctl', 'show', 'decklab-titanx.service', '-p', 'ActiveState', '--value'],
                              capture_output=True, text=True, timeout=3)
        result['worker_state'] = proc.stdout.strip() if proc.returncode == 0 else 'unknown'
    except (OSError, subprocess.TimeoutExpired):
        result['worker_state'] = 'unknown'
    if check_platform and settings.get('lab_id'):
        try:
            nodes = platform(settings).request('/deviceNode/listVO', {'namespaceId': settings['lab_id']})
            matches = [n for n in nodes if n.get('originNodeId') == settings['node_id']]
            result['registered'] = []
            if len(matches) == 1:
                node = matches[0]
                result['platform_state'] = node.get('state')
                for board in node.get('deviceBoards') or []:
                    slot, serial = board.get('decklabSlot'), board.get('decklabSerial')
                    if (type(slot) is int and serial and result['slots'].get(str(slot)) == serial
                            and board.get('remoteComserver') == 100
                            and board.get('remoteCom') == result['first_com'] + slot - 1):
                        result['registered'].append(serial)
        except (TaskError, SettingsError):
            result['platform_error'] = '暂时无法读取 TitanX 档案，请检查平台连接与账号权限'
    return result
