"""Commands used by a single transient test service on the target."""
import base64
import json
import os
from pathlib import Path
import re
from selectors import DefaultSelector, EVENT_READ
import shlex
import signal
import subprocess
import sys
import time

BOOT = '/proc/sys/kernel/random/boot_id'
SERIAL = '/sys/class/dmi/id/product_serial'
RTC = '/sys/class/rtc/rtc0'


def rtc_evidence(step):
    _, name = step(['cat', RTC + '/name'])
    if 'rpmi' not in name.lower():
        def pmic_irq():
            code, interrupts = step(['cat', '/proc/interrupts'])
            rows = [line for line in interrupts.splitlines() if ',' not in line and line.rstrip().endswith(' rtc@pmic')]
            if code or len(rows) != 1:
                return None
            match = re.match(r'^\s*\d+:\s+((?:\d+\s+)+)', rows[0])
            return sum(map(int, match[1].split())) if match else None
        count = pmic_irq()
        if count is not None:
            return 'RTC 专用中断', pmic_irq, count

    # The RTC and other devices may share an IRQ, so count events on rtc0's
    # wakeup source. Never treat another action on a shared IRQ as RTC proof.
    counters = list(Path(RTC).glob('device/wakeup/wakeup*/event_count'))
    if len(counters) != 1:
        return None

    def wake_events():
        code, count = step(['cat', str(counters[0])])
        return int(count.strip()) if not code and re.fullmatch(r'[0-9]+', count.strip()) else None

    count = wake_events()
    return ('RTC 设备唤醒事件', wake_events, count) if count is not None else None


def run_command(argv, timeout, emit, *, capture_stdout=True, merge_output=False):
    stdout = bytearray()
    deadline = time.monotonic() + timeout
    timed_out = False
    with subprocess.Popen(argv, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT if merge_output else subprocess.PIPE,
                          start_new_session=not capture_stdout) as process, DefaultSelector() as pipes:
        def stop():
            if capture_stdout:
                process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for name in ('stdout',) if merge_output else ('stdout', 'stderr'):
            pipes.register(getattr(process, name), EVENT_READ, name)
        while pipes.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                stop()
                break
            for key, _ in pipes.select(remaining):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    pipes.unregister(key.fileobj)
                    continue
                if capture_stdout and key.data == 'stdout':
                    stdout.extend(chunk)
                emit(key.data, chunk)
        try:
            code = process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            stop()
            process.wait()
            timed_out, code = True, 124
    return (124 if timed_out else code), stdout.decode(errors='replace')


def identity():
    serial = Path(SERIAL)
    value = serial.read_text().strip() if serial.exists() else ''
    if value.lower() in ('', 'to be filled by o.e.m.', 'unknown', 'none'):
        value = Path('/proc/device-tree/serial-number').read_bytes().strip(b'\0\r\n ').decode()
    return {'serial': value, 'boot_id': Path(BOOT).read_text().strip()}


