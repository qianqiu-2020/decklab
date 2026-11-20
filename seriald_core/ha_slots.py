"""Home Assistant controller inventory and fixed board actions."""
from dataclasses import dataclass, field
from contextlib import ExitStack
from http.client import HTTPConnection, HTTPSConnection, HTTPException
import json
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

from .websocket_io import _protocol_modules

from .tasks import TaskError

SUFFIX = re.compile(r'(switch|button)\.[a-z0-9_]*board_([1-4])_(reset|fastboot_mode)\Z')


def origin(value):
    if not isinstance(value, str) or len(value) > 500 or any(c.isspace() for c in value):
        raise ValueError('请输入 Home Assistant 根地址')
    u = urlsplit(value)
    if (u.scheme not in ('http', 'https') or not u.hostname or u.username is not None
            or u.password is not None or u.query or u.fragment or u.path not in ('', '/')):
        raise ValueError('请输入 Home Assistant 根地址')
    host = '[' + u.hostname + ']' if ':' in u.hostname else u.hostname
    try:
        port = u.port
        if port == 0:
            raise ValueError()
    except ValueError:
        raise ValueError('Home Assistant 端口无效') from None
    return u.scheme + '://' + host + (':' + str(port) if port and port != (443 if u.scheme == 'https' else 80) else '')


def request(url, token, path, payload):
    parts = urlsplit(url)
    con = (HTTPSConnection if parts.scheme == 'https' else HTTPConnection)(parts.hostname, parts.port, timeout=12)
    try:
        con.request('GET' if payload is None else 'POST', path, None if payload is None else json.dumps(payload), {'Authorization': 'Bearer ' + token,
                                                      'Content-Type': 'application/json'})
        res = con.getresponse()
        if res.status != 200:
            raise TaskError('Home Assistant 返回 HTTP %d' % res.status, 502)
        raw = res.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError()
        return json.loads(raw)
    except TaskError:
        raise
    except (HTTPException, OSError, ValueError):
        raise TaskError('Home Assistant 连接失败或回执无效', 502) from None
    finally:
        con.close()


def registries(url, token):
    """Use the same registry API as BoardLab; wsproto handles framing and masking."""
    parts = urlsplit(url)
    deadline = time.monotonic() + 12
    commands = ('config/area_registry/list', 'config/device_registry/list', 'config/entity_registry/list')
    results = []
    try:
        module, events = _protocol_modules()
        peer = module.WSConnection(module.ConnectionType.CLIENT)
        with ExitStack() as stack:
            sock = stack.enter_context(socket.create_connection((parts.hostname, parts.port or
                (443 if parts.scheme == 'https' else 80)), timeout=5))
            if parts.scheme == 'https':
                sock = stack.enter_context(ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname))
            sock.sendall(peer.send(events.Request(host=parts.netloc, target='/api/websocket')))
            fragments, length, authenticated = [], 0, False
            while len(results) < len(commands):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                sock.settimeout(remaining)
                data = sock.recv(65536)
                if not data:
                    raise OSError()
                peer.receive_data(data)
                for event in peer.events():
                    if isinstance(event, events.Ping):
                        sock.sendall(peer.send(event.response()))
                        continue
                    if isinstance(event, events.AcceptConnection):
                        continue
                    if not isinstance(event, events.TextMessage):
                        raise ValueError()
                    length += len(event.data)
                    if length > 8 * 1024 * 1024:
                        raise ValueError()
                    fragments.append(event.data)
                    if not event.message_finished:
                        continue
                    msg = json.loads(''.join(fragments))
                    fragments, length = [], 0
                    if msg['type'] == 'auth_required' and not authenticated:
                        answer = {'type': 'auth', 'access_token': token}
                    else:
                        if msg['type'] == 'auth_ok' and not authenticated:
                            authenticated = True
                        elif (authenticated and msg['type'] == 'result' and msg['id'] == len(results) + 1
                              and msg.get('success') and isinstance(msg.get('result'), list)):
                            results.append(msg['result'])
                        else:
                            raise TaskError('Home Assistant 拒绝认证或读取注册表，请检查令牌权限', 502)
                        if len(results) == len(commands):
                            return results
                        answer = {'id': len(results) + 1, 'type': commands[len(results)]}
                    sock.sendall(peer.send(events.TextMessage(data=json.dumps(answer))))
    except TaskError:
        raise
    except (OSError, ValueError, KeyError, RuntimeError):
        raise TaskError('无法读取 Home Assistant 控制器清单，请检查地址和连接', 502) from None


