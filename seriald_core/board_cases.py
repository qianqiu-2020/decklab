"""One-round-at-a-time board tests supervised by the collector."""
import base64
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
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
     ]},
    {'id': 'rtc-wake', 'name': '深度挂起与 RTC 唤醒', 'kind': 'builtin',
     'description': '每轮设置 35 秒 RTC 闹钟，进入深度挂起后通过串口核对唤醒。',
     'steps': [
         '检查 /sys/power/mem_sleep 选中 deep、pm_test 为 none、console_suspend 为 Y、RTC 没有旧闹钟，记录 rtc@pmic 中断计数和内核日志游标。',
         '执行 rtcwake -m no -s 35 设置闹钟，向串口写入布防标记，再执行 systemctl suspend。',
         '同一次启动内必须出现一次 deep 进入和一次退出，RTC 中断增加，systemd-suspend 成功，闹钟清除。',
     ]},
)
CASES = tuple(case['id'] for case in BUILTIN_CASES)
BUILTIN_COMMANDS = {
    'reboot-aging': 'systemctl --no-block reboot',
    'rtc-wake': 'rtcwake -m no -s 35\nsystemctl suspend',
}
CUSTOM_CASE = re.compile(r'shell-[0-9a-f]{24}')
MAX_SERIAL = 8 * 1024 * 1024
BOOT_ID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
START_REJECTED = re.compile(
    r'^Failed to start transient service unit: (?:Cannot set property\b.*|Unknown property\b.*|'
    r'Access denied\.?|Permission denied\.?|Invalid argument\.?)$', re.MULTILINE | re.IGNORECASE)
