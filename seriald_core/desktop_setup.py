"""Bounded service identification and owner setup for the existing desktop book."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import ipaddress
import json
import logging
from pathlib import Path
import shlex
import shutil
import subprocess
import re
import secrets
import socket
import ssl
import sqlite3
import struct
import threading
import time
from urllib.parse import quote, unquote, urlsplit

from .tasks import TaskError
from .web_settings import SettingsError

PORTS = (('rdp', 3389), ('rdp', 3390), ('vnc', 5900), ('vnc', 5901), ('vnc', 5902))
_SLOTS = threading.BoundedSemaphore(8)


def hosts_for(device):
    result = []
    for value in (device.probe_info or {}).get('ips', []):
        if not isinstance(value, str):
            continue
        fields = value.split()
        if len(fields) != 2 or fields[0] == 'lo' or fields[0].startswith(('docker', 'veth', 'br-')):
            continue
        try:
            address = ipaddress.ip_interface(fields[1]).ip
        except ValueError:
            continue
        if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
            continue
        host = str(address)
        if host not in result:
            result.append(host)
    return result[:4]


def checked_host(value):
    if not isinstance(value, str) or len(value) > 253 or '%' in value:
        raise TaskError('请输入主机名或 IP 地址')
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if not re.fullmatch(r'(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', value) or any(
                not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in value.split('.')):
            raise TaskError('请输入主机名或 IP 地址')
        return value.lower()
    if address.is_multicast or address.is_unspecified:
        raise TaskError('不能连接广播或未指定地址')
    return str(address)


def receive(stream, length):
    data = bytearray()
    while len(data) < length:
        block = stream.recv(length - len(data))
        if not block:
            raise OSError('connection closed')
        data.extend(block)
    return bytes(data)


def probe(host, port, provider, timeout=1):
    """Identify a service without credentials, desktop login or graphical input."""
    with _SLOTS, socket.create_connection((host, port), timeout=timeout) as stream:
        stream.settimeout(timeout)
        result = {'host': host, 'port': port, 'provider': provider}
        if provider == 'vnc':
            banner = receive(stream, 12)
            if not re.fullmatch(rb'RFB [0-9]{3}\.[0-9]{3}\n', banner):
                return None
            return dict(result, protocol_version=banner.decode().strip())
        # Standard X.224 negotiation only. FreeRDP continues to own the actual
        # RDP connection, authentication, graphics and redirection protocols.
        stream.sendall(bytes.fromhex('030000130ee00000000000010008000b000000'))
        header = receive(stream, 4)
        length = int.from_bytes(header[2:], 'big')
        if header[:2] != b'\x03\x00' or not 11 <= length <= 512:
            return None
        body = receive(stream, length - 4)
        if len(body) < 7 or body[1] != 0xd0:
            return None
        if len(body) < 15 or body[-8] != 2 or body[-6:-4] != b'\x08\x00' or struct.unpack('<I', body[-4:])[0] not in (1, 2, 8):
            return dict(result, error='RDP 服务未提供可用的 TLS 协商，不能建立证书校验连接')
        # Observation only; no password or certificate policy change during discovery.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            with context.wrap_socket(stream, server_hostname=host) as secured:
                certificate = secured.getpeercert(binary_form=True)
                if not certificate:
                    raise ValueError('server did not present a certificate')
        except (OSError, ValueError):
            return dict(result, error='已发现 RDP 服务，但未能完成 TLS 证书检测')
        return dict(result, certificate_sha256=hashlib.sha256(certificate).hexdigest(), certificate_trusted=False)


def connection_fields(config):
    if not config or config.get('provider') == 'helper':
        return {}
    try:
        parts = urlsplit(config['secret_url'])
        vnc = config['provider'] == 'vnc'
        secret = (parts.password or parts.username) if vnc else parts.password
        return {'provider': config['provider'], 'host': parts.hostname or '',
                'port': parts.port or (3389 if config['provider'] == 'rdp' else 5900),
                'username': '' if vnc else unquote(parts.username or ''), 'password_saved': bool(secret),
                'certificate_sha256': config.get('certificate_sha256', ''),
                'certificate_policy': config.get('certificate_policy',
                    'pin' if config.get('certificate_sha256') else 'system') if not vnc else '',
                'engine': config.get('engine', 'aardwolf') if config['provider'] == 'rdp' else '',
                'allow_agent': config.get('allow_agent', False)}
    except (KeyError, ValueError):
        return {}


class DesktopSetup:
    def __init__(self, desktop, settings):
        self.desktop, self.settings = desktop, settings
        self.cache, self.lock = {}, threading.Lock()

    def log_adb(self, serial, actor, detail):
        try:
            self.desktop.manager.store.event(serial, actor, 'desktop-adb', detail)
        except sqlite3.Error:
            logging.getLogger(__name__).exception('Cannot persist desktop ADB activity')

    def identified(self, device):
        if not device or not device.serial:
            raise TaskError('请先识别设备，再配置远程桌面', 409)
        if self.desktop.manager.store.has_conflict(device.serial):
            raise TaskError('请先处理板卡身份冲突', 409)

    def public(self, device):
        self.identified(device)
        snapshot = self.settings.snapshot()
        fields = connection_fields(self.desktop.book.boards.get(device.serial))
        ssh = device.ssh_settings()
        family = ssh.get('os_family', '')
        return {'revision': snapshot['revision'], 'connection': fields, 'hosts': hosts_for(device),
                'os_family': family, 'quick_connect': self.quick_eligible(device, fields),
                'suggested_user': ssh.get('user', '') if not family and ssh.get('saved') else '',
                'protocols': ['vnc', 'rdp']}

    def quick_eligible(self, device, fields):
        return (bool(device.present and shutil.which('adb'))
                and (not self.desktop.book.boards.get(device.serial)
                     or fields.get('provider') == 'rdp' and fields.get('host') in hosts_for(device)
                     and fields.get('port') == 3389 and fields.get('username') == 'rdp'))

    def quick_config(self, device, previous, *, stage_only=False, actor='web'):
        from .adb_session import usb_endpoints
        from .board_cases import READ_IDENTITY, adb_command, find_transport
        fields = connection_fields(previous)
        if not self.quick_eligible(device, fields):
            return None, ''
        serial, connection = device.serial, device.connection_id
        executable = shutil.which('adb')
        registered = self.desktop.manager.hardware.board_tests.get(serial)
        started = time.monotonic()
        step = 'adb devices -l · 匹配 USB 连接并核对板卡身份'

        def receipt(result):
            self.log_adb(serial, actor, f'{step} · {result} · {time.monotonic() - started:.1f} 秒')

        try:
            _, endpoint, facts = find_transport(executable, serial, registered, usb_endpoints=usb_endpoints)
            lxqt = Path(__file__).with_name('desktop_lxqt_target.py').read_text(encoding='utf-8')
            gnome = Path(__file__).with_name('desktop_gnome_target.py').read_text(encoding='utf-8')
            guard = (READ_IDENTITY + '[ "$serial" = ' + shlex.quote(serial)
                       + ' ] && [ "$(cat /proc/sys/kernel/random/boot_id)" = ' + shlex.quote(facts['boot_id'])
                       + ' ] || exit 1\n')
            command = (guard + "if grep -qx 'ID=bianbu' /etc/os-release; then\nexec python3 -I - <<'DECKLAB_LXQT'\n"
                       + lxqt + '\nreport_lxqt()\nDECKLAB_LXQT\nelse\n' + "exec python3 -I - <<'DECKLAB_GNOME'\n"
                       + gnome + '\nDECKLAB_GNOME\nfi')
            if stage_only:
                command = guard + "exec python3 -I - --stage-only <<'DECKLAB_GNOME'\n" + gnome + '\nDECKLAB_GNOME'
            step = ('adb shell · 查询首次设置状态' if stage_only else 'adb shell · 读取桌面类型、登录阶段和连接设置')
            reply = adb_command(executable, '-t', endpoint, 'shell', '-T', command, timeout=12)
            if reply.returncode:
                receipt(f'失败，ADB 退出码 {reply.returncode}')
                return None, ''
            data = json.loads(reply.stdout)
        except subprocess.TimeoutExpired:
            receipt('等待 ADB 回执超时')
            return None, ''
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            receipt('失败，' + type(error).__name__)
            return None, ''
        try:
            self.identified(device)
        except TaskError:
            receipt('板卡身份已变化，结果已丢弃')
            raise
        if (device.serial != serial or device.connection_id != connection or not device.present
                or device.board.connection is not device):
            receipt('板卡接线已变化，结果已丢弃')
            raise TaskError('板卡接线已变化，请重新打开桌面', 409)
        if not isinstance(data, dict):
            receipt('ADB 退出码 0，无法识别桌面回执')
            return None, ''
        stage = data.get('stage', '')
        stages = {'initial':'GNOME 首次设置', 'finishing':'GNOME 首次设置即将完成', 'login':'GNOME 登录',
                  'no-user':'尚无普通用户', 'lxqt-initial':'LXQt 首次设置', 'lxqt-finishing':'LXQt 首次设置即将完成',
                  'lxqt-greeter':'LXQt 登录', 'lxqt-desktop':'LXQt 用户桌面', 'lxqt-unavailable':'LXQt 屏幕不可用'}
        names = {'accounts':'busctl get-property HasNoUsers', 'show-seat':'loginctl show-seat',
                 'show-session':'loginctl show-session', 'grdctl':'grdctl --system status --show-credentials'}
        queries = data.get('queries')
        if isinstance(queries, list):
            steps = [names[q] for q in queries[:8] if isinstance(q, str) and q in names]
            if steps:
                step += ' · ' + ' → '.join(steps)
        receipt('ADB 退出码 0，' + (stages.get(stage, '未识别桌面状态') if isinstance(stage, str) else '无效的桌面状态'))
        if stage_only:
            return None, stage if stage in ('initial', 'finishing', 'login', 'no-user') else ''
        if stage == 'lxqt-unavailable':
            return None, stage
        if stage in ('lxqt-initial', 'lxqt-finishing', 'lxqt-greeter', 'lxqt-desktop'):
            command = guard + 'exec python3 -I -c ' + shlex.quote(lxqt + '\nserve_lxqt()')
            config = dict(previous or {}, provider='vnc', display='desktop', display_kind='physical',
                          usb_vnc_argv=[executable, '-t', endpoint, 'shell', '-T', command])
            for key in ('engine', 'secret_url', 'text_input', 'certificate_sha256', 'certificate_policy'):
                config.pop(key, None)
            return config, stage
        if stage not in ('initial', 'finishing', 'login', 'no-user'):
            return None, ''
        pin, port = data.get('pin'), data.get('port')
        user, password = data.get('username'), data.get('password')
        if (not isinstance(pin, str) or not re.fullmatch(r'[a-f0-9]{64}', pin)
                or type(port) is not int or not 1 <= port <= 65535
                or any(not isinstance(v, str) or not v or v in ('(hidden)', '(null)')
                       or len(v) > 4096 or '\0' in v for v in (user, password))):
            return None, stage
        hosts = hosts_for(device)
        if not hosts:
            return None, stage
        host = fields.get('host') if fields.get('host') in hosts else hosts[0]
        authority = '[' + host + ']' if ':' in host else host
        config = dict(previous or {}, provider='rdp', engine='freerdp', display='desktop',
                      display_kind='unknown', text_input='clipboard', certificate_sha256=pin,
                      secret_url='rdp+ntlm-password://' + quote(user, safe='') + ':' + quote(password, safe='')
                      + '@' + authority + ':' + str(port))
        config.pop('certificate_policy', None)
        return config, stage

    def discover(self, device, endpoint=None):
        self.identified(device)
        serial = device.serial
        known = hosts_for(device)
        hosts = [endpoint[0]] if endpoint else known
        identity = (tuple(known), device.connection_id)
        now = time.monotonic()
        if not self.lock.acquire(blocking=False):
            raise TaskError('正在检测远程桌面，请稍候', 429)
        try:
            cached = self.cache.get(serial)
            if endpoint is None and cached and cached['identity'] == identity and cached['endpoint'] == endpoint and now - cached['created'] < 20:
                return deepcopy(cached['public'])
            def attempt(item):
                provider, port, host = item
                try:
                    return probe(host, port, provider)
                except (OSError, ValueError):
                    return None
            with ThreadPoolExecutor(max_workers=8) as workers:
                if endpoint and endpoint[2] == 'rdp' and endpoint[1] in (3389, 3390):
                    requests = [('rdp', port, endpoint[0]) for port in (3389, 3390)]
                else:
                    requests = [(endpoint[2], endpoint[1], endpoint[0])] if endpoint else [(provider, port, host) for host in hosts for provider, port in PORTS]
                found = list(workers.map(attempt, requests))
            if device.serial != serial or (tuple(hosts_for(device)), device.connection_id) != identity:
                raise TaskError('设备身份或地址已改变，请重新检测', 409)
            candidates = [dict(value, id=secrets.token_hex(12)) for value in found if value]
            public = {'candidates': candidates, 'hosts': hosts, 'observed_at': time.time(),
                      'reason': '' if any(not item.get('error') for item in candidates) else '已发现服务，但当前握手未满足连接要求' if candidates else '未发现 VNC／RDP 服务，可填写地址手工连接' if hosts else '尚无可探测的设备 IP；可先识别设备或手工填写地址'}
            self.cache[serial] = {'identity': identity, 'endpoint': endpoint, 'created': time.monotonic(), 'public': public}
            if len(self.cache) > 128:
                self.cache.pop(next(iter(self.cache)))
            return deepcopy(public)
        finally:
            self.lock.release()

    def configure(self, device, payload):
        self.identified(device)
        serial = device.serial
        allowed = {'key', 'serial', 'actor', 'client_id', 'revision', 'provider', 'host', 'port',
                   'username', 'password', 'reuse_password', 'certificate_sha256', 'trust_certificate', 'certificate_policy',
                   'allow_agent', 'candidate_id'}
        if set(payload) - allowed:
            raise TaskError('未知的桌面连接设置')
        provider, host, port = payload.get('provider'), checked_host(payload.get('host')), payload.get('port')
        if provider not in ('vnc', 'rdp') or type(port) is not int or not 1 <= port <= 65535:
            raise TaskError('请选择 VNC／RDP 并填写有效端口')
        username, password = payload.get('username', ''), payload.get('password')
        if (not isinstance(username, str) or len(username) > 128 or any(c in username for c in '\x00\r\n')
                or provider == 'rdp' and not username or password is not None and (
                not isinstance(password, str) or len(password) > 4096 or '\x00' in password)):
            raise TaskError('桌面用户名或密码无效')
        if provider == 'vnc' and username:
            raise TaskError('VNC 连接只使用密码，无需填写用户名')
        for key in ('reuse_password', 'trust_certificate', 'allow_agent'):
            if key in payload and type(payload[key]) is not bool:
                raise TaskError('桌面连接选项必须为布尔值')
        pin = payload.get('certificate_sha256', '')
        if not isinstance(pin, str) or pin and (provider != 'rdp' or not re.fullmatch(r'[0-9a-fA-F]{64}', pin)):
            raise TaskError('RDP 证书指纹必须是完整的 SHA256')
        policy = payload.get('certificate_policy', 'pin' if pin else 'accept' if provider == 'rdp' else '')
        if (provider == 'rdp' and (policy not in ('accept', 'system', 'pin') or bool(pin) != (policy == 'pin'))
                or provider == 'vnc' and policy != ''):
            raise TaskError('RDP 证书校验选项无效')
        candidate_id = payload.get('candidate_id')
        if candidate_id:
            cached = self.cache.get(serial)
            if (not cached or cached['identity'] != (tuple(hosts_for(device)), device.connection_id)
                    or time.monotonic() - cached['created'] > 180):
                raise TaskError('探测结果已过期或设备连接已改变，请重新检测', 409)
            candidate = next((v for v in cached['public']['candidates'] if v['id'] == candidate_id), None)
            if (not candidate or candidate.get('error') or (provider, host, port) != (
                    candidate['provider'], candidate['host'], candidate['port'])
                    or policy == 'pin' and pin.lower() != candidate.get('certificate_sha256', '').lower()):
                raise TaskError('所选服务与探测结果不符，请重新检测', 409)
        desktop = self.desktop
        with desktop.lock:
            if device.serial != serial:
                raise TaskError('设备身份已改变，请重新加载', 409)
            if any(not s['closed'] and s['serial'] == serial for s in desktop.sessions.values()):
                raise TaskError('请先断开该设备的现有桌面连接，再修改设置', 423)
            snapshot = self.settings.snapshot(secrets=True)
            if payload.get('revision') != snapshot['revision']:
                raise TaskError('配置已改变，请重新加载', 409)
            document = deepcopy(snapshot['configuration']['documents'].get('desktop', {'boards': {}}))
            old = document.get('boards', {}).get(serial, desktop.book.boards.get(serial, {}))
            previous = connection_fields(old)
            endpoint_same = all(previous.get(k) == value for k, value in (
                ('provider', provider), ('host', host), ('port', port)))
            same = endpoint_same and previous.get('username') == username
            if password is None and payload.get('reuse_password'):
                if not same:
                    raise TaskError('连接目标或用户名已改变，请重新填写密码', 409)
                parts = urlsplit(old['secret_url'])
                password = unquote((parts.password or parts.username or '') if provider == 'vnc' else parts.password or '')
            if pin and (not endpoint_same or old.get('certificate_sha256', '').lower() != pin.lower()) and not payload.get('trust_certificate'):
                raise TaskError('请核对并确认首次观察到的 RDP 证书指纹', 409)
            authority = '[' + host + ']' if ':' in host else host
            credentials = quote(username, safe='') + ':' + quote(password or '', safe='')
            config = {'provider': provider, 'secret_url': provider + ('+ntlm-password://' if provider == 'rdp' else '+plain://')
                      + credentials + '@' + authority + ':' + str(port),
                      'display': old.get('display', 'desktop'), 'display_kind': old.get('display_kind', 'unknown'),
                      'allow_agent': payload.get('allow_agent', False)}
            reservation = desktop.book.boards.get(serial, {}).get('reservation') or old.get('reservation')
            if reservation:
                config['reservation'] = deepcopy(reservation)
            for key in ('scale', 'width', 'height'):
                if key in old:
                    config[key] = old[key]
            if provider == 'rdp':
                mode = old.get('text_input', 'unicode') if old.get('provider') == 'rdp' else 'clipboard'
                config.update(engine='freerdp', text_input=mode)
                if pin:
                    config['certificate_sha256'] = pin.lower()
                elif policy == 'accept':
                    config['certificate_policy'] = policy
            from .desktop import DesktopBook
            active = DesktopBook({'boards': dict(desktop.book.boards, **{serial: config})})
            document.setdefault('boards', {})[serial] = config
            try:
                self.settings.save({'version': 1, 'documents': {'desktop': document}}, snapshot['revision'])
            except SettingsError as error:
                raise TaskError('配置已改变，请重新加载' if error.status == 409 else '无法保存桌面连接配置', error.status) from None
            desktop.book = active
            return {'ok': True, 'state': 'configured', 'setup': self.public(device)}