def inventory(url, token):
    areas, devices, entities = registries(url, token)
    areas = {a['area_id']: a['name'] for a in areas}
    devices = {d['id']: d for d in devices}
    slots = {}
    for row in entities:
        device = row.get('device_id')
        if row.get('platform') != 'esphome' or row.get('disabled_by') or device not in devices:
            continue
        dev = devices[device]
        entity = row['entity_id']
        name = dev.get('name_by_user') or dev.get('name')
        area = areas.get(row.get('area_id') or dev.get('area_id'), '')
        match = SUFFIX.fullmatch(entity)
        if not match or not isinstance(device, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,100}', device):
            continue
        number = int(match[2])
        key = device + '/' + str(number)
        slot = slots.setdefault(key, dict(device=device, number=number, name=name or device, area=area,
                                          entities={}))
        action = 'reset' if match[3] == 'reset' else 'enter-download'
        if action in slot['entities']:
            raise TaskError('同一控制器槽位存在重复控制实体，请核对 Home Assistant', 409)
        slot['entities'][action] = entity
    return slots


@dataclass(frozen=True)
class SlotAction:
    url: str
    token: str = field(repr=False)
    key: str
    action: str
    entity: str
    allow_agent: bool
    provider = 'home-assistant'
    exclusive_serial = False
    timeout = 30

    def bind(self, device):
        return self

    def run(self, check, cleanup_failed=None, outcome=None):
        check()
        slot = inventory(self.url, self.token).get(self.key, {})
        if slot.get('entities', {}).get(self.action) != self.entity:
            raise TaskError('槽位离线或实体已变化，请重新检测接线', 409)
        state = request(self.url, self.token, '/api/states/' + self.entity, None)
        if not isinstance(state, dict) or state.get('entity_id') != self.entity or state.get('state') == 'unavailable':
            raise TaskError('槽位实体不可用，请检查控制器连接', 409)
        check()
        if outcome:
            outcome('issued')
        service = 'switch/turn_on' if self.entity.startswith('switch.') else 'button/press'
        try:
            receipt = request(self.url, self.token, '/api/services/' + service, {'entity_id': self.entity})
            if not isinstance(receipt, list):
                raise TaskError('Home Assistant 回执无效', 502)
        except TaskError as error:
            if outcome:
                outcome('unknown')
            raise TaskError(str(error) + '；操作结果未知，请核对原任务和设备', 502) from None
        if outcome:
            outcome('acknowledged')
        return {'provider': self.provider, 'control_result': 'acknowledged'}


def validate_token(token):
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{1,4096}', token):
        raise ValueError('请填写 Home Assistant 访问令牌')


def add_slots(boards, connections):
    if not isinstance(connections, dict):
        raise ValueError('工位配置无效')
    bound = set()
    physical = set()
    assigned_entities = set()
    for url, config in connections.items():
        if origin(url) != url or not isinstance(config, dict) or set(config) != {'secret_token', 'slots'}:
            raise ValueError('工位地址或配置无效')
        token = config['secret_token']
        validate_token(token)
        if not isinstance(config['slots'], dict):
            raise ValueError('槽位配置无效')
        for key, s in config['slots'].items():
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}/[1-4]', key) or not isinstance(s, dict):
                raise ValueError('槽位标识无效')
            serial, entities, agent = s.get('serial'), s.get('entities'), s.get('allow_agent')
            if (not isinstance(serial, str) or not serial or len(serial.encode()) > 100 or serial in bound
                    or type(agent) is not bool or not isinstance(entities, dict) or not entities
                    or any(not isinstance(s.get(k), str) for k in ('area', 'name'))):
                raise ValueError('板卡重复绑定或槽位配置无效')
            if key in physical or any((url, entity) in assigned_entities for entity in entities.values() if isinstance(entity, str)):
                raise ValueError('同一控制器槽位不能重复绑定，请先移交原接线')
            if 'suspended_on' in s and (not isinstance(s['suspended_on'], str) or not s['suspended_on']):
                raise ValueError('换板标记无效')
            physical.add(key)
            actions = boards.setdefault(serial, {})
            for action, entity in entities.items():
                match = SUFFIX.fullmatch(entity) if isinstance(entity, str) else None
                if (not match or match[2] != key[-1] or action in actions
                        or action != ('reset' if match[3] == 'reset' else 'enter-download')):
                    raise ValueError('槽位动作重复或实体不匹配')
                assigned_entities.add((url, entity))
                if s.get('suspended_on'):
                    continue
                actions[action] = SlotAction(url, token, key, action, entity, agent)
            bound.add(serial)
