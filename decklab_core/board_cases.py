"""One-round-at-a-time board tests supervised by the collector."""
import base64
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import threading
import time

from .board_maintenance import maintenance_for
from .flash_store import FlashStore, validate_request
from .tasks import TaskCancelled, TaskError

BUILTIN_CASES = (
    {'id': 'reboot-aging', 'name': '软件重启老化（NOR → UFS → Debian）', 'kind': 'builtin',
     'description': '每轮软件重启一次，检查新启动编号和 NOR → UFS → Debian 启动路径。',
     'steps': [
         '执行前核对板卡序列号、启动编号、固件版本、EFI 启动项、ESP 和 Kingston TY7B-128 UFS 根盘。',
         '在板上执行 systemctl --no-block reboot，通过串口观察 NOR 启动记录。',
         '重新连接后确认 Linux 启动编号改变、multi-user.target 已启动，且固件、EFI 启动项、ESP 与实际根盘仍匹配起始记录。',
     ],
     'mechanisms': [
         '控制机按已登记的 USB 路径找到当前 ADB 连接，启动前核对板卡序列号和 Linux 启动编号。',
         '控制机最多观察 180 秒。通过条件包括新启动编号、从串口捕获的 NOR 启动标记，以及重连后读取的启动项与根盘。退出码 0 只说明重启命令已提交。',
     ]},
    {'id': 'rtc-wake', 'name': '深度挂起与 RTC 唤醒', 'kind': 'builtin',
     'description': '每轮设置 35 秒 RTC 闹钟，进入深度挂起后通过串口核对唤醒。',
     'steps': [
         '检查 deep、pm_test、串口挂起策略和已有闹钟，记录当前 RTC 的唤醒证据计数和内核日志游标。',
         '用 rtcwake 在 rtc0 设置 35 秒闹钟，向串口写入布防标记，再执行 systemctl suspend。',
         '同一次启动内必须出现一次 deep 进入和一次退出，RTC 中断增加，systemd-suspend 成功，闹钟清除。',
     ],
     'mechanisms': [
         'K1 读取专用 RTC 中断，K3 核对 RTC 设备自身的唤醒事件。板端用内核日志、计数变化、挂起服务状态和闹钟共同判定。',
         '控制机通过串口等待同一次启动的布防标记和结果，观察上限为 235 秒。证据不足的轮次记为无结果。',
     ]},
)
CASES = tuple(case['id'] for case in BUILTIN_CASES)
BUILTIN_COMMANDS = {
    'reboot-aging': 'systemctl --no-block reboot',
    'rtc-wake': 'rtcwake -m no -s 35 -d rtc0\nsystemctl suspend',
}
CUSTOM_CASE = re.compile(r'shell-[0-9a-f]{24}')
MAX_SERIAL = 8 * 1024 * 1024
BOOT_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
USB_PORT = re.compile(r'[1-9]\d*-[1-9]\d*(?:\.[1-9]\d*)*')
UART = re.compile(r'ttyS\d{1,3}')
START_REJECTED = re.compile(
    r'^Failed to start transient service unit: (?:Cannot set property\b.*|Unknown property\b.*|'
    r'Access denied\.?|Permission denied\.?|Invalid argument\.?)$', re.MULTILINE | re.IGNORECASE)
READ_IDENTITY = r"""
serial=$(cat /sys/class/dmi/id/product_serial 2>/dev/null)
case "$(printf '%s' "$serial" | tr '[:upper:]' '[:lower:]')" in
    ''|'to be filled by o.e.m.'|'unknown'|'none') serial=$(tr -d '\000' 2>/dev/null </proc/device-tree/serial-number) ;;
esac
""" + 'if [ -z "$serial" ]; then serial=$(python3 -I -c ' + shlex.quote(
    Path(__file__).with_name('onie_target.py').read_text(encoding='utf-8')) + ' 2>/dev/null); fi\n'
IDENTITY = READ_IDENTITY + r"""
printf 'serial=%s\n' "$serial"
printf 'boot_id='; cat /proc/sys/kernel/random/boot_id
"""
BOOT_FACTS = r"""
printf 'model='; tr -d '\000' </proc/device-tree/model; printf '\n'
printf 'firmware='; cat /sys/class/dmi/id/bios_version
printf 'distribution='; sed -n 's/^ID=//p' /etc/os-release
printf 'multi_user='; systemctl is-active multi-user.target
printf 'root='; findmnt -n -o UUID,FSTYPE,OPTIONS /
rootdev=$(lsblk -no PKNAME "$(findmnt -n -o SOURCE /)")
printf 'root_path='; readlink -f "/sys/class/block/$rootdev/device"
printf 'disk_model='; cat "/sys/class/block/$rootdev/device/model"
printf 'esp_partuuid='; lsblk -no PARTUUID "$(findmnt -n -o SOURCE /boot/efi)"
efi=$(efibootmgr -v)
current=$(printf '%s\n' "$efi" | sed -n 's/^BootCurrent: //p')
printf '%s\n' "$efi" | sed -n "s/^Boot${current}[* ]/entry=/p"
"""