def main(case, uart, tag, serial, boot_id, encoded_script='', shell_timeout='60'):
    def write_uart(raw):
        with open(uart, 'wb') as output:
            output.write(raw)

    def emit(text, raw=b''):
        write_uart(('\n' + text + '\n').encode('utf-8', 'replace') + raw)

    def step(argv, timeout=12, capture=True):
        emit(f'{tag} CMD {shlex.join(argv)}')
        try:
            options = {} if capture else {'capture_stdout': False}
            code, stdout = run_command(argv, timeout, lambda channel, raw: emit(f'{tag} {channel}', raw), **options)
        except OSError as error:
            code, stdout = 127, ''
            emit(f'{tag} stderr\n{error}')
        emit(f'{tag} EXIT {code}')
        return code, stdout

    def done(status, reason):
        emit(tag + ' RESULT ' + json.dumps(dict(status=status, reason=reason, **identity())))

    emit(tag + ' 开始')
    code, _ = step(['cat', SERIAL if Path(SERIAL).exists() else '/proc/device-tree/serial-number', BOOT])
    if code or identity() != {'serial': serial, 'boot_id': boot_id}:
        done('uncertain', '板卡身份或启动编号变化')
        return
    emit(f'{tag} BOARD {serial} BOOT {boot_id}')
    if case == 'reboot-aging':
        emit(tag + ' ISSUED')
        code, _ = step(['systemctl', '--no-block', 'reboot'])
        if code:
            done('failed', '软件重启命令失败')
        return
    if case == 'shell':
        script = base64.b64decode(encoded_script, validate=True).decode()
        emit(tag + ' ISSUED')
        status, _ = step(['/bin/sh', '-c', script], timeout=int(shell_timeout), capture=False)
        done('passed' if status == 0 else 'failed', f'Shell 脚本退出码 {status}')
        return
    if case == 'shell-step':
        command = base64.b64decode(encoded_script, validate=True).decode()
        argv = ['/bin/sh', '-c', command]
        emit(f'{tag} CMD {shlex.join(argv)}')
        emit(tag + ' ISSUED')
        emit(tag + ' OUTPUT')
        try:
            status, _ = run_command(argv, int(shell_timeout), lambda _channel, raw: write_uart(raw),
                                    capture_stdout=False, merge_output=True)
        except OSError as error:
            status = 127
            emit(f'{tag} stderr\n{error}')
        emit(tag + ' OUTPUT-END')
        emit(f'{tag} EXIT {status}')
        done('passed' if status == 0 else 'failed', f'Shell 命令退出码 {status}')
        return

    modes = step(['cat', '/sys/power/mem_sleep'])[1]
    pm_test = step(['cat', '/sys/power/pm_test'])[1]
    console = step(['cat', '/sys/module/printk/parameters/console_suspend'])[1]
    alarm_status, alarm = step(['cat', RTC + '/wakealarm'])
    evidence = rtc_evidence(step)
    code, journal = step(['journalctl', '-k', '-b', '-n', '1', '--show-cursor', '-o', 'cat', '--no-pager'])
    cursor = re.search(r'^-- cursor: ([0-9a-zA-Z;=]+)$', journal, re.M)
    if (code or not cursor or '[deep]' not in modes or '[none]' not in pm_test
            or console.strip() != 'Y' or alarm_status or alarm.strip() not in ('', '0')):
        done('uncertain', '深度挂起、RTC 或内核日志预检未通过')
        return
    if not evidence:
        done('uncertain', '没有找到 RTC 专用中断或设备级唤醒计数')
        return
    counter_name, rtc_count, before = evidence

    if step(['rtcwake', '-m', 'no', '-s', '35', '-d', 'rtc0'])[0] != 0:
        done('failed', '设置 RTC 闹钟失败')
        return
    emit(tag + ' ARMED')
    status, _ = step(['systemctl', 'suspend'], timeout=200)
    if status:
        done('failed', 'systemctl suspend 未成功退出')
        return
    # systemctl may return before entering suspend. Allow the journal to catch
    # up after wake without charging the time spent suspended to this window.
    deadline = time.monotonic() + 25
    while True:
        code, trace = step(['journalctl', '-k', '-b', '--after-cursor=' + cursor[1], '-o', 'cat', '--no-pager'])
        after = rtc_count()
        _, result = step(['systemctl', 'show', 'systemd-suspend.service', '-p', 'Result', '-p', 'ExecMainStatus'])
        alarm_status, alarm = step(['cat', RTC + '/wakealarm'])
        if identity()['boot_id'] != boot_id:
            break
        if 'Failed to suspend' in trace or re.search(r'^Result=(exit-code|signal|timeout)$', result, re.M):
            done('failed', '内核或挂起服务报告失败')
            return
        if (not code and trace.count('PM: suspend entry (deep)') == 1 and trace.count('PM: suspend exit') == 1
                and after is not None and after > before and 'Result=success' in result
                and 'ExecMainStatus=0' in result and not alarm_status and alarm.strip() in ('', '0')):
            done('passed', f'同一次启动恢复，{counter_name} {before} → {after}')
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    done('uncertain', 'RTC 唤醒或同一次启动的证据不足')


if __name__ == '__main__':
    main(*sys.argv[1:])
