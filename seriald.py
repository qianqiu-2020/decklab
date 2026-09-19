#!/usr/bin/env python3
"""seriald —— 串口设备管理服务。

解决什么: /dev/ttyUSB* 的编号在 USB 重新枚举后会变,插拔顺序一乱就认不出
哪根线接哪块板子。本服务用 **USB 物理拓扑**(/dev/serial/by-path/)做稳定锚点 ——
它只跟"插在哪个 USB 口"有关,与设备名无关;再叠一层用户可编辑的板子编号,
于是无论 ttyUSB 号怎么变,都能按编号找到板子。

每个串口: 自动开一个采集器 → 落盘日志 → 嗅探板子 hostname → 页面可视可写。

三条铁律(都是实际踩坑总结,改动前务必先读):
  1. **一个口只能有一个读者**。tty 允许多进程 open,但输入队列是共享的,
     两个读者会把字节流瓜分掉(实测丢 30%)。所以每设备只跑一个采集线程,
     页面取数据一律从它的日志/缓冲扇出,绝不另开 fd 去读。
  2. **写与读是两条独立通路**。写只在发送时临时开 O_WRONLY fd,不参与读取,
     因此与采集器零竞争。
  3. **绝不 tcsetattr/stty**。termios 是设备级共享的,任何一方改了整台设备都变,
     包括正在读的采集线程。写侧连 termios 都不读。

用法: seriald.py [端口=8898] [绑定地址=10.0.90.177]
"""
import base64
import codecs
import errno
import fcntl
import functools
import http.server
import hashlib
import json
import os
import pty
import re
import secrets
import select
import socketserver
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
from urllib.parse import urlparse, parse_qs

BASE = os.environ.get('SERIALD_BASE', '/data/seriald')      # 运行数据:日志、编号映射
# 静态根(页面/前端库)与数据根分开 —— 打成 deb 后代码在 /usr/lib/seriald,
# 数据仍在 /data/seriald。默认取脚本自身所在目录,所以直接跑源码时行为不变。
STATIC = os.environ.get('SERIALD_STATIC') or os.path.dirname(os.path.abspath(__file__))
LOG_ROOT = os.path.join(BASE, 'logs')
LABELS = os.path.join(BASE, 'labels.json')
BY_PATH = '/dev/serial/by-path'
# 自愈脚本的位置。必须由 root 拥有且调用者不可写,否则 sudoers 白名单等于通用提权。
# 打包后由 dpkg 装到 /usr/libexec/seriald/rescue.sh(天然满足这个前提)。
RESCUE_BIN = os.environ.get('SERIALD_RESCUE_BIN', '/usr/local/sbin/seriald-rescue.sh')

BAUD = 115200
SCROLLBACK = 256 * 1024          # 每设备内存缓冲上限(页面实时视图用)
SSH_LIMIT = 8                   # 防止遗留标签页无限创建 ssh 子进程
SSH_IDLE = 30 * 60
DATE_FMT = '%Y%m%d'

# 从控制台流里嗅探 hostname 的几种常见形态
HOST_PATTERNS = [
    re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]{1,62}) login:\s*$'),      # getty 提示符
    re.compile(r'\broot@([A-Za-z0-9][A-Za-z0-9._-]{1,62}):'),          # shell 提示符
    re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]{1,62}) kernel:'),         # 内核日志转发
    re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]{1,62}) systemd\['),       # systemd 日志转发
    re.compile(r'^Welcome to .*?\(GNU/Linux .*?\)'),                   # 只用于识别"已启动"
]
ANSI = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)'
                  r'|\x1b[=>()][A-Za-z0-9]?|\x1bP.*?\x1b\\')

_lock = threading.Lock()


def safe_name(s, fallback='unnamed'):
    """把任意字符串收成安全的目录名。"""
    s = re.sub(r'[^A-Za-z0-9._-]+', '-', (s or '').strip()).strip('-')
    return s[:48] if s and s not in ('.', '..') else fallback


def load_labels():
    try:
        with open(LABELS, encoding='utf-8') as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_labels(d):
    tmp = LABELS + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, LABELS)


