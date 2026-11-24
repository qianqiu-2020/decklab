"""Built-in, one-round-at-a-time board tests."""
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time

from .board_maintenance import maintenance_for
from .flash_store import FlashStore, validate_request
from .tasks import TaskCancelled, TaskError

CASES = ('reboot-aging', 'rtc-wake')
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
    def __init__(self, manager, device, row, case, rounds, seconds, executable):
        self.manager, self.device, self.serial = manager, device, device.serial
        self.row, self.case, self.rounds, self.seconds = row, case, rounds, seconds
        self.adb = executable
        self.maintenance = maintenance_for(manager)
        self.connection = device.connection_id
        self.board = device.board

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
        deadline = time.monotonic() + (235 if self.case == 'rtc-wake' else 180)
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
                    if result['status'] != 'passed' or (self.case == 'rtc-wake' and row['armed']):
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
        row['timeout_seconds'] = 235 if self.case == 'rtc-wake' else 180
        return 'uncertain', '观察超时，本轮证据不足'

    def run(self, context):
        recent, completed = [], 0
        result = {'case': self.case, 'completed_rounds': 0, 'recent_rounds': recent}
        first_issue = None
        report = None
        in_flight = False
        try:
            report = tempfile.TemporaryFile()
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
                    payload = 'LC_ALL=C ' + shlex.join(['systemd-run', '--no-block', '--collect', '--unit=' + unit,
                                                       '--property=RuntimeMaxSec=230s', '/usr/bin/python3', '-c', script,
                                                       self.case, self.row['uart'], tag, self.serial, before['boot_id']])
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
                if row['status'] == 'passed':
                    completed += 1
                    expected_boot = row.get('boot_after')
                    context.mark_outcome('observed')
                else:
                    first_issue = {key: value for key, value in row.items() if key not in ('preflight', 'submit', 'last_readback')}
                    if evidence and evidence.data:
                        raw = bytes(evidence.data)
                        source = io.BytesIO(raw)
                        source.seek(0, 2)
                        try:
                            first_issue['serial_artifact_id'] = self.artifact(context, f'{context.id}-round-{number}-serial.log', source)
                        except (OSError, TaskError) as error:
                            first_issue['serial_artifact_error'] = str(error)
                        first_issue['serial_lines'] = raw.count(b'\n') + int(not raw.endswith(b'\n'))
                        last = raw.splitlines()
                        first_issue['last_serial_line'] = last[-1].decode(errors='replace')[:300] if last else ''
                    result['first_issue'] = first_issue
                report.write((json.dumps(row, ensure_ascii=False) + '\n').encode())
                recent.append({key: value for key, value in row.items() if key not in ('preflight', 'submit', 'last_readback')})
                del recent[:-20]
                result.update(completed_rounds=completed, in_flight=None)
                context.store.update(context.id, result=result)
                if first_issue or context.cancelled.is_set():
                    break
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
            if in_flight:
                self.maintenance.keep_recovery(context.id)
                FlashStore(self.manager.store).update_details(context.id, {'recovery_required': True})
            else:
                self.maintenance.release(context.id, confirmed=True)


def start(manager, device, payload, actor):
    if actor != 'web':
        raise TaskError('板卡用例仅限主访问身份', 403)
    if set(payload) - {'kind', 'key', 'serial', 'actor', 'client_id', 'connection_id', 'request_id',
                       'case', 'rounds', 'seconds'}:
        raise TaskError('用例仅接受固定用例和轮数或时长')
    case, rounds, seconds = payload.get('case'), payload.get('rounds'), payload.get('seconds')
    if ((rounds is None) == (seconds is None) or
            (rounds is not None and (type(rounds) is not int or not 1 <= rounds <= 10000)) or
            (seconds is not None and (type(seconds) is not int or not 1 <= seconds <= 7 * 86400))):
        raise TaskError('请填写轮数或时长')
    row = manager.hardware.board_tests.get(device.serial, {})
    executable = shutil.which('adb')
    if not executable or case not in row.get('cases', []):
        raise TaskError('用例尚未配置固定 USB 接线或控制机缺少 adb', 409)
    request_id = payload.get('request_id')
    digest = manager.tasks.store.request_digest({'serial': device.serial, 'case': case, 'rounds': rounds, 'seconds': seconds})
    validate_request(request_id, digest)
    operation = BoardCase(manager, device, row, case, rounds, seconds, executable)
    return manager.tasks.start_board('board-test', actor, payload.get('client_id', ''), device.serial, operation.run,
        request_id=request_id, input_digest=digest, details={'attempt_id': request_id}, resources=['board-test-usb:' + row['usb_port']],
        maintenance=operation.maintenance, connection_id=device.connection_id, tracks_outcome=True)