IDENTITY = r"""
serial=$(cat /sys/class/dmi/id/product_serial 2>/dev/null)
case "$(printf '%s' "$serial" | tr '[:upper:]' '[:lower:]')" in
    ''|'to be filled by o.e.m.'|'unknown'|'none') serial=$(tr -d '\000' </proc/device-tree/serial-number);;
esac
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


def library(store):
    with store.lock:
        custom = [dict(row, kind='shell') for row in store.db.execute(
            'SELECT * FROM board_test_cases ORDER BY name, id')]
    return {'cases': [dict(case, script=BUILTIN_COMMANDS[case['id']]) for case in BUILTIN_CASES] + custom}


def save_case(store, payload):
    if set(payload) - {'id', 'name', 'script', 'timeout', 'revision'}:
        raise TaskError('无法识别用例字段')
    name, script = payload.get('name'), payload.get('script')
    timeout, revision = payload.get('timeout'), payload.get('revision')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 or not name.isprintable():
        raise TaskError('请填写最多 80 字符的用例名称')
    if (not isinstance(script, str) or not script.strip() or len(script.encode()) > 8192
            or any(ord(char) < 32 and char not in '\n\r\t' for char in script)):
        raise TaskError('请填写不超过 8 KiB 的 Shell 脚本')
    if type(timeout) is not int or not 1 <= timeout <= 180:
        raise TaskError('每轮超时须为 1 到 180 秒')
    name, script = name.strip(), script.replace('\r\n', '\n').replace('\r', '\n')
    case_id = payload.get('id')
    with store.lock, store.db:
        if case_id is None and revision is None:
            case_id, revision = 'shell-' + secrets.token_hex(12), 1
            store.db.execute('INSERT INTO board_test_cases VALUES (?,?,?,?,?)', (case_id, name, script, timeout, revision))
        else:
            if not isinstance(case_id, str) or not CUSTOM_CASE.fullmatch(case_id) or type(revision) is not int or revision < 1:
                raise TaskError('请选择要编辑的 Shell 用例')
            changed = store.db.execute('UPDATE board_test_cases SET name=?, script=?, timeout=?, revision=revision+1 '
                                       'WHERE id=? AND revision=?', (name, script, timeout, case_id, revision))
            if changed.rowcount != 1:
                raise TaskError('用例已更新，请重新打开后编辑', 409)
            revision += 1
    return {'id': case_id, 'name': name, 'script': script, 'timeout': timeout, 'revision': revision, 'kind': 'shell'}


def config(document):
    if not isinstance(document, dict):
        raise ValueError('board_tests must map board serials to test configuration')
    ports = set()
    for serial, row in document.items():
        if (not isinstance(serial, str) or not serial or len(serial.encode()) > 100
                or not isinstance(row, dict) or set(row) != {'usb_port', 'uart', 'cases'}
                or not isinstance(row['usb_port'], str) or not re.fullmatch(r'[1-9]\d*-[1-9]\d*(?:\.[1-9]\d*)*', row['usb_port'])
                or not isinstance(row['uart'], str) or not re.fullmatch(r'/dev/ttyS\d{1,3}', row['uart'])
                or not isinstance(row['cases'], list) or not row['cases']
                or any(case not in CASES for case in row['cases'])
                or len(set(row['cases'])) != len(row['cases']) or row['usb_port'] in ports):
            raise ValueError('board_tests requires distinct USB ports, a board UART and supported case IDs')
        ports.add(row['usb_port'])
    return document


def select_transport(output, port):
    matches = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == 'device' and 'usb:' + port in fields[2:]:
            ids = [value.split(':')[1] for value in fields[2:] if re.fullmatch(r'transport_id:[1-9]\d*', value)]
            if len(ids) != 1:
                raise ConnectionError('USB 调试连接缺少唯一的 transport_id')
            matches.append(ids[0])
    if len(matches) != 1:
        raise ConnectionError('板卡 USB 调试连接未就绪或身份不唯一')
    return matches[0]


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

    def command(self, *args):
        return subprocess.run([self.adb, *args], capture_output=True, text=True, errors='replace', timeout=10,
                              env={**os.environ, 'ADB_SERVER_SOCKET': 'tcp:127.0.0.1:5037'})

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

    def observe(self, tag, before, baseline, evidence, row):
        timeout = 235 if self.case == 'rtc-wake' else 180
        if self.case not in CASES:
            timeout = self.custom['timeout'] + 25
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
            if self.manager.hardware.board_tests.get(self.serial) != self.row:
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
                    if self.manager.hardware.board_tests.get(self.serial) != self.row:
                        raise ValueError('板卡用例接线已变化')
                    endpoint, before = self.preflight(context, row)
                    row['boot_before'] = before['boot_id']
                    if expected_boot is not None and before['boot_id'] != expected_boot:
                        raise ValueError('轮次之间出现未经本任务确认的启动')
                    if self.case == 'reboot-aging':
                        baseline = verify_boot(before, baseline)
                    evidence = SerialEvidence(device)
                    tag = f'DECKLAB-CASE {context.id} {number}'
                    unit = f'decklab-case-{context.id}-{number}'
                    target_case, arguments = self.case, []
                    if self.case not in CASES:
                        target_case = 'shell'
                        arguments = [base64.b64encode(self.custom['script'].encode()).decode(), str(self.custom['timeout'])]
                    payload = 'LC_ALL=C ' + shlex.join(['systemd-run', '--no-block', '--collect', '--unit=' + unit,
                                                       '--property=RuntimeMaxSec=230s', '/usr/bin/python3', '-c', script,
                                                       target_case, self.row['uart'], tag, self.serial, before['boot_id'],
                                                       *arguments])
                    context.check()
                    context.issued()
                    row.update(trigger='unknown', service=unit)
                    result.update(in_flight=number, service=unit)
                    context.store.update(context.id, result=result)
                    in_flight = True
                    try:
                        submitted = self.command('-t', endpoint, 'shell', payload)
                        row['submit'] = {'exit_code': submitted.returncode, 'stdout': submitted.stdout[:2048],
                                         'stderr': submitted.stderr[:2048]}
                        if submitted.returncode == 0:
                            row['trigger'] = 'acknowledged'
                        elif rejected := START_REJECTED.search(submitted.stderr[:2048]):
                            row.update(trigger='not-issued', status='failed',
                                       reason='临时服务启动失败：' + rejected[0][:220])
                            in_flight = False
                            context.mark_outcome('not-issued')
                    except (OSError, subprocess.TimeoutExpired):
                        row['submit'] = {'error': '触发回执未收到，继续观察原轮次'}
                    if in_flight:
                        row['status'], row['reason'] = self.observe(tag, before, baseline, evidence, row)
                        in_flight = not row.get('round_finished', False)
                    if evidence.incomplete and row['status'] == 'passed':
                        row.update(status='uncertain', reason='原串口证据有缺口')
                except (ConnectionError, OSError, ValueError, subprocess.TimeoutExpired) as error:
                    row.update(status='uncertain', reason=str(error)[:256])
                except TaskCancelled:
                    break
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
                    first_issue = {key: value for key, value in row.items() if key not in ('preflight', 'submit', 'last_readback')}
                    result['first_issue'] = first_issue
                report.write((json.dumps(row, ensure_ascii=False) + '\n').encode())
                recent.append({key: value for key, value in row.items() if key not in ('preflight', 'submit', 'last_readback')})
                del recent[:-20]
                result.update(completed_rounds=completed, in_flight=None)
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
    row = manager.hardware.board_tests.get(device.serial, {})
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
    if not executable or not row or (not custom_case and case not in row.get('cases', [])):
        raise TaskError('用例尚未配置固定 USB 接线或控制机缺少 adb', 409)
    custom = None
    if custom_case:
        with manager.store.lock:
            saved = manager.store.db.execute('SELECT * FROM board_test_cases WHERE id=? AND revision=?', (case, revision)).fetchone()
        if saved is None:
            raise TaskError('用例已更新，重新选择后再运行', 409)
        custom = dict(saved)
    operation = BoardCase(manager, device, row, case, rounds, seconds, executable, custom=custom)
    return manager.tasks.start_board('board-test', actor, payload.get('client_id', ''), device.serial, operation.run,
        request_id=request_id, input_digest=digest, details={'attempt_id': request_id}, resources=['board-test-usb:' + row['usb_port']],
        maintenance=operation.maintenance, connection_id=device.connection_id, tracks_outcome=True)