def open_serial(dev, flush=False, identity=None):
    """115200 8N1,无流控,raw,不回显。只有采集器调这个(唯一读者)。

    默认保留内核里已经排队的字节;重连时 flush 会截断板子的启动现场。
    """
    fd = os.open(dev, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        if identity is not None and (s.st_dev, s.st_ino, s.st_rdev) != identity:
            raise OSError(errno.ENODEV, '串口已重新枚举')
        a = termios.tcgetattr(fd)
        a[0] = 0
        a[1] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[3] = 0
        a[4] = termios.B115200
        a[5] = termios.B115200
        a[6][termios.VMIN] = 0
        a[6][termios.VTIME] = 0
        # Manager 在旧采集器彻底退出后才允许进入这里;运行中的读者绝不重新配置。
        termios.tcsetattr(fd, termios.TCSANOW, a)
        if flush:
            termios.tcflush(fd, termios.TCIFLUSH)
        return fd
    except BaseException:
        os.close(fd)                     # tcgetattr/tcsetattr 失败也必须归还 fd
        raise


def write_all(fd, data, timeout=5.0, cancel=None):
    """非阻塞 fd 也会短写;串行补齐且有截止时间,不持有输出缓冲锁。"""
    pending = memoryview(data)
    deadline = time.monotonic() + timeout
    while pending:
        if cancel is not None and cancel.is_set():
            raise OSError(errno.ECANCELED, '会话已停止')
        left = deadline - time.monotonic()
        if left <= 0:
            raise OSError(errno.ETIMEDOUT, '写入超时,可能已发送部分内容')
        try:
            n = os.write(fd, pending)
            if n <= 0:
                raise OSError(errno.EIO, '写入未取得进展')
            pending = pending[n:]
        except InterruptedError:
            continue
        except BlockingIOError:
            try:
                select.select([], [fd], [], min(left, 0.1))
            except InterruptedError:
                continue


def write_serial(dev, data, identity=None, cancel=None):
    """铁律 2/3:只写不读、不碰 termios。"""
    try:
        fd = os.open(dev, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as e:
        return False, str(e)
    try:
        if identity is not None:
            s = os.fstat(fd)
            if (s.st_dev, s.st_ino, s.st_rdev) != identity:
                raise OSError(errno.ENODEV, '串口已重新枚举,拒绝写入')
        write_all(fd, data.encode('utf-8', 'replace'), cancel=cancel)
        return True, ''
    except OSError as e:
        return False, str(e)
    finally:
        os.close(fd)


# 每次命令由 _run_cmd 加独立 nonce 帧;命令回显不含完整 marker,迟到结果不会串单。
SSH_STATE_CMD = (
    "if ! command -v systemctl >/dev/null 2>&1; then echo SSHD=unsupported-systemd; "
    "elif systemctl is-active --quiet ssh; then echo SSHD=active; "
    "elif systemctl is-active --quiet sshd; then echo SSHD=active; "
    "else echo SSHD=inactive; fi"
)
PROBE_CMD = (
    "printf 'HOST=%s\\n' \"$(hostname 2>/dev/null | tr '\\r\\n' '  ')\"; "
    "printf 'SERIAL=%s\\n' \"$(cat /proc/device-tree/serial-number 2>/dev/null | tr -d '\\0' | tr '\\r\\n' '  ')\"; "
    "printf 'MODEL=%s\\n' \"$(cat /proc/device-tree/model 2>/dev/null | tr -d '\\0' | tr '\\r\\n' '  ')\"; "
    "ip -4 -o addr show 2>/dev/null | awk '{print \"IP=\"$2\" \"$4}'; "
    + SSH_STATE_CMD
)
SSH_CMD = (
    "if [ \"$(id -u)\" != 0 ]; then echo SSHD=requires-root; "
    "elif ! command -v systemctl >/dev/null 2>&1; then echo SSHD=unsupported-systemd; "
    "elif ! command -v sshd >/dev/null 2>&1 && ! command -v apt-get >/dev/null 2>&1; "
    "then echo SSHD=unsupported-package-manager; else "
    "if ! command -v sshd >/dev/null 2>&1; then "
    "apt-get update -qq >/dev/null 2>&1 && DEBIAN_FRONTEND=noninteractive "
    "apt-get install -y -qq openssh-server >/dev/null 2>&1; fi; "
    "if ! command -v sshd >/dev/null 2>&1; then echo SSHD=install-failed; else "
    "systemctl enable --now ssh >/dev/null 2>&1 || systemctl enable --now sshd >/dev/null 2>&1; "
    + SSH_STATE_CMD + "; fi; fi"
)

# 登录提示符 / 密码提示符 —— **必须中英双认**。
# 实测:k1 跑 Bianbu 2.3.5 且是中文 locale,getty 显示的是 `密码：`(全角冒号)、
# `用户名`、`登录错误`。只认 `Password:` 会永远等不到,自动登录静默失败。
RE_LOGIN = r'(?:login|用户名)[:：]?'
RE_PASSWD = r'(?:[Pp]assword|密码)[:：]'
RE_PROMPT = r'[#$]\s*$'


def usb_path_of(dev):
    """从 /dev/ttyUSBx 反查它挂在哪个 USB 设备上(如 3-4.2.2)。

    注意要取**最深**那一层:路径里有集线器各级(usb3/3-4/3-4.2/3-4.2.2/3-4.2.2:1.0),
    匹配到第一个会得到集线器 3-4 —— 对它做 unbind 等于拔掉整个集线器。
    """
    try:
        p = os.path.realpath('/sys/class/tty/%s/device' % os.path.basename(dev))
    except OSError:
        return None
    hits = re.findall(r'/(\d+-[\d.]+)(?::[\d.]+)?/', p + '/')
    return hits[-1] if hits else None


def try_rescue(dev, mode='soft'):
    """尝试自愈一个打不开的串口。

    分两步(见 rescue.sh 注释):soft 只关 autosuspend;rebind 追加软拔插。
    这一步能顺带区分病因 —— 只关 autosuspend 就复活,说明是"挂起唤醒失败";
    还要软拔插才行,说明是更底层的固件级死锁。

    执行路径:
      * seriald 以 root 跑  -> 直接写 /sys(最简,无需任何配置)
      * 否则               -> sudo -n 调白名单脚本(需用户一次性配置 sudoers);
                              -n 保证不会卡在密码提示,sudoers 没配就直接失败并如实上报
    白名单脚本必须由 root 拥有且不可被调用者改写,否则等于通用提权。
    """
    name = usb_path_of(dev)
    if not name:
        return False, '找不到 %s 对应的 USB 设备路径' % dev
    if mode not in ('soft', 'rebind') or not re.fullmatch(r'[0-9]+-[0-9]+(?:\.[0-9]+)*', name):
        return False, 'USB 路径或自愈模式不合法'
    steps = []
    failed = False

    if os.geteuid() != 0:
        if not os.path.exists(RESCUE_BIN):
            return False, ('自愈脚本未安装(%s)。装好后重试,或用页面提示里的一次性命令'
                           % RESCUE_BIN)
        try:
            p = subprocess.run(['sudo', '-n', RESCUE_BIN, name, mode],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            return False, '调用自愈脚本失败: %s' % e
        out = (p.stdout + ' ' + p.stderr).strip().replace('\n', '; ')
        ok = p.returncode == 0
        if not ok and 'password' in out.lower():
            out = 'sudo 需要密码(白名单未配置): ' + out
        return ok, '%s [%s]' % (out or '无输出', name)

    # root 直连:不依赖脚本
    ctl = '/sys/bus/usb/devices/%s/power/control' % name
    try:
        with open(ctl, 'w') as f:
            f.write('on')
        steps.append('autosuspend 已关')
    except OSError as e:
        failed = True
        steps.append('关 autosuspend 失败: %s' % (e.strerror or e))
    if mode == 'rebind':
        for act in ('unbind', 'bind'):
            try:
                with open('/sys/bus/usb/drivers/usb/%s' % act, 'w') as f:
                    f.write(name)
                steps.append(act)
            except OSError as e:
                failed = True
                steps.append('%s 失败: %s' % (act, e.strerror or e))
    # 一步成功不代表整次 rebind 成功;与特权脚本的退出码语义一致。
    ok = not failed
    return ok, '%s [%s]' % ('; '.join(steps), name)


def parse_probe(raw):
    """解析已校验的 nonce 帧正文;字段缺失或重复必须报错,不能把残帧当成功。"""
    out = {'hostname': '', 'serial': '', 'model': '', 'ips': [], 'sshd': ''}
    # 调用者已经验证完整 nonce 帧;字段值里的 marker 样文字不能再截断正文。
    body = raw.replace('\r\n', '\n')
    for key in ('HOST', 'SERIAL', 'MODEL', 'SSHD'):
        if len(re.findall(r'^' + key + r'=', body, re.M)) != 1:
            raise ValueError('识别结果缺少或重复字段: ' + key)

    def last(key, pat):
        vals = []
        for m in re.finditer(pat, body, re.M):
            v = m.group(1).strip()
            if v and '$(' not in v and '2>/dev/null' not in v:
                vals.append(v)
        return vals[-1] if vals else ''

    out['hostname'] = last('HOST', r'^HOST=(.*)$')
    out['serial'] = last('SERIAL', r'^SERIAL=(.*)$')
    out['model'] = last('MODEL', r'^MODEL=(.*)$')
    out['sshd'] = last('SSHD', r'^SSHD=(.*)$')
    seen = set()
    for m in re.finditer(r'^IP=(\S+)\s+(\d+\.\d+\.\d+\.\d+/\d+)', body, re.M):
        iface, cidr = m.group(1), m.group(2)
        if cidr.startswith('127.') or cidr in seen:
            continue
        seen.add(cidr)
        out['ips'].append('%s %s' % (iface, cidr))
    out['raw'] = body[-1200:]
    return out


class SshSession:
    """一条 ssh 到板子的会话(pty)。

    和串口那条链路并存但互不干扰:串口走"单读者采集",这里走独立 pty,
    各读各的。输出按偏移给多读者取增量,同时落盘成可读日志 ——
    维护调试板子的核心诉求就是"操作过程要有记录"。
    """

    def __init__(self, sid, ip, label, logdir):
        self.sid = sid
        self.ip = ip
        self.label = label
        self.buf = bytearray()
        self.base = 0
        self.cv = threading.Condition()
        self.write_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._stop = threading.Event()
        self._disposed = threading.Event()
        self._thread = None
        self._started_once = False
        self.closed = False
        self.fd = None
        self.proc = None
        self.started = time.time()
        self.last_touch = time.monotonic()
        self.cols = 120                   # 默认尺寸;浏览器连上后会按实际宽度覆盖
        self.rows = 32
        self.logf = None
        self.logpath = None
        self._logdir = logdir
        self._logday = time.strftime(DATE_FMT)
        self._log_decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self._log_pending = ''
        self._log_finished = False
        self.argv = ['sshpass', '-p', 'bianbu', 'ssh', '-tt',
                     '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10',
                     '-o', 'ServerAliveInterval=30', 'root@' + ip]
        self._open_log(logdir)

    def _open_log(self, logdir):
        try:
            day = time.strftime(DATE_FMT)
            path = os.path.join(logdir, 'ssh-%s.log' % day)
            self.logf = open(path, 'a', encoding='utf-8', buffering=1)
            self.logpath = path
            self._logday = day
        except OSError:
            self.logf = None

    def _log(self, data, final=False):
        with self._log_lock:
            if self.logf and not self._log_finished and self._logday != time.strftime(DATE_FMT):
                # SSH 会话可能跨天,不能一直把后续输出写进连接当天的文件。
                try:
                    self.logf.close()
                except OSError:
                    pass
                self.logf = None
                self._open_log(self._logdir)
            if not self.logf or self._log_finished:
                return
            # read() 可以把一个汉字或一行切开,逐块 decode 会写出乱码和伪换行。
            self._log_pending += self._log_decoder.decode(data, final=final)
            self._log_finished = final
            lines = self._log_pending.split('\n')
            self._log_pending = lines.pop()
            if final or len(self._log_pending) > 16384:
                lines.append(self._log_pending)
                self._log_pending = ''
            stamp = time.strftime('%Y-%m-%d %H:%M:%S')
            try:
                for raw in lines:
                    line = ANSI.sub('', raw.rstrip('\r').split('\r')[-1]).rstrip()
                    if line:
                        self.logf.write('%s  %s\n' % (stamp, line))
            except OSError:
                pass

    def _close_log(self):
        self._log(b'', final=True)
        with self._log_lock:
            if self.logf:
                try:
                    self.logf.close()
                except OSError:
                    pass
                self.logf = None

    def ensure(self):
        with self._lifecycle_lock:
            with self.cv:
                # 旧 sid 曾在写入时偷偷重建连接;结束的会话只能由显式新建替代。
                if self.closed or self._started_once:
                    return self.fd is not None and not self.closed
                self._started_once = True
            master = slave = proc = None
            try:
                master, slave = pty.openpty()
                os.set_blocking(master, False)
                self._set_winsize(slave)  # SSH 初次请求远端 pty 时就使用浏览器尺寸。
                proc = subprocess.Popen(
                    self.argv, stdin=slave, stdout=slave, stderr=slave,
                    # 多线程进程里 preexec_fn 可能继承已锁住的锁,导致子进程死锁。
                    start_new_session=True, env=dict(os.environ, TERM='xterm-256color'),
                    close_fds=True)
                with self.cv:
                    self.fd, self.proc = master, proc
                    self.last_touch = time.monotonic()
                self._append('\r\n*** 已建立 SSH 会话 root@%s (%dx%d) ***\r\n'
                             % (self.ip, self.cols, self.rows))
                self._thread = threading.Thread(target=self._reader, args=(master, proc),
                                                daemon=True)
                self._thread.start()
            except (OSError, RuntimeError) as e:
                self._append('\r\n*** 建立 SSH 会话失败: %s ***\r\n' % e)
                self._thread = None
                self._finish(master, proc)
                return False
            finally:
                if slave is not None:
                    os.close(slave)
            return True

    def _set_winsize(self, fd):
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ,
                        struct.pack('HHHH', self.rows, self.cols, 0, 0))
        except OSError:
            pass

    def resize(self, cols, rows):
        """把浏览器终端尺寸同步到 pty。

        不同步的话远端以为自己是 80x24,宽度用不满,vi/htop 也会按错误列数画。
        SSH 收到 window-change 会转发给远端。
        """
        if cols < 20 or rows < 5 or cols > 500 or rows > 200:
            return False
        with self.write_lock:
            with self.cv:
                if self.closed or self.fd is None:
                    return False
                self.cols, self.rows = cols, rows
                self.last_touch = time.monotonic()
                fd = self.fd
            self._set_winsize(fd)
        return True

    def _reader(self, fd, proc):
        try:
            while not self._stop.is_set():
                try:
                    ready, _, _ = select.select([fd], [], [], 0.25)
                    if not ready:
                        continue
                    data = os.read(fd, 65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    break
                if not data:
                    break
                self._append(data)
        finally:
            try:
                self._append('\r\n*** SSH 会话已结束 ***\r\n')
            finally:
                self._finish(fd, proc)

    def _finish(self, fd, proc):
        self._stop.set()
        # fd 只由读线程回收;和写入互斥,否则 fd 号码复用后可能写进别的会话。
        with self.write_lock:
            with self.cv:
                self.closed = True
                self.fd = None
                self.cv.notify_all()
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if proc is not None:
            # sshpass/ssh 属于同一进程组;只发 TERM 而不 wait 曾遗留子进程和僵尸。
            try:
                os.killpg(proc.pid, 15)
            except OSError:
                pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, 9)
                except OSError:
                    pass
                try:
                    proc.kill()
                except OSError:
                    pass
                proc.wait()
            else:
                # 组长可能已退出而 ssh 仍存活;回收整组,不只清理直接子进程。
                try:
                    os.killpg(proc.pid, 9)
                except OSError:
                    pass
        self._close_log()
        self._disposed.set()

    def _append(self, data):
        b = data if isinstance(data, bytes) else data.encode('utf-8', 'replace')
        self._log(b)
        with self.cv:
            self.buf += b
            if len(self.buf) > SCROLLBACK:
                drop = len(self.buf) - SCROLLBACK
                del self.buf[:drop]
                self.base += drop
            self.cv.notify_all()

    def read(self, off, wait=15.0):
        if off <= 0:
            wait = 0
        deadline = time.monotonic() + wait
        with self.cv:
            self.last_touch = time.monotonic()
            while (not self.closed and self.fd is not None and off >= self.base + len(self.buf)
                   and time.monotonic() < deadline):
                self.cv.wait(1.0)
            start = max(off - self.base, 0) if off > self.base else 0
            return (self.base + len(self.buf), bytes(self.buf[start:]),
                    self.fd is not None and not self.closed)

    def write(self, text):
        # 慢远端会使写入等待可写;不能持有输出 cv,否则读者也一起停住。
        with self.write_lock:
            with self.cv:
                if self.closed or self.fd is None:
                    return False
                self.last_touch = time.monotonic()
                fd = self.fd
            try:
                write_all(fd, text.encode('utf-8', 'replace'), cancel=self._stop)
            except OSError:
                return False
            return True

    def close(self):
        with self._lifecycle_lock:
            if self._disposed.is_set():
                return
            with self.cv:
                self.closed = True
                self.cv.notify_all()
            self._stop.set()
            self.append_marker('*** 会话已关闭 ***')
            if self._thread is not None:
                # select 最多等待 250ms;写入由 stop 中断,不能从这里抢关读者的 fd。
                self._thread.join()
            else:
                self._finish(self.fd, self.proc)

    def is_idle(self, now, ttl):
        with self.cv:
            return self.closed or now - self.last_touch >= ttl

    def append_marker(self, text):
        self._append('\r\n%s\r\n' % text)

    def snapshot(self):
        with self.cv:
            return {'sid': self.sid, 'kind': 'ssh', 'ip': self.ip,
                    'alive': self.fd is not None and not self.closed,
                    'started': self.started, 'bytes': len(self.buf)}


class Device:
    """一个串口设备(按 by-path 稳定标识)及其采集状态。"""
    def __init__(self, key, dev):
        self.key = key                    # by-path 名,稳定锚点
        self.dev = dev                    # 当前 /dev/ttyUSBx(会变)
        self.hostname = ''
        self.cv = threading.Condition()
        self.buf = bytearray()
        self.base = 0
        self.fd = None
        self.bytes = 0
        self.last_rx = 0.0
        self.started = time.time()
        self.probed = 0                   # 主动探测次数
        self.last_probe = 0.0
        self.fail_count = 0               # 连续打开失败次数(用于退避)
        self.fail_logged = 0.0
        self.last_rescue = 0.0            # 上次尝试自愈的时间
        self.rescue_note = ''             # 自愈结果(页面显示用)
        self.probing = False
        self.operation_lock = threading.Lock()  # 整个控制台事务互斥,包含手动输入/探测/自愈
        self.write_lock = threading.Lock()      # fd 退役与写入串行,绝不占用输出 cv
        self.sessions_lock = threading.RLock()
        self.log_lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.identity = None
        self.present = False
        self.console_tail = ''
        self.newline = None            # 收到新提示符后才确认本连接的 CR/LF 策略
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.log_pending = ''
        self.probe_info = {}              # 最近一次识别结果
        self.ssh_sessions = {}            # sid -> SshSession(与串口并存的独立会话)
        self.ssh_seq = 0
        self.ssh_pending = 0
        self.logf = None
        # preload 可能在没有日志时早退;所有状态必须先初始化。
        self.logpath = None
        self.dead = False
        self.preload()

    def preload(self):
        """启动时把最近一份串口日志的尾部装回内存缓冲。

        历史只供页面回看;带时间戳和结束标记的旧日志不能代表现场登录状态。
        识别时先从新收到的 console_tail 确认,必要时请求板子重画提示符。
        """
        try:
            d = self.stable_dir()
            files = sorted(f for f in os.listdir(d)
                           if f.startswith('serial-') and f.endswith('.log'))
            if not files:
                # 旧版本目录只作历史读取,新日志不再写进可能碰撞的旧目录。
                d = self.legacy_dir()
                files = sorted(f for f in os.listdir(d)
                               if f.startswith('serial-') and f.endswith('.log'))
            if not files:
                return
            with open(os.path.join(d, files[-1]), 'rb') as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - SCROLLBACK // 4))
                tail = f.read()
            self.buf += ('\r\n--- 以下为历史串口日志(%s) ---\r\n' % files[-1]).encode()
            self.buf += tail
            self.buf += '\r\n--- 历史结束 ---\r\n'.encode()
            # 历史装饰文字不能参与提示符判断;只用新收到的 console_tail 确认现场。
        except OSError:
            pass

    @property
    def label(self):
        """用户指定优先,其次嗅探到的 hostname,最后退回 by-path。"""
        custom = load_labels().get(self.key, {}).get('label')
        return safe_name(custom or self.hostname or self.key.rsplit(':', 1)[0],
                         fallback='unnamed')

    def stable_dir(self):
        """日志落盘的**稳定目录**:按 by-path 命名,与 hostname/编号无关。

        踩过的坑:早先直接按 label 建目录,而 label 在嗅探到 hostname 的瞬间会从
        by-path 变成 hostname —— 于是同一块板子的日志被劈成两个目录,前半截"消失"。
        落盘路径必须只依赖物理锚点。
        """
        suffix = hashlib.sha256(self.key.encode('utf-8')).hexdigest()[:20]
        # 去掉接口号/截断前缀曾让不同串口共用日志;摘要必须覆盖完整 by-path。
        d = os.path.join(LOG_ROOT, safe_name(self.key) + '-' + suffix)
        os.makedirs(d, exist_ok=True)
        return d

    def legacy_dir(self):
        return os.path.join(LOG_ROOT, safe_name(self.key.rsplit(':', 1)[0], 'unnamed'))

    def link_label(self):
        """在稳定目录旁挂一个以编号命名的软链,方便人按编号找日志。"""
        try:
            want = self.label
            real = self.stable_dir()
            if not want or os.path.basename(real) == want:
                return
            link = os.path.join(LOG_ROOT, want)
            if os.path.islink(link):
                target = os.readlink(link)
                if target == real:
                    return
                if target != self.legacy_dir():
                    return                 # 重名标签不能抢走另一设备的日志别名
                os.unlink(link)
            elif os.path.exists(link):
                return                     # 已有同名实体目录,别动
            os.symlink(real, link)
        except OSError:
            pass

    def open_log(self):
        path = os.path.join(self.stable_dir(), 'serial-%s.log' % time.strftime(DATE_FMT))
        if self.logf and self.logpath == path:
            return
        if self.logf:
            self.logf.close()
            self.logf = None
        self.logf = open(path, 'a', encoding='utf-8', buffering=1)
        self.logpath = path
        self.link_label()

    def _log_text(self, text):
        try:
            self.open_log()
            stamp = time.strftime('%Y-%m-%d %H:%M:%S')
            for raw in text.split('\n'):
                line = ANSI.sub('', raw.rstrip('\r').split('\r')[-1]).rstrip()
                if line:
                    self.logf.write('%s  %s\n' % (stamp, line))
        except (OSError, ValueError):
            # 磁盘满/目录不可写不能杀死唯一采集线程,内存扇出仍要继续。
            if self.logf:
                try:
                    self.logf.close()
                except OSError:
                    pass
            self.logf = None

    def append(self, data, received=True):
        b = data if isinstance(data, bytes) else data.encode('utf-8', 'replace')
        with self.log_lock:
            if received:
                text = self.decoder.decode(b)
                self.log_pending += text
                lines = self.log_pending.split('\n')
                self.log_pending = lines.pop()
                if lines:
                    self._log_text('\n'.join(lines))
                if len(self.log_pending) > 8192:
                    self._log_text(self.log_pending)
                    self.log_pending = ''
            else:
                text = b.decode('utf-8', 'replace')
                self._log_text(text)
                # 状态文字插入半个 UTF-8 字符/ANSI 序列中会破坏原始串口流。
                # 本地诊断只落日志;终端缓冲始终保持采集器实际收到的字节顺序。
                return
            with self.cv:
                self.buf += b
                if len(self.buf) > SCROLLBACK:
                    drop = len(self.buf) - SCROLLBACK
                    del self.buf[:drop]
                    self.base += drop
                if received:
                    self.bytes += len(b)
                    self.last_rx = time.time()
                    self.console_tail = (self.console_tail + text)[-4096:]
                    self.sniff(self.console_tail)
                self.cv.notify_all()

    def close_log(self):
        with self.log_lock:
            self.log_pending += self.decoder.decode(b'', final=True)
            if self.log_pending:
                self._log_text(self.log_pending)
            self.log_pending = ''
            self.decoder.reset()
            if self.logf:
                try:
                    self.logf.close()
                except OSError:
                    pass
                self.logf = None

    def _write(self, text):
        with self.write_lock:
            if self.stop.is_set() or not self.present or self.fd is None:
                return False, '设备未打开或已重新枚举'
            return write_serial(os.path.join(BY_PATH, self.key), text,
                                identity=self.identity, cancel=self.stop)

    def send(self, text):
        # probing 布尔值的检查/赋值并不原子;所有写入口共用事务锁才不会串话。
        if not self.operation_lock.acquire(blocking=False):
            return False, '已有控制台操作在进行中,稍候'
        try:
            return self._write(text)
        finally:
            self.operation_lock.release()

    def sniff(self, text):
        """被动嗅探 hostname —— 不改动板子,只读它自己吐出来的内容。"""
        for line in text.split('\n'):
            s = ANSI.sub('', line.rstrip('\r').split('\r')[-1]).strip()
            if not s:
                continue
            for i, pat in enumerate(HOST_PATTERNS):
                if i == 4:
                    continue
                m = pat.match(s)
                if m:
                    h = m.group(1)
                    if h and h != self.hostname and not h.startswith('localhost'):
                        self.hostname = h
                        return

    def read(self, off, wait=15.0):
        if off <= 0:
            wait = 0        # 首次加载/切换设备:立刻返回已有内容。
                            # 否则会白等一次长轮询(最多 15s),表现为"切设备很慢"。
        deadline = time.monotonic() + wait
        with self.cv:
            while (self.fd is not None and off >= self.base + len(self.buf)
                   and time.monotonic() < deadline):
                self.cv.wait(1.0)
            start = max(off - self.base, 0) if off > self.base else 0
            return self.base + len(self.buf), bytes(self.buf[start:]), self.fd is not None

    # ---- 在串口上跑命令(识别 / 开 SSH) ----
    # 注意:串口是独占的,这些操作会往板子控制台打字,和"读"共用同一条链路,
    # 但不会和采集线程抢读(写是独立通路)。同一时刻只允许一个这样的操作。

    def now_off(self):
        with self.cv:
            return self.base + len(self.buf)

    def _since(self, base):
        """取缓冲里绝对偏移 base 之后的内容(缓冲会裁剪,所以要按绝对偏移换算)。"""
        with self.cv:
            i = max(base - self.base, 0)
            return bytes(self.buf[i:]).decode('utf-8', 'replace')

    def _say(self, msg):
        self.append('\r\n*** %s ***\r\n' % msg, received=False)

    def _wait(self, base, pattern, timeout=20.0):
        rx = re.compile(pattern)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.stop.is_set() or self.fd is None:
                return None
            m = rx.search(ANSI.sub('', self._since(base)))
            if m:
                return m
            self.stop.wait(min(0.3, max(0, end - time.monotonic())))
        return None

    def ensure_shell(self, base):
        """仅从现场输出判断登录状态,兼容 ANSI、免密码登录以及 CR/LF。"""
        with self.cv:
            tail = ANSI.sub('', self.console_tail).rstrip()[-120:]
        if self.newline is not None and re.search(RE_PROMPT, tail):
            return True, base
        at_login = bool(re.search(RE_LOGIN + r'\s*$', tail))
        if not at_login or self.newline is None:
            at_login = False
            # 有的板子只认 LF;先试本连接成功过的换行,只在没收到提示符时尝试另一种。
            preferred = self.newline or '\r'
            for newline in (preferred, '\n' if preferred == '\r' else '\r'):
                base = self.now_off()
                if not self._write(newline)[0]:
                    return False, base
                match = self._wait(base, r'([#$]|' + RE_LOGIN + r')\s*$', 4)
                if match:
                    self.newline = newline
                    if re.search(RE_PROMPT, match.group(0)):
                        return True, self.now_off()
                    at_login = True
                    break
        if not at_login:
            return False, base
        self._say('停在登录提示符,自动登录 root')
        base = self.now_off()
        if not self._write('root' + self.newline)[0]:
            return False, base
        failure = r'(?:[Ll]ogin incorrect|登录错误|认证失败|Authentication failure)'
        match = self._wait(base, RE_PASSWD + r'\s*$|' + RE_PROMPT + '|' + failure, 12)
        if not match or re.search(failure, match.group(0)):
            return False, base
        if re.search(RE_PROMPT, match.group(0)):
            return True, self.now_off()       # 免密码登录不能继续盲发默认密码
        base = self.now_off()
        if not self._write('bianbu' + self.newline)[0]:
            return False, base
        match = self._wait(base, RE_PROMPT + '|' + failure, 20)
        return (bool(match and re.search(RE_PROMPT, match.group(0))), self.now_off())

    def _run_cmd(self, cmd, timeout=30.0):
        """一个事务覆盖登录、完整发送和带 nonce 的响应,禁止其他写入口插队。"""
        if not self.operation_lock.acquire(blocking=False):
            return False, '已有一次识别/操作在进行中,稍候'
        self.probing = True
        try:
            if self.fd is None or self.stop.is_set():
                return False, '设备未打开(串口没接上或已被拔掉)'
            ok, base = self.ensure_shell(self.now_off())
            if not ok:
                return False, '板子不在 shell 提示符,且自动登录失败(可能需要手动处理)'
            nonce = secrets.token_hex(12)
            begin, end = '__SD_B_' + nonce + '__', '__SD_E_' + nonce + '__'
            # printf 参数拆开 marker,回显再怎么分块也不会成为完整标记行。
            framed = ("printf '\\n%s%s\\n' '__SD_B_' '%s__'; %s; "
                      "printf '\\n%s%s\\n' '__SD_E_' '%s__'")
            framed = framed % ('%s', '%s', nonce, cmd, '%s', '%s', nonce)
            base = self.now_off()
            ok, err = self._write(framed + self.newline)
            if not ok:
                return False, err
            if not self._wait(base, r'(?m)^' + re.escape(end) + r'\r?\n', timeout):
                return False, '等待板子完整响应超时'
            raw = ANSI.sub('', self._since(base)).replace('\r\n', '\n')
            match = re.search(r'(?m)^' + re.escape(begin) + r'\n([\s\S]*?)^'
                              + re.escape(end) + r'\n', raw)
            if not match:
                return False, '响应帧不完整(可能超出缓冲上限)'
            return True, match.group(1)
        finally:
            self.probing = False
            self.operation_lock.release()

    def run_probe(self):
        """识别:hostname / 板子序列号(device-tree serial-number) / IP / sshd 状态。"""
        origin = self.stop
        ok, raw = self._run_cmd(PROBE_CMD, timeout=30)
        if not ok:
            return {'ok': False, 'error': raw}
        try:
            info = parse_probe(raw)
        except ValueError as e:
            return {'ok': False, 'error': str(e)}
        info['ok'] = True
        info['ts'] = time.time()
        with self.operation_lock:
            if self.stop is not origin or origin.is_set():
                return {'ok': False, 'error': '识别期间设备已重新枚举'}
            self.probe_info = info
            # 板子序列号比 hostname 可靠得多:hostname 可能是出厂的默认名、还可能重复。
            # 但不拿它当目录键 —— 那要能登录才拿得到,离线板子就没有;目录键仍用 by-path。
            if info.get('serial') and not self.hostname:
                self.hostname = info['serial']
            self._say('识别结果: 序列号=%s 主机名=%s IP=%s sshd=%s'
                      % (info.get('serial') or '-', info.get('hostname') or '-',
                         ','.join(info.get('ips') or []) or '-', info.get('sshd') or '-'))
        return info

    def ssh_connect(self):
        """一键 SSH:用识别到的 IP 起一条**独立会话**(默认 root/bianbu)。

        没有 IP 就先识别一次 —— 这正是"识别设备"存在的意义:串口负责拿到
        IP/序列号/sshd 状态,SSH 会话负责后续高效操作。两者并存、各记各的日志。
        """
        origin = self.stop
        info = self.probe_info
        if not info.get('ips'):
            r = self.run_probe()
            if not r.get('ok'):
                return None, '需要先拿到 IP,但识别失败: %s' % r.get('error')
            info = self.probe_info
        ip = None
        for x in (info.get('ips') or []):
            iface, cidr = (x.split(' ', 1) + [''])[:2]
            if iface == 'docker0' or cidr.startswith('172.17.') or cidr.startswith('127.'):
                continue                      # 跳过 docker0 之类的内部地址
            ip = cidr.split('/')[0]
            break
        if not ip:
            return None, '没找到可用 IP(只看到 docker0 之类的内部地址)'
        self.reap_sessions()
        with self.sessions_lock:
            if len(self.ssh_sessions) + self.ssh_pending >= SSH_LIMIT:
                return None, 'SSH 会话已达上限(%d),请先关闭闲置会话' % SSH_LIMIT
            self.ssh_seq += 1
            sid = 'ssh%d' % self.ssh_seq
            self.ssh_pending += 1
        session = None
        try:
            if self.stop is not origin or origin.is_set():
                return None, '设备已重新枚举,请重新识别 IP'
            session = SshSession(sid, ip, self.label, self.stable_dir())
            if not session.ensure():
                session.close()
                return None, '建立 SSH 会话失败'
            with self.operation_lock:
                if self.stop is not origin or origin.is_set():
                    session.close()
                    return None, '设备已重新枚举,SSH 会话已关闭'
                with self.sessions_lock:
                    self.ssh_sessions[sid] = session
            return session, ''
        except (OSError, RuntimeError) as e:
            if session:
                session.close()
            return None, str(e)
        finally:
            with self.sessions_lock:
                self.ssh_pending -= 1

    def get_session(self, sid):
        with self.sessions_lock:
            return self.ssh_sessions.get(sid)

    def list_sessions(self):
        with self.sessions_lock:
            sessions = list(self.ssh_sessions.values())
        return [s.snapshot() for s in sessions]

    def close_session(self, sid):
        with self.sessions_lock:
            session = self.ssh_sessions.pop(sid, None)
        if session:
            session.close()                  # join/wait 不得放在注册表锁内
        return session is not None

    def reap_sessions(self):
        expired = []
        now = time.monotonic()
        with self.sessions_lock:
            for sid, session in list(self.ssh_sessions.items()):
                with session.cv:
                    if not session.is_idle(now, SSH_IDLE):
                        continue
                    # 判闲置和标记关闭必须原子;不能把刚被用户续用的会话关掉。
                    session.closed = True
                    session._stop.set()
                expired.append(self.ssh_sessions.pop(sid))
        for session in expired:
            session.close()

    def run_ssh_enable(self):
        """一键开 SSH:装了就直接启,没装先装(默认 root/bianbu 登录后执行)。"""
        ok, raw = self._run_cmd(SSH_CMD, timeout=240)
        if not ok:
            return {'ok': False, 'error': raw}
        m = None
        for m in re.finditer(r'^SSHD=([^\r\n]+)$', raw or '', re.M):
            pass
        state = m.group(1).strip() if m else 'unknown'
        good = state in ('active', 'activating')
        self._say('一键开 SSH: sshd=%s' % state)
        return {'ok': good, 'sshd': state,
                'error': '' if good else 'sshd 未能启动: ' + state}

    def snapshot(self):
        tail = ''
        with self.cv:
            last = bytes(self.buf[-400:]).decode('utf-8', 'replace')
        for line in reversed(last.split('\n')):
            s = ANSI.sub('', line.rstrip('\r').split('\r')[-1]).strip()
            if s:
                tail = s[:160]
                break
        return {'key': self.key, 'dev': self.dev, 'label': self.label,
                'hostname': self.hostname, 'alive': self.fd is not None,
                'bytes': self.bytes, 'last_rx': self.last_rx,
                'last_line': tail, 'log': os.path.basename(self.logpath or ''),
                'probes': self.probed, 'fail_count': self.fail_count,
                'rescue_note': self.rescue_note, 'busy': self.probing,
                'info': self.probe_info}


class Manager:
    """设备发现 + 每个实际 tty 一个采集线程;旧线程退出后才转移所有权。"""

    def __init__(self):
        self.devices = {}
        self.lock = threading.Lock()
        self.scan_lock = threading.Lock()

    @staticmethod
    def _matches(d):
        try:
            s = os.stat(os.path.join(BY_PATH, d.key))
            return (s.st_dev, s.st_ino, s.st_rdev) == d.identity
        except OSError:
            return False

    def scan(self):
        with self.scan_lock:
            found = {}
            try:
                names = sorted(os.listdir(BY_PATH))
            except OSError:
                names = []
            with self.lock:
                existing = dict(self.devices)
            # 新 udev 会给同一 tty 多个别名;优先保留原锚点,按设备号去重。
            names.sort(key=lambda key: (key not in existing, key))
            seen = set()
            for key in names:
                path = os.path.join(BY_PATH, key)
                try:
                    dev = os.path.realpath(path)
                    st = os.stat(path)
                    if not stat.S_ISCHR(st.st_mode) or st.st_rdev in seen:
                        continue
                    seen.add(st.st_rdev)
                    found[key] = (dev, (st.st_dev, st.st_ino, st.st_rdev))
                except OSError:
                    continue
            stopping = []
            for key, d in existing.items():
                if found.get(key) != (d.dev, d.identity):
                    d.present = False
                    d.stop.set()
                    if d.thread and d.thread.is_alive():
                        stopping.append(d.thread)
            for thread in stopping:
                thread.join(timeout=0.1)
            # 不能直接改 d.dev 让旧线程重开被复用的 ttyUSB;必须先等 close/finally 完成。
            if any(thread.is_alive() for thread in stopping):
                return {key: value[0] for key, value in found.items()}
            for key, (dev, identity) in found.items():
                with self.lock:
                    d = self.devices.get(key)
                if d is None:
                    d = Device(key, dev)         # 文件 IO 不占用全局注册表锁
                    with self.lock:
                        self.devices[key] = d
                if d.thread and d.thread.is_alive():
                    continue
                if not d.operation_lock.acquire(blocking=False):
                    continue                    # 旧识别事务退出前不能把新连接交给同一对象
                try:
                    if d.identity != identity:
                        d.probe_info = {}
                        d.hostname = ''
                    d.dev, d.identity = dev, identity
                    d.present = True
                    d.dead = False
                    d.stop = threading.Event()
                    d.console_tail = ''
                    d.newline = None
                    d.thread = threading.Thread(target=self._capture, args=(d,), daemon=True)
                    d.thread.start()
                finally:
                    d.operation_lock.release()
            return {key: value[0] for key, value in found.items()}

    @staticmethod
    def _close_reader(d):
        # 写入最多等待 5 秒;关闭不能和写侧交错,更不能占用输出 cv 等待串口。
        with d.write_lock:
            fd, d.fd = d.fd, None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        with d.cv:
            d.cv.notify_all()

    def _capture(self, d):
        try:
            while not d.stop.is_set() and self._matches(d):
                if d.fd is None:
                    try:
                        with d.write_lock:
                            if d.stop.is_set() or not self._matches(d):
                                break
                            with d.cv:
                                d.console_tail = ''
                                d.newline = None
                            d.fd = open_serial(os.path.join(BY_PATH, d.key),
                                               flush=False, identity=d.identity)
                            # 重连也可能已有启动日志排队,flush 会直接丢掉这些字节。
                        d.fail_count = 0
                        d._say('seriald 已接入 %s' % d.dev)
                    except (OSError, termios.error) as e:
                        self._close_reader(d)
                        d.fail_count += 1
                        delay = min(5 * (2 ** min(d.fail_count - 1, 4)), 60)
                        if d.fail_count == 1 or time.time() - d.fail_logged > 60:
                            d.fail_logged = time.time()
                            d._say('打不开 %s: %s (退避 %ds 重试)' % (d.dev, e, delay))
                        d.stop.wait(delay)
                        continue
                try:
                    ready, _, _ = select.select([d.fd], [], [], 0.5)
                    if not ready:
                        continue
                    data = os.read(d.fd, 65536)
                    if not data:
                        # EOF 的 fd 会一直 readable;不关闭/退避会空转占满 CPU。
                        raise OSError(errno.EIO, '串口 EOF')
                    d.append(data)
                except (BlockingIOError, InterruptedError):
                    continue
                except (OSError, ValueError) as e:
                    self._close_reader(d)
                    d._say('%s 读取中断: %s' % (d.dev, e))
                    d.stop.wait(1.0)
                    # 断线后的重开也属于新连接;交回 scan 等旧操作退出,不能原地换 fd。
                    break
        finally:
            d.stop.set()
            self._close_reader(d)
            d.dead = True
            d.close_log()

    def probe_all(self):
        """保留显式探测入口;所有发送必须通过同一个控制台事务锁。"""
        now = time.time()
        with self.lock:
            devs = list(self.devices.values())
        for d in devs:
            if d.hostname or d.fd is None or now - d.started < 20:
                continue
            if now - d.last_rx < 15 or d.probed >= 5 or now - d.last_probe < 60:
                continue
            ch = '\r' if d.probed % 2 == 0 else '\n'
            ok, _ = d.send(ch)
            if ok:
                d.last_probe = now
                d.probed += 1
                d._say('seriald 探测 #%d:已发送 %s' % (d.probed, repr(ch)))

    def loop(self):
        while True:
            try:
                self.scan()
                with self.lock:
                    devs = list(self.devices.values())
                for d in devs:
                    d.link_label()
                    d.reap_sessions()
                # 启动/重连只被动采集;自动回车或 USB 软拔插会干扰正在使用的控制台。
            except Exception as e:
                print('seriald scan: %s' % e, file=sys.stderr, flush=True)
            time.sleep(3)


MGR = Manager()


class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = 'seriald/1.0'

    def _json(self, code, payload):
        if getattr(self, 'response_key', None):
            payload = dict(payload, key=self.response_key)
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _dev(self, q):
        key = (q.get('key') or [''])[0]
        self.response_key = key
        with MGR.lock:
            return MGR.devices.get(key)

    def do_GET(self):
        self.response_key = None
        u = urlparse(self.path)
        route, q = u.path, parse_qs(u.query)

        if route == '/api/devices':
            with MGR.lock:
                devices = list(MGR.devices.values())
            devs = [d.snapshot() for d in devices]
            devs.sort(key=lambda x: x['key'])
            return self._json(200, {'devices': devs, 'by_path': BY_PATH,
                                    'server_time': time.time()})

        if route == '/api/serial':
            d = self._dev(q)
            if not d:
                return self._json(404, {'error': 'no such device'})
            try:
                off = int((q.get('off') or ['0'])[0] or 0)
                if off < 0:
                    raise ValueError
            except ValueError:
                return self._json(400, {'error': 'bad offset'})
            new_off, data, alive = d.read(off)
            return self._json(200, {'key': d.key, 'dev': d.dev, 'off': new_off,
                                    'alive': alive, 'data_b64': base64.b64encode(data).decode('ascii'),
                                    'data': data.decode('utf-8', 'replace')})

        if route == '/api/session':
            d = self._dev(q)
            if not d:
                return self._json(404, {'error': 'no such device'})
            sid = (q.get('sid') or ['serial'])[0]
            try:
                off = int((q.get('off') or ['0'])[0] or 0)
                if off < 0:
                    raise ValueError
            except ValueError:
                return self._json(400, {'error': 'bad offset'})
            if sid == 'serial':
                new_off, data, alive = d.read(off)
                return self._json(200, {'sid': 'serial', 'dev': d.dev, 'off': new_off,
                                        'alive': alive, 'data_b64': base64.b64encode(data).decode('ascii'),
                                    'data': data.decode('utf-8', 'replace')})
            s = d.get_session(sid)
            if not s:
                return self._json(404, {'error': 'no such session'})
            new_off, data, alive = s.read(off)
            return self._json(200, {'sid': sid, 'ip': s.ip, 'off': new_off,
                                    'alive': alive, 'data_b64': base64.b64encode(data).decode('ascii'),
                                    'data': data.decode('utf-8', 'replace')})

        if route == '/api/sessions':
            d = self._dev(q)
            if not d:
                return self._json(404, {'error': 'no such device'})
            out = [{'sid': 'serial', 'kind': 'serial', 'dev': d.dev,
                    'alive': d.fd is not None}]
            out += d.list_sessions()
            return self._json(200, {'sessions': out, 'label': d.label})

        if route == '/api/logs':
            d = self._dev(q)
            if not d:
                return self._json(404, {'error': 'no such device'})
            d.link_label()
            real = d.stable_dir()
            out = []
            directories = [real]
            legacy = d.legacy_dir()
            if legacy != real and os.path.isdir(legacy):
                directories.append(legacy)
            for directory in directories:
                for fn in sorted(os.listdir(directory), reverse=True):
                    if not (fn.startswith(('serial-', 'ssh-')) and fn.endswith('.log')):
                        continue
                    p = os.path.join(directory, fn)
                    try:
                        out.append({'name': ('旧目录/' if directory != real else '') + fn,
                                    'size': os.path.getsize(p),
                                    'path': 'logs/%s/%s' % (os.path.basename(directory), fn)})
                    except OSError:
                        pass
            return self._json(200, {'label': d.label, 'dir': os.path.basename(real),
                                    'alias': d.label, 'files': out})

        if route == '/':
            return self._page()
        return super().do_GET()

    def do_POST(self):
        self.response_key = None
        route = urlparse(self.path).path
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n < 0 or n > 65536:
                return self._json(413, {'error': 'body must be at most 65536 bytes'})
            if self.headers.get('Transfer-Encoding'):
                return self._json(400, {'error': 'transfer encoding unsupported'})
            raw = self.rfile.read(n)
            if len(raw) != n:
                return self._json(400, {'error': 'incomplete body'})
            payload = json.loads(raw.decode('utf-8')) if n else {}
            if not isinstance(payload, dict):
                return self._json(400, {'error': 'JSON object required'})
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {'error': 'bad json'})
        except TimeoutError:
            return self._json(408, {'error': 'request body timed out'})
        q = {'key': [str(payload.get('key') or '')]}
        d = self._dev(q)
        if not d:
            return self._json(404, {'error': 'no such device'})

        if route == '/api/send':
            text = str(payload.get('data') or '')
            if not text:
                return self._json(400, {'error': 'data required'})
            ok, err = d.send(text)
            return self._json(200 if ok else 503, {'ok': ok, 'error': err, 'dev': d.dev})

        if route == '/api/probe':
            ok, err = d.send('\r')
            if ok:
                d.last_probe = time.time()
                d.probed += 1
                d._say('手动探测:已发送回车')
            return self._json(200, {'ok': ok, 'error': err})

        if route == '/api/rescue':
            # 手动自愈。mode: soft=只关 autosuspend(最轻), rebind=追加软拔插。
            # 分两步是为了能区分病因,见 rescue.sh 注释。
            mode = str(payload.get('mode') or 'soft')
            if mode not in ('soft', 'rebind'):
                return self._json(400, {'error': 'mode must be soft|rebind'})
            if not d.operation_lock.acquire(blocking=False):
                return self._json(409, {'ok': False, 'error': '已有控制台操作在进行中'})
            try:
                with d.write_lock:
                    if not d.present or not MGR._matches(d):
                        return self._json(409, {'ok': False, 'error': '设备已重新枚举,请刷新后重试'})
                    d.last_rescue = time.time()
                    rx_before = d.bytes
                    ok, note = try_rescue(d.dev, mode)
                d.rescue_note = note
                d.fail_count = 0
                d._say('手动自愈(%s)%s: %s' % (mode, '步骤完成' if ok else '失败', note))
            finally:
                d.operation_lock.release()
            # sysfs 写成功只能证明辅助步骤完成;不能据此宣布采集器已恢复。
            recovered = d.fd is not None and d.bytes > rx_before and MGR._matches(d)
            requires_setup = not ok and any(x in note.lower() for x in
                                           ('未安装', 'password', 'not allowed', 'sudo 需要密码'))
            hint = ''
            if requires_setup:
                hint = (
                    '一次配置,之后免密且只放行这一个脚本(需要 root):\n'
                    '  sudo install -o root -g root -m 0755 \\\n'
                    '      /data/seriald/rescue.sh /usr/local/sbin/seriald-rescue.sh\n'
                    "  echo 'qiu ALL=(root) NOPASSWD: /usr/local/sbin/seriald-rescue.sh' \\\n"
                    '      | sudo tee /etc/sudoers.d/seriald-rescue\n'
                    '  sudo chmod 0440 /etc/sudoers.d/seriald-rescue && sudo visudo -c\n'
                    '\n'
                    '为什么装在 /usr/local/sbin:白名单脚本若对 qiu 可写,'
                    '改一行就等于通用提权 —— 所以它必须在 root 拥有、qiu 不可写的目录里。')
            return self._json(200, {'ok': ok, 'recovered': recovered, 'requires_setup': requires_setup,
                                    'note': note, 'hint': hint, 'mode': mode})

        if route == '/api/ssh-connect':
            # 一键 SSH:起一条与串口并存的独立会话,输出单独落盘
            s, err = d.ssh_connect()
            if not s:
                return self._json(502, {'ok': False, 'error': err})
            return self._json(200, {'ok': True, 'sid': s.sid, 'ip': s.ip})

        if route == '/api/session-send':
            sid = str(payload.get('sid') or 'serial')
            text = str(payload.get('data') or '')
            if not text:
                return self._json(400, {'error': 'data required'})
            if sid == 'serial':
                ok, err = d.send(text)
            else:
                s = d.get_session(sid)
                if not s:
                    return self._json(404, {'error': 'no such session'})
                ok, err = (s.write(text), '')
            return self._json(200 if ok else 503, {'ok': ok, 'error': err})

        if route == '/api/session-resize':
            sid = str(payload.get('sid') or '')
            try:
                cols = int(payload.get('cols') or 0)
                rows = int(payload.get('rows') or 0)
            except (TypeError, ValueError):
                return self._json(400, {'error': 'bad cols/rows'})
            s = d.get_session(sid)
            if not s:
                return self._json(404, {'error': 'no such session'})
            return self._json(200, {'ok': s.resize(cols, rows), 'cols': s.cols, 'rows': s.rows})

        if route == '/api/session-close':
            sid = str(payload.get('sid') or '')
            if not d.close_session(sid):
                return self._json(404, {'error': 'no such session'})
            return self._json(200, {'ok': True})

        if route == '/api/identify':
            # 完整识别:必要时自动登录,再采集 device-tree 序列号 / hostname / IP / sshd。
            # 会比 /api/probe(只发个回车促成提示符)慢,但拿到的是硬标识。
            info = d.run_probe()
            return self._json(200 if info.get('ok') else 502, info)

        if route == '/api/ssh-enable':
            r = d.run_ssh_enable()
            return self._json(200 if r.get('ok') else 502, r)

        if route == '/api/label':
            label = safe_name(str(payload.get('label') or ''), fallback='')
            if not label:
                return self._json(400, {'error': 'label required'})
            with _lock:
                m = load_labels()
                m.setdefault(d.key, {})['label'] = label
                m[d.key]['dev'] = d.dev
                save_labels(m)
            return self._json(200, {'ok': True, 'label': label})

        return self._json(404, {'error': 'not found'})

    def end_headers(self):
        if self.path in ('/', ''):
            self.send_header('Cache-Control', 'no-cache, must-revalidate')
        super().end_headers()

    def log_message(self, fmt, *args):
        pass

    def _page(self):
        path = os.path.join(STATIC, 'index.html')
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except OSError:
            body = b'<h1>seriald</h1><p>index.html missing</p>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BoundedThreadingServer(socketserver.ThreadingTCPServer):
    """长轮询也占一个线程;给连接数和请求读入时间都设硬上限。"""
    allow_reuse_address = True
    daemon_threads = True
    max_workers = 64
    request_timeout = 15.0

    def __init__(self, *args, **kwargs):
        self._workers = threading.BoundedSemaphore(self.max_workers)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        # 必须在线程创建之前拿配额;在线程内部排队仍会被慢连接耗尽线程/内存。
        if not self._workers.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(self.request_timeout)
            super().process_request(request, client_address)
        except BaseException:
            self._workers.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._workers.release()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8898
    bind = sys.argv[2] if len(sys.argv) > 2 else os.environ.get('SERIALD_BIND', '10.0.90.177')
    os.makedirs(LOG_ROOT, exist_ok=True)
    handler = functools.partial(Handler, directory=STATIC)
    with BoundedThreadingServer((bind, port), handler) as httpd:
        # 先占用监听端口再启动采集,否则重复启动失败前就可能多开一组读者。
        threading.Thread(target=MGR.loop, daemon=True).start()
        print('seriald on http://%s:%d/  (by-path=%s)' % (bind, port, BY_PATH), flush=True)
        httpd.serve_forever()


if __name__ == '__main__':
    main()
