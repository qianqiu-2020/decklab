"""Fixed commands used by a single transient test service on the target."""
import json
import os
from pathlib import Path
import re
from selectors import DefaultSelector, EVENT_READ
import shlex
import subprocess
import sys
import time

BOOT = '/proc/sys/kernel/random/boot_id'
SERIAL = '/sys/class/dmi/id/product_serial'


def run_command(argv, timeout, emit):
    stdout = bytearray()
    deadline = time.monotonic() + timeout
    timed_out = False
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process, DefaultSelector() as pipes:
        for name in ('stdout', 'stderr'):
            pipes.register(getattr(process, name), EVENT_READ, name)
        while pipes.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                process.kill()
                break
            for key, _ in pipes.select(remaining):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    pipes.unregister(key.fileobj)
                    continue
                if key.data == 'stdout':
                    stdout.extend(chunk)
                emit(key.data, chunk)
        try:
            code = process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            timed_out, code = True, 124
    return (124 if timed_out else code), stdout.decode(errors='replace')


def identity():
    serial = Path(SERIAL)
    value = serial.read_text().strip() if serial.exists() else ''
    if value.lower() in ('', 'to be filled by o.e.m.', 'unknown', 'none'):
        value = Path('/proc/device-tree/serial-number').read_bytes().strip(b'\0\r\n ').decode()
    return {'serial': value, 'boot_id': Path(BOOT).read_text().strip()}


def main(case, uart, tag, serial, boot_id):
    def emit(text, raw=b''):
        with open(uart, 'wb') as output:
            output.write(('\n' + text + '\n').encode('utf-8', 'replace') + raw)

    def step(argv, timeout=12):
        emit(f'{tag} CMD {shlex.join(argv)}')
        try:
            code, stdout = run_command(argv, timeout, lambda channel, raw: emit(f'{tag} {channel}', raw))
        except OSError as error:
            code, stdout = 127, ''
            emit(f'{tag} stderr\n{error}')
        emit(f'{tag} EXIT {code}')
        return code, stdout

    def done(status, reason):
        emit(tag + ' RESULT ' + json.dumps(dict(status=status, reason=reason, **identity())))

    def rtc_count():
        _, interrupts = step(['cat', '/proc/interrupts'])
        rows = [line.split()[1:] for line in interrupts.splitlines() if line.split() and line.split()[-1] == 'rtc@pmic']
        if len(rows) != 1:
            return None
        counters = re.match(r'(?:\d+ )+', ' '.join(rows[0]) + ' ')
        return sum(map(int, counters[0].split())) if counters else None

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

    modes = step(['cat', '/sys/power/mem_sleep'])[1]
    pm_test = step(['cat', '/sys/power/pm_test'])[1]
    console = step(['cat', '/sys/module/printk/parameters/console_suspend'])[1]
    alarm = step(['cat', '/sys/class/rtc/rtc0/wakealarm'])[1]
    before = rtc_count()
    code, journal = step(['journalctl', '-k', '-b', '-n', '1', '--show-cursor', '-o', 'cat', '--no-pager'])
    cursor = re.search(r'^-- cursor: ([0-9a-zA-Z;=]+)$', journal, re.M)
    if (code or not cursor or '[deep]' not in modes or '[none]' not in pm_test
            or console.strip() != 'Y' or alarm.strip() not in ('', '0') or before is None):
        done('uncertain', '深度挂起、RTC 或内核日志预检未通过')
        return

    if step(['rtcwake', '-m', 'no', '-s', '35'])[0] != 0:
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
        alarm = step(['cat', '/sys/class/rtc/rtc0/wakealarm'])[1]
        if identity()['boot_id'] != boot_id:
            break
        if 'Failed to suspend' in trace or re.search(r'^Result=(exit-code|signal|timeout)$', result, re.M):
            done('failed', '内核或挂起服务报告失败')
            return
        if (not code and trace.count('PM: suspend entry (deep)') == 1 and trace.count('PM: suspend exit') == 1
                and after is not None and after > before and 'Result=success' in result
                and 'ExecMainStatus=0' in result and alarm.strip() in ('', '0')):
            done('passed', f'同一次启动恢复，RTC 中断 {before} → {after}')
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    done('uncertain', 'RTC 唤醒或同一次启动的证据不足')


if __name__ == '__main__':
    main(*sys.argv[1:])