def saved_case(row):
    return dict(row, kind='shell', commands=json.loads(row['commands']))


def library(store):
    with store.lock:
        custom = [saved_case(row) for row in store.db.execute(
            'SELECT * FROM board_test_cases ORDER BY name, id')]
    return {'cases': [dict(case, script=BUILTIN_COMMANDS[case['id']]) for case in BUILTIN_CASES] + custom}


def shell_text(value):
    if (not isinstance(value, str) or not value.strip() or len(value.encode()) > 8192
            or any(ord(char) < 32 and char not in '\n\r\t' for char in value)):
        raise TaskError('请填写不超过 8 KiB 的 Shell 命令')
    return value.replace('\r\n', '\n').replace('\r', '\n')


def command_steps(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise TaskError('请填写 1 到 16 条命令')
    steps = []
    for step in value:
        if not isinstance(step, dict) or set(step) - {'command', 'expected'}:
            raise TaskError('无法识别命令字段')
        command = shell_text(step.get('command'))
        expected = step.get('expected', '')
        if (not isinstance(expected, str) or len(expected.encode()) > 256
                or any(ord(char) < 32 and char != '\t' for char in expected)):
            raise TaskError('每条预期串口输出须为不超过 256 字节的单行文本')
        steps.append({'command':command, 'expected':expected})
    if sum(len((step['command'] + step['expected']).encode()) for step in steps) > 8192:
        raise TaskError('全部命令和预期输出合计不得超过 8 KiB')
    return steps


def save_case(store, payload):
    if set(payload) - {'id', 'name', 'script', 'commands', 'timeout', 'revision'}:
        raise TaskError('无法识别用例字段')
    name = payload.get('name')
    timeout, revision = payload.get('timeout'), payload.get('revision')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 or not name.isprintable():
        raise TaskError('请填写最多 80 字符的用例名称')
    if ('script' in payload) == ('commands' in payload):
        raise TaskError('请提供命令列表或一段 Shell 脚本')
    commands = command_steps(payload['commands']) if 'commands' in payload else []
    script = '' if commands else shell_text(payload.get('script'))
    if type(timeout) is not int or not 1 <= timeout <= 180:
        raise TaskError('每轮超时须为 1 到 180 秒')
    name, document = name.strip(), json.dumps(commands, ensure_ascii=False)
    case_id = payload.get('id')
    with store.lock, store.db:
        if case_id is None and revision is None:
            case_id, revision = 'shell-' + secrets.token_hex(12), 1
            store.db.execute('INSERT INTO board_test_cases (id,name,script,timeout,revision,commands) VALUES (?,?,?,?,?,?)',
                             (case_id, name, script, timeout, revision, document))
        else:
            if not isinstance(case_id, str) or not CUSTOM_CASE.fullmatch(case_id) or type(revision) is not int or revision < 1:
                raise TaskError('请选择要编辑的 Shell 用例')
            changed = store.db.execute('UPDATE board_test_cases SET name=?, script=?, timeout=?, commands=?, revision=revision+1 '
                                       'WHERE id=? AND revision=?', (name, script, timeout, document, case_id, revision))
            if changed.rowcount != 1:
                raise TaskError('用例已更新，请重新打开后编辑', 409)
            revision += 1
    return saved_case(dict(id=case_id, name=name, script=script, timeout=timeout, revision=revision, commands=document))


def config(document):
    if not isinstance(document, dict):
        raise ValueError('board_tests must map board serials to test configuration')
    ports = set()
    for serial, row in document.items():
        if (not isinstance(serial, str) or not serial or len(serial.encode()) > 100
                or not isinstance(row, dict) or set(row) != {'usb_port', 'uart', 'cases'}
                or not isinstance(row['usb_port'], str) or not USB_PORT.fullmatch(row['usb_port'])
                or not isinstance(row['uart'], str) or not row['uart'].startswith('/dev/') or not UART.fullmatch(row['uart'][5:])
                or not isinstance(row['cases'], list) or not row['cases']
                or any(case not in CASES for case in row['cases'])
                or len(set(row['cases'])) != len(row['cases']) or row['usb_port'] in ports):
            raise ValueError('board_tests requires distinct USB ports, a board UART and supported case IDs')
        ports.add(row['usb_port'])
    return document


def usb_adb_interface(port):
    # An enumerated USB function can survive loss of its adbd transport.
    if not USB_PORT.fullmatch(port):
        return False
    for interface in Path('/sys/bus/usb/devices').glob(port + ':*'):
        try:
            if tuple((interface / field).read_text().strip().lower() for field in
                     ('bInterfaceClass', 'bInterfaceSubClass', 'bInterfaceProtocol')) == ('ff', '42', '01'):
                return True
        except OSError:
            continue
    return False


def select_transport(output, port):
    matches = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == 'device' and 'usb:' + port in fields[2:]:
            ids = [value.split(':')[1] for value in fields[2:] if re.fullmatch(r'transport_id:[1-9]\d*', value)]
            if len(ids) != 1:
                raise ConnectionError('USB 调试连接缺少唯一的 transport_id')
            matches.append(ids[0])
    if not matches:
        if usb_adb_interface(port):
            raise ConnectionError(f'USB {port} 已枚举 ADB 接口，但控制机没有在线调试连接。请在板端串口'
                                  '检查 systemctl status adbd；在 Debian 上可执行 systemctl restart adbd 后重试。')
        raise ConnectionError(f'ADB 未在已登记的 USB 路径 {port} 发现在线设备。请检查调试线和板端 adbd；'
                              '如果换过 USB 口，请核对登记的路径。')
    if len(matches) > 1:
        raise ConnectionError(f'USB {port} 报告了多个在线 ADB 连接，请核对控制机的 adb devices -l。')
    return matches[0]

def adb_command(executable, *args, timeout=10):
    return subprocess.run([executable, *args], capture_output=True, text=True, errors='replace', timeout=timeout,
                          env={**os.environ, 'ADB_SERVER_SOCKET': 'tcp:127.0.0.1:5037'})


class AdbTransportUnavailable(ConnectionError):
    """No online transport could be identified for the requested board."""


def adb_listing(executable):
    result = adb_command(executable, 'devices', '-l', timeout=3)
    if result.returncode:
        raise ConnectionError('无法读取控制机的 USB 调试连接')
    return result.stdout


def transport_identity(executable, endpoint, timeout):
    try:
        response = adb_command(executable, '-t', endpoint, 'shell',
            IDENTITY + "printf 'uid='; id -u; printf 'consoles='; cat /sys/class/tty/console/active",
            timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return {} if response.returncode else dict(line.split('=', 1) for line in response.stdout.splitlines() if '=' in line)


class AdbDiscoveryScan:
    """One discovery pass. Each ADB transport in its listing is probed at most once."""
    def __init__(self, executable):
        self.listing = adb_listing(executable)
        self.executable = executable
        self._lock = threading.Lock()
        self._endpoint_locks = {}
        self._facts = {}

    def identity(self, endpoint, timeout):
        with self._lock:
            lock = self._endpoint_locks.setdefault(endpoint, threading.Lock())
        with lock:
            if endpoint not in self._facts:
                self._facts[endpoint] = transport_identity(self.executable, endpoint, timeout)
            return self._facts[endpoint]


class AdbDiscoveryScans:
    def __init__(self):
        self._lock = threading.Lock()
        self._scans = {}

    def start(self):
        executable = shutil.which('adb')
        if not executable:
            raise TaskError('控制机缺少 adb', 503)
        try:
            scan = AdbDiscoveryScan(executable)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TaskError('控制机暂时无法读取 USB 调试连接', 503) from error
        key = secrets.token_hex(16)
        with self._lock:
            now = time.monotonic()
            self._scans = {k: v for k, v in self._scans.items() if v[0] > now}
            if len(self._scans) >= 16:
                del self._scans[next(iter(self._scans))]
            self._scans[key] = (now + 300, scan)
        return key

    def get(self, key):
        with self._lock:
            expires, scan = self._scans.get(key, (0, None))
            if expires <= time.monotonic():
                raise TaskError('ADB 发现轮次已结束', 409)
            return scan


def find_transport(executable, serial, registered, *, usb_endpoints=None, allow_relocation=False, on_stage=None, scan=None):
    registered_port = registered['usb_port'] if registered else None
    stage = on_stage or (lambda *args: None)
    stage('listing-adb-usb', registered_port)
    listing = scan.listing if scan is not None else adb_listing(executable)
    online_ports = {
        field[4:] for fields in map(str.split, listing.splitlines()) if len(fields) >= 2 and fields[1] == 'device'
        for field in fields[2:]
        if field.startswith('usb:') and USB_PORT.fullmatch(field[4:])}
    # Only a new operation can follow a moved board. Follow its confirmed
    # identity, never the ADB serial or the first newly enumerated device.
    inspect_other_ports = allow_relocation and bool(online_ports.difference({registered_port}))
    ports = online_ports if not registered or inspect_other_ports else {registered_port}
    rows = [fields for fields in map(str.split, listing.splitlines()) if len(fields) >= 2
            and (fields[1] in ('device', 'offline', 'unauthorized') or fields[1:3] == ['no', 'permissions'])]
    relevant = [row for row in rows if len(row) >= 2 and
                (not registered or 'usb:' + registered['usb_port'] in row[2:])]
    if allow_relocation and not relevant and not online_ports:
        visible = sorted({entry.name.split(':', 1)[0] for entry in Path('/sys/bus/usb/devices').glob('*:*')
                          if USB_PORT.fullmatch(entry.name.split(':', 1)[0])})
        interfaces = [port for port in visible if usb_adb_interface(port)]
        if interfaces:
            raise AdbTransportUnavailable('控制机在 USB ' + '、'.join(interfaces)
                                           + ' 看到了 ADB 接口，当前没有在线会话可供核对板卡身份。')
    if not registered and not rows:
        raise AdbTransportUnavailable('控制机 ADB 列表为空，请检查 USB 调试线和板端 ADB 服务。')
    if not inspect_other_ports and relevant and all(row[1] == 'no' for row in relevant):
        raise ConnectionError('控制机没有访问 ADB USB 设备的权限，请检查 USB 设备权限。')
    if not inspect_other_ports and relevant and all(row[1] == 'unauthorized' for row in relevant):
        raise ConnectionError('USB 调试未授权，请在板端授权控制机的 ADB 密钥')
    if not inspect_other_ports and relevant and all(row[1] == 'offline' for row in relevant):
        raise AdbTransportUnavailable('登记端口的 USB 调试连接离线')
    if allow_relocation and registered_port and not online_ports and not relevant:
        raise AdbTransportUnavailable('控制机未发现可核对板号的在线 USB 调试连接。确认板端 adbd 正常运行后重试，换 USB 口时会自动查找。')
    candidates = [(port, select_transport(listing, port))
                  for port in sorted(ports, key=lambda p: (p != registered_port, p))]
    if usb_endpoints and not registered and any(row[1] == 'device' and not any(
            field.startswith('usb:') and USB_PORT.fullmatch(field[4:]) for field in row[2:]) for row in relevant):
        extra = usb_endpoints()
        known = {endpoint for _, endpoint in candidates}
        candidates.extend((port, endpoint) for port, endpoint in extra if endpoint not in known)
    matches = []
    deadline = time.monotonic() + 8
    if candidates:
        stage('matching-adb-board')
    for port, endpoint in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConnectionError('USB 调试连接探测超时，请检查接线或登记板卡 USB 路径')
        timeout = min(3, remaining)
        facts = scan.identity(endpoint, timeout) if scan is not None else transport_identity(executable, endpoint, timeout)
        if facts.get('serial') == serial and BOOT_ID.fullmatch(facts.get('boot_id', '')):
            if port == registered_port:
                stage('verified-adb-port', port)
                return port, endpoint, facts
            matches.append((port, endpoint, facts))
    if len(matches) > 1:
        raise ConnectionError('多个 USB 调试连接报告同一板卡身份，请核对接线')
    if not matches:
        if relevant and all(row[1] == 'offline' for row in relevant):
            message = '登记端口的 USB 调试连接离线' if registered else '控制机只发现离线的 USB 调试连接'
            raise AdbTransportUnavailable(message)
        raise AdbTransportUnavailable('未找到与当前板卡匹配的在线 USB 调试连接，请检查数据线和板端 ADB 服务')
    stage('verified-moved-adb-port' if registered_port else 'verified-adb-port', matches[0][0])
    return matches[0]


def resolve_connection(executable, serial, registered):
    port, _, facts = find_transport(executable, serial, registered, allow_relocation=True)
    if facts.get('uid') != '0':
        raise ConnectionError('当前板卡的 USB 调试连接缺少 root 权限')
    if registered:
        return dict(registered, usb_port=port, cases=list(dict.fromkeys([*registered['cases'], 'rtc-wake'])))
    consoles = [name for name in facts.get('consoles', '').split() if UART.fullmatch(name)]
    if len(consoles) != 1:
        raise ConnectionError('无法唯一确认板端串口控制台，请在硬件配置中登记接线')
    return {'usb_port':port, 'uart':'/dev/' + consoles[0], 'cases':['rtc-wake']}


def adb_binding(manager, device, scan=None):
    """Return the currently verified USB path for a board."""
    serial, generation = device.serial, device.connection_id
    if not serial or not device.present or manager.store.has_conflict(serial):
        raise TaskError('板卡尚未在线或身份有冲突', 409)
    executable = shutil.which('adb')
    if not executable:
        raise TaskError('控制机缺少 adb', 503)
    try:
        port, _, facts = find_transport(executable, serial, manager.hardware.board_tests.get(serial), allow_relocation=True, scan=scan)
    except ConnectionError as error:
        raise TaskError(str(error), 409) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TaskError('控制机暂时无法读取 USB 调试连接', 503) from error
    if (not USB_PORT.fullmatch(port) or device.serial != serial or device.connection_id != generation
            or not device.present or manager.store.has_conflict(serial)):
        raise TaskError('USB 连接已变化，请重新检测', 409)
    return {'serial': serial, 'connection_id': generation, 'usb_port': port, 'boot_id': facts['boot_id']}


def case_availability(manager, device):
    executable = shutil.which('adb')
    if not executable:
        return {'test_cases': [], 'test_cases_reason': '控制机缺少 adb'}
    if not device.present or not device.serial:
        return {'test_cases': [], 'test_cases_reason': '请先连接并识别当前板卡的串口'}
    registered = manager.hardware.board_tests.get(device.serial)
    try:
        row = resolve_connection(executable, device.serial, registered)
    except ConnectionError as error:
        return {'test_cases': [], 'test_cases_reason': str(error)}
    except (OSError, subprocess.TimeoutExpired):
        return {'test_cases': [], 'test_cases_reason': 'USB 调试连接暂时不可用，请重试检测'}
    return {'test_cases': row['cases'], 'test_case_connection': {key: row[key] for key in ('usb_port', 'uart')}}


def verify_boot(facts, expected=None):
    root = facts.get('root', '').split()
    entry, esp = facts.get('entry', ''), facts.get('esp_partuuid', '')
    if (facts.get('model') != 'SpacemiT K3 Pico-ITX' or facts.get('multi_user') != 'active'
            or facts.get('distribution', '').strip('"') != 'debian' or not facts.get('firmware')
            or len(root) != 3 or root[1] != 'ext4' or 'rw' not in root[2].split(',')
            or not re.search(r'(?:\.ufshc|/ufs@[^/]*)(?:/|$)', facts.get('root_path', ''))
            or facts.get('disk_model', '').strip() != 'TY7B-128'
            or '/UFS(' not in entry or not BOOT_ID.fullmatch(esp.lower())
            or not BOOT_ID.fullmatch(root[0].lower())
            or '/HD(' in entry and esp.lower() not in entry.lower()):
        raise ValueError('未确认从预期的 UFS 启动项和根盘进入 Debian 多用户状态')
    identity = (facts['firmware'], root[0], esp, entry)
    if expected is not None and identity != expected:
        raise ValueError('固件、EFI 启动项、ESP 或根盘与起始基线不一致')
    return identity


class SerialEvidence:
    def __init__(self, device):
        self.device = device
        self.offset = device.now_off()
        self.connection = device.connection_id
        self.data = bytearray()
        self.incomplete = False

    def read(self, current):
        if current is not self.device or self.connection != self.device.connection_id or not self.device.present:
            self.incomplete = True
        with self.device.cv:
            end = self.device.base + len(self.device.buf)
            start = max(0, self.offset - self.device.base)
            self.incomplete |= self.offset < self.device.base
            left = MAX_SERIAL - len(self.data)
            data = self.device.buf[start:start + left]
            self.data.extend(data)
            self.incomplete |= len(data) < end - max(self.offset, self.device.base)
            self.offset = end


class BoardCase:
    def __init__(self, manager, device, row, case, rounds, seconds, executable, custom=None):
        self.manager, self.device, self.serial = manager, device, device.serial
        self.row, self.case, self.rounds, self.seconds = row, case, rounds, seconds
        self.adb = executable
        self.maintenance = maintenance_for(manager)
        self.connection = device.connection_id
        self.board = device.board
        self.custom = custom
        self.registered = manager.hardware.board_tests.get(self.serial)

    def command(self, *args):
        return adb_command(self.adb, *args)

    def endpoint(self):
        listing = self.command('devices', '-l')
        if listing.returncode:
            raise ConnectionError('无法读取本机 USB 调试设备')
        return select_transport(listing.stdout, self.row['usb_port'])

    def facts(self, endpoint, record):
        response = self.command('-t', endpoint, 'shell', IDENTITY + (BOOT_FACTS if self.case == 'reboot-aging' else ''))
        record.update({'transport_id': endpoint, 'usb_port': self.row['usb_port'],
                       'stdout': response.stdout[:8192], 'stderr': response.stderr[:2048],
                       'exit_code': response.returncode})
        values = dict(line.split('=', 1) for line in response.stdout.splitlines() if '=' in line)
        if response.returncode or not values.get('serial') or not BOOT_ID.fullmatch(values.get('boot_id', '')):
            raise ConnectionError('板卡身份回读尚未完成')
        if values['serial'] != self.serial:
            raise ValueError('USB 路径上的板卡序列号与任务目标不同')
        return values

    def artifact(self, context, name, source):
        size = source.tell()
        if not size:
            return None
        artifacts = self.manager.artifacts
        row = artifacts.create('download', name, size, 'web', self.serial, context.id)
        try:
            source.seek(0)
            offset = 0
            while chunk := source.read(65536):
                artifacts.append(row['id'], offset, chunk, 'web', self.serial, context.id)
                offset += len(chunk)
            return artifacts.seal(row['id'], 'web', self.serial, context.id)['id']
        except Exception:
            artifacts.fail(row['id'], context.id)
            raise

    def preflight(self, context, row):
        deadline = time.monotonic() + 20
        while True:
            context.check()
            try:
                endpoint = self.endpoint()
                return endpoint, self.facts(endpoint, row['preflight'])
            except (ConnectionError, OSError, subprocess.TimeoutExpired):
                if time.monotonic() >= deadline:
                    raise ConnectionError('板卡 USB 调试连接未恢复，未触发本轮') from None
                if context.cancelled.wait(1):
                    context.check()

    def observe(self, tag, before, baseline, evidence, row, *, step_timeout=None, expected=''):
        timeout = 235 if self.case == 'rtc-wake' else 180
        if self.case not in CASES:
            timeout = (step_timeout if step_timeout is not None else self.custom['timeout']) + 25
        deadline = time.monotonic() + timeout
        result_line = re.compile(re.escape(tag.encode()) + rb' RESULT (\{[^\n]*\})\r*\n')
        while time.monotonic() < deadline:
            evidence.read(getattr(self.board, 'connection', None))
            row['armed'] = (tag + ' ARMED').encode() in evidence.data
            row['issued_on_board'] = (tag + ' ISSUED').encode() in evidence.data
            row['suspend_exit_seen'] = b'PM: suspend exit' in evidence.data
            match = result_line.search(evidence.data)
            if match:
                try:
                    result = json.loads(match[1])
                    if (result['serial'] != self.serial or result['boot_id'] != before['boot_id']
                            or result['status'] not in ('passed', 'failed', 'uncertain')):
                        return 'uncertain', '串口结果的身份无法核对'
                    if (result['status'] != 'passed' or (self.case == 'rtc-wake' and row['armed'])
                            or (self.case not in CASES and row['issued_on_board'])):
                        row['boot_after'] = result['boot_id']
                        row['round_finished'] = True
                        if step_timeout is not None and result['status'] == 'passed' and expected:
                            marker = re.compile(re.escape(tag.encode()) + rb' OUTPUT\r*\n')
                            start = marker.search(evidence.data, 0, match.start())
                            end = evidence.data.rfind(tag.encode() + b' OUTPUT-END', 0, match.start())
                            if not start or end < start.end() or evidence.incomplete:
                                return 'uncertain', '本条命令的串口输出记录不完整'
                            row['expected_matched'] = evidence.data.find(expected.encode(), start.end(), end) >= 0
                            if not row['expected_matched']:
                                return 'failed', '未看到预期串口输出'
                        return result['status'], result['reason'][:256]
                except (ValueError, KeyError, TypeError):
                    pass
            if self.case == 'reboot-aging':
                readback = {}
                try:
                    after = self.facts(self.endpoint(), readback)
                    if after['boot_id'] != before['boot_id'] and after.get('multi_user') == 'active':
                        row['boot_after'] = after['boot_id']
                        row['round_finished'] = True
                        verify_boot(after, baseline)
                        if row['issued_on_board'] and re.search(rb'(?m)^nor m:0x[0-9a-f]+ d:0x[0-9a-f]+', evidence.data):
                            return 'passed', '已核对新启动编号、NOR、UFS 和 Debian'
                except (ConnectionError, OSError, subprocess.TimeoutExpired):
                    pass
                except ValueError as error:
                    return ('failed' if row.get('boot_after') else 'uncertain'), str(error)
                finally:
                    if readback:
                        row['last_readback'] = readback
            time.sleep(1)
        row['timeout_seconds'] = timeout
        return 'uncertain', '观察超时，本轮证据不足'

    def run(self, context):
        recent, completed = [], 0
        result = {'case': self.case, 'completed_rounds': 0, 'recent_rounds': recent}
        if self.case not in CASES:
            result.update(case_name=self.custom['name'], case_revision=self.custom['revision'])
        first_issue = None
        report = serial_log = None
        in_flight = False
        try:
            report = tempfile.TemporaryFile()
            serial_log = tempfile.TemporaryFile()
            if not self.device.present or self.device.stop.is_set() or self.device.connection_id != self.connection:
                raise TaskError('板卡串口连接已改变', 409)
            if self.manager.hardware.board_tests.get(self.serial) != self.registered:
                raise TaskError('板卡用例接线已变化', 409)
            script = Path(__file__).with_name('board_case_target.py').read_text(encoding='utf-8')
            start = time.monotonic()
            baseline = None
            expected_boot = None
            for number in range(1, (self.rounds or 10000) + 1):
                if context.cancelled.is_set():
                    break
                if number > 1 and self.seconds and time.monotonic() - start >= self.seconds:
                    break
                context.progress(completed, self.rounds or 0, 'board-test-round')
                row = {'round': number, 'trigger': 'not-issued', 'preflight': {}}
                evidence = None
                try:
                    device = getattr(self.board, 'connection', None)
                    if device is None or not device.present:
                        raise ValueError('未确认目标板串口连接')
                    if self.manager.hardware.board_tests.get(self.serial) != self.registered:
                        raise ValueError('板卡用例接线已变化')
                    endpoint, before = self.preflight(context, row)
                    row['boot_before'] = before['boot_id']
                    if expected_boot is not None and before['boot_id'] != expected_boot:
                        raise ValueError('轮次之间出现未经本任务确认的启动')
                    if self.case == 'reboot-aging':
                        baseline = verify_boot(before, baseline)
                    evidence = SerialEvidence(device)
                    steps = self.custom.get('commands', []) if self.case not in CASES else []
                    if steps:
                        row['commands'] = []
                        deadline = time.monotonic() + self.custom['timeout']
                    for index, step in enumerate(steps or [None], 1):
                        context.check()
                        tag = f'DECKLAB-CASE {context.id} {number}'
                        unit = f'decklab-case-{context.id}-{number}'
                        current = row
                        target_case, arguments, observation = self.case, [], {}
                        if step is not None:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                row.update(status='failed', reason=f'第 {index} 条命令开始前已达到本轮超时')
                                break
                            tag += f' STEP {index}'
                            unit += f'-{index}'
                            current = {'index': index, 'expected': step['expected']}
                            target_case = 'shell-step'
                            timeout = math.ceil(remaining)
                            arguments = [base64.b64encode(step['command'].encode()).decode(), str(timeout)]
                            observation = {'step_timeout': timeout, 'expected': step['expected']}
                        elif self.case not in CASES:
                            target_case = 'shell'
                            arguments = [base64.b64encode(self.custom['script'].encode()).decode(), str(self.custom['timeout'])]
                        payload = 'LC_ALL=C ' + shlex.join(['systemd-run', '--no-block', '--collect', '--unit=' + unit,
                                                           '--property=RuntimeMaxSec=230s', '/usr/bin/python3', '-c', script,
                                                           target_case, self.row['uart'], tag, self.serial, before['boot_id'],
                                                           *arguments])
                        context.issued()
                        if step is not None:
                            row['commands'].append(current)
                            result.update(current_command=index, total_commands=len(steps))
                        current.update(trigger='unknown', service=unit)
                        result.update(in_flight=number, service=unit)
                        receipt = {'command': payload, 'started_at': time.time(), 'state': 'running',
                                   'usb_port': self.row['usb_port'], 'transport_id': endpoint,
                                   'round': number, 'exit_code': None,
                                   'serial_window': {'connection_id': evidence.connection, 'start_off': evidence.offset}}
                        result['adb_receipt'] = receipt
                        context.store.update(context.id, result=result)
                        in_flight = True
                        try:
                            submitted = self.command('-t', endpoint, 'shell', payload)
                            current['submit'] = {'exit_code': submitted.returncode, 'stdout': submitted.stdout[:2048],
                                                 'stderr': submitted.stderr[:2048]}
                            receipt.update(adb_exit_code=submitted.returncode, finished_at=time.time())
                            for name in ('stdout', 'stderr'):
                                data = getattr(submitted, name).encode()
                                receipt[name] = data[:1024].decode(errors='replace')
                                receipt[name + '_truncated_bytes'] = max(0, len(data) - 1024)
                            if submitted.returncode == 0:
                                current['trigger'] = 'acknowledged'
                                receipt.update(state='exited', exit_code=0)
                            elif rejected := START_REJECTED.search(submitted.stderr[:2048]):
                                current.update(trigger='not-issued', status='failed',
                                               reason='临时服务启动失败：' + rejected[0][:220])
                                in_flight = False
                                receipt.update(state='rejected', exit_code=submitted.returncode)
                                context.mark_outcome('not-issued' if index == 1 else 'observed')
                            else:
                                receipt['state'] = 'unconfirmed'
                        except (OSError, subprocess.TimeoutExpired) as error:
                            current['submit'] = {'error': '触发回执未收到，继续观察原轮次'}
                            receipt.update(state='unconfirmed', finished_at=time.time())
                            if isinstance(error, subprocess.TimeoutExpired):
                                for name in ('stdout', 'stderr'):
                                    data = getattr(error, name) or b''
                                    if isinstance(data, str):
                                        data = data.encode()
                                    receipt[name] = data[:1024].decode(errors='replace')
                                    receipt[name + '_truncated_bytes'] = max(0, len(data) - 1024)
                        context.store.update(context.id, result=result)
                        if in_flight:
                            current['status'], current['reason'] = self.observe(tag, before, baseline, evidence, current, **observation)
                            in_flight = not current.get('round_finished', False)
                        if evidence.incomplete and current['status'] == 'passed':
                            current.update(status='uncertain', reason='原串口证据有缺口')
                        if step is not None:
                            if current.get('round_finished'):
                                context.mark_outcome('observed')
                            row.update(status=current['status'], reason=current['reason'], service=unit,
                                       trigger=current['trigger'], boot_after=current.get('boot_after'))
                            if current['status'] != 'passed':
                                row['reason'] = f"第 {index} 条命令 · {current['reason']}"
                                if current.get('expected_matched') is False:
                                    row['failed_expected'] = step['expected']
                            elif index == len(steps):
                                row['reason'] = '全部命令及预期串口输出通过'
                        if current['status'] != 'passed':
                            break
                except (ConnectionError, OSError, ValueError, subprocess.TimeoutExpired) as error:
                    row.update(status='uncertain', reason=str(error)[:256])
                except TaskCancelled:
                    if not row.get('commands'):
                        break
                    row.update(status='cancelled', reason='已停止，剩余命令未执行')
                if evidence:
                    row['serial_incomplete'] = evidence.incomplete
                    if evidence.data:
                        offset = serial_log.tell()
                        if offset + len(evidence.data) <= self.manager.artifacts.policy.max_file:
                            try:
                                serial_log.write(evidence.data)
                                row.update(serial_offset=offset, serial_size=len(evidence.data),
                                           serial_lines=evidence.data.count(b'\n') + int(not evidence.data.endswith(b'\n')))
                            except OSError as error:
                                row['serial_artifact_error'] = str(error)
                        else:
                            row['serial_artifact_error'] = '本次串口归档已达到文件大小上限'
                if row['status'] == 'passed':
                    completed += 1
                    expected_boot = row.get('boot_after')
                    context.mark_outcome('observed')
                else:
                    first_issue = row
                report.write((json.dumps(row, ensure_ascii=False) + '\n').encode())
                summary = {key: value for key, value in row.items()
                           if key not in ('preflight', 'submit', 'last_readback', 'commands')}
                if row.get('commands'):
                    summary['command_results'] = [
                        {key: command[key] for key in ('index', 'status', 'expected_matched') if key in command}
                        for command in row['commands']]
                if first_issue:
                    result['first_issue'] = summary
                recent.append(summary)
                del recent[:-20]
                result.update(completed_rounds=completed, in_flight=None)
                result.pop('current_command', None)
                context.store.update(context.id, result=result)
                if first_issue or context.cancelled.is_set():
                    break
            if serial_log.tell():
                try:
                    result['serial_artifact_id'] = self.artifact(context, f'{context.id}-serial.log', serial_log)
                except (OSError, TaskError) as error:
                    result['serial_artifact_error'] = str(error)
            if report.tell():
                try:
                    result['report_artifact_id'] = self.artifact(context, f'{context.id}-rounds.jsonl', report)
                except (OSError, TaskError) as error:
                    result['report_artifact_error'] = str(error)
            context.store.update(context.id, result=result)
            context.check()
            if first_issue:
                raise TaskError(first_issue['reason'], 502)
            context.progress(completed, self.rounds or 0, 'board-test-completed')
            return result
        finally:
            if report:
                report.close()
            if serial_log:
                serial_log.close()
            if in_flight:
                self.maintenance.keep_recovery(context.id)
                FlashStore(self.manager.store).update_details(context.id, {'recovery_required': True})
            else:
                self.maintenance.release(context.id, confirmed=True)


def start(manager, device, payload, actor):
    if actor != 'web':
        raise TaskError('板卡用例仅限主访问身份', 403)
    if set(payload) - {'kind', 'key', 'serial', 'actor', 'client_id', 'connection_id', 'request_id',
                       'case', 'case_revision', 'rounds', 'seconds'}:
        raise TaskError('用例仅接受已保存的用例和轮数或时长')
    case, rounds, seconds = payload.get('case'), payload.get('rounds'), payload.get('seconds')
    if ((rounds is None) == (seconds is None) or
            (rounds is not None and (type(rounds) is not int or not 1 <= rounds <= 10000)) or
            (seconds is not None and (type(seconds) is not int or not 1 <= seconds <= 7 * 86400))):
        raise TaskError('请填写轮数或时长')
    registered = manager.hardware.board_tests.get(device.serial)
    executable = shutil.which('adb')
    revision = payload.get('case_revision')
    custom_case = isinstance(case, str) and CUSTOM_CASE.fullmatch(case) is not None
    if ((custom_case and (type(revision) is not int or revision < 1))
            or (not custom_case and 'case_revision' in payload)):
        raise TaskError('请重新选择所保存的用例版本')
    if case not in CASES and not custom_case:
        raise TaskError('未知板卡用例')
    request_id = payload.get('request_id')
    inputs = {'serial': device.serial, 'case': case, 'rounds': rounds, 'seconds': seconds}
    if custom_case:
        inputs['case_revision'] = revision
    digest = manager.tasks.store.request_digest(inputs)
    validate_request(request_id, digest)
    previous = FlashStore(manager.store).find_request(actor, request_id, digest)
    if previous is not None:
        return manager.tasks.store.get(previous)
    if not executable:
        raise TaskError('控制机缺少 adb', 409)
    if case == 'reboot-aging' and (not registered or case not in registered['cases']):
        raise TaskError('软件重启老化用例需要配置预期的启动环境', 409)
    try:
        row = resolve_connection(executable, device.serial, registered)
    except ConnectionError as error:
        raise TaskError(str(error), 409) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TaskError('USB 调试连接暂时不可用，请重试检测', 409) from error
    custom = None
    if custom_case:
        with manager.store.lock:
            saved = manager.store.db.execute('SELECT * FROM board_test_cases WHERE id=? AND revision=?', (case, revision)).fetchone()
        if saved is None:
            raise TaskError('用例已更新，重新选择后再运行', 409)
        custom = saved_case(saved)
    if not device.present or device.connection_id != payload['connection_id']:
        raise TaskError('板卡串口连接已变化', 409)
    if manager.hardware.board_tests.get(device.serial) != registered:
        raise TaskError('板卡用例接线已变化', 409)
    operation = BoardCase(manager, device, row, case, rounds, seconds, executable, custom=custom)
    return manager.tasks.start_board('board-test', actor, payload.get('client_id', ''), device.serial, operation.run,
        request_id=request_id, input_digest=digest, details={'attempt_id': request_id}, resources=['board-test-usb:' + row['usb_port']],
        maintenance=operation.maintenance, connection_id=device.connection_id, tracks_outcome=True)
