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
import fcntl
import functools
import http.server
import json
import os
import pty
import re
import secrets
import select
import socketserver
import struct
import subprocess
import sys
import termios
import threading
import time
from urllib.parse import urlparse, parse_qs

BASE = '/data/seriald'
LOG_ROOT = os.path.join(BASE, 'logs')
LABELS = os.path.join(BASE, 'labels.json')
BY_PATH = '/dev/serial/by-path'
# 自愈脚本的安装位置。必须由 root 拥有且调用者不可写,否则 sudoers 白名单等于通用提权。
# 源文件在 /data/seriald/rescue.sh,安装命令见 /api/rescue 返回的 hint。
RESCUE_BIN = '/usr/local/sbin/seriald-rescue.sh'

BAUD = 115200
SCROLLBACK = 256 * 1024          # 每设备内存缓冲上限(页面实时视图用)
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
    return s[:48] or fallback


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


def open_serial(dev, flush=True):
    """115200 8N1,无流控,raw,不回显。只有采集器调这个(唯一读者)。

    flush 只在首次打开时用:重开时的 tcflush 会丢掉内核缓冲里尚未读走的数据,
    实测会把现场截断。
    """
    fd = os.open(dev, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    a = termios.tcgetattr(fd)
    a[0] = 0
    a[1] = 0
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    a[3] = 0
    a[4] = termios.B115200
    a[5] = termios.B115200
    a[6][termios.VMIN] = 0
    a[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, a)
    if flush:
        termios.tcflush(fd, termios.TCIFLUSH)
    return fd


def write_serial(dev, data):
    """铁律 2/3:只写不读、不碰 termios。"""
    try:
        fd = os.open(dev, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as e:
        return False, str(e)
    try:
        os.write(fd, data.encode('utf-8', 'replace'))
        return True, ''
    except OSError as e:
        return False, str(e)
    finally:
        os.close(fd)


# 一次"识别"采集的内容。用成对标记包起来,便于从串口流里可靠截取 ——
# 串口上有回显、有提示符、有 ANSI,不定界就没法 parse。
#
# 关键:标记后面挂 `$(date +%s)`。回显里显示的是字面量 `__SD_E__$(date +%s)`,
# 而真实输出是 `__SD_E__1789...`。于是"等 `__SD_E__\d`"就不会被回显骗到 ——
# 早先标记是纯字面量,_wait 直接匹配上回显,秒返回并 parse 出一堆空值(实测踩到)。
PROBE_CMD = (
    "echo __SD_B__$(date +%s); "
    "echo HOST=$(hostname 2>/dev/null); "
    "echo SERIAL=$(cat /proc/device-tree/serial-number 2>/dev/null | tr -d '\\0'); "
    "echo MODEL=$(cat /proc/device-tree/model 2>/dev/null | tr -d '\\0'); "
    "ip -4 -o addr show 2>/dev/null | awk '{print \"IP=\"$2\" \"$4}'; "
    "echo SSHD=$(systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null || echo none); "
    "echo __SD_E__$(date +%s)"
)

# 一键开 SSH 服务端(装了就直接启,没装先 apt 装)。同样用 nonce 标记。
SSH_CMD = (
    "command -v sshd >/dev/null 2>&1 || "
    "(apt-get update -qq >/dev/null 2>&1; DEBIAN_FRONTEND=noninteractive "
    "apt-get install -y -qq openssh-server >/dev/null 2>&1); "
    "systemctl enable --now ssh >/dev/null 2>&1 || systemctl enable --now sshd >/dev/null 2>&1; "
    "sleep 1; echo __SD_SSH__$(date +%s)=$(systemctl is-active ssh 2>/dev/null || "
    "systemctl is-active sshd 2>/dev/null || echo failed)"
)

# 结束标记的"展开后"形态
PROBE_END_RE = r'__SD_E__\d'
SSH_END_RE = r'__SD_SSH__\d'

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
    steps = []

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
        steps.append('关 autosuspend 失败: %s' % (e.strerror or e))
    if mode == 'rebind':
        for act in ('unbind', 'bind'):
            try:
                with open('/sys/bus/usb/drivers/usb/%s' % act, 'w') as f:
                    f.write(name)
                steps.append(act)
            except OSError as e:
                steps.append('%s 失败: %s' % (act, e.strerror or e))
    ok = any('已关' in s or s == 'bind' for s in steps)
    return ok, '%s [%s]' % ('; '.join(steps), name)


def parse_probe(raw):
    """从串口回显里解析识别结果。

    串口上命令会被回显一遍,输出再来一遍;且回显里含 `HOST=$(hostname ...)` 这种字面量,
    会和真实输出撞名。所以:先按成对标记定界,再同名**取最后一次出现**,
    并丢掉含 `$(` / `2>/dev/null` 的伪匹配(那是回显不是结果)。
    """
    out = {'hostname': '', 'serial': '', 'model': '', 'ips': [], 'sshd': ''}
    # 取**最后一个** __SD_B__ 之后的内容:回显里那个是字面量,真实输出在后
    i = raw.rfind('__SD_B__')
    body = raw[i:] if i >= 0 else raw
    body = body.split('__SD_E__')[0]

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
        self.fd = None
        self.proc = None
        self.started = time.time()
        self.last_touch = time.time()
        self.cols = 120                   # 默认尺寸;浏览器连上后会按实际宽度覆盖
        self.rows = 32
        self.logf = None
        self.logpath = None
        self.argv = ['sshpass', '-p', 'bianbu', 'ssh', '-tt',
                     '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=10',
                     '-o', 'ServerAliveInterval=30', 'root@' + ip]
        self._open_log(logdir)

    def _open_log(self, logdir):
        try:
            path = os.path.join(logdir, 'ssh-%s.log' % time.strftime(DATE_FMT))
            self.logf = open(path, 'a', encoding='utf-8', buffering=1)
            self.logpath = path
        except OSError:
            self.logf = None

    def _log(self, data):
        if not self.logf:
            return
        text = data.decode('utf-8', 'replace')
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        try:
            for raw in text.split('\n'):
                line = ANSI.sub('', raw.rstrip('\r').split('\r')[-1]).rstrip()
                if line:
                    self.logf.write('%s  %s\n' % (stamp, line))
        except OSError:
            pass

    def ensure(self):
        with self.cv:
            if self.fd is not None:
                return
            master, slave = pty.openpty()
            self._set_winsize(slave)      # 必须在 spawn 之前:ssh 启动时读本地 tty 尺寸
                                          # 作为初始 pty 请求发给远端
            try:
                self.proc = subprocess.Popen(
                    self.argv, stdin=slave, stdout=slave, stderr=slave,
                    preexec_fn=os.setsid, env=dict(os.environ, TERM='xterm-256color'),
                    close_fds=True)
            except OSError as e:
                os.close(master); os.close(slave)
                self._append('\r\n*** 建立 SSH 会话失败: %s ***\r\n' % e)
                return
            os.close(slave)
            self.fd = master
            self.last_touch = time.time()
            threading.Thread(target=self._reader, daemon=True).start()
            self._append('\r\n*** 已建立 SSH 会话 root@%s (%dx%d) ***\r\n'
                         % (self.ip, self.cols, self.rows))

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
        self.cols, self.rows = cols, rows
        with self.cv:
            if self.fd is None:
                return False
            self._set_winsize(self.fd)
        return True

    def _reader(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                break
            if not data:
                break
            self._append(data)
        self._append('\r\n*** SSH 会话已结束 ***\r\n')
        with self.cv:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
            self.cv.notify_all()

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
        self.last_touch = time.time()
        deadline = time.time() + wait
        with self.cv:
            while (self.fd is not None and off >= self.base + len(self.buf)
                   and time.time() < deadline):
                self.cv.wait(1.0)
            start = max(off - self.base, 0) if off > self.base else 0
            return self.base + len(self.buf), bytes(self.buf[start:]), self.fd is not None

    def write(self, text):
        self.last_touch = time.time()
        self.ensure()
        with self.cv:
            if self.fd is None:
                return False
            try:
                os.write(self.fd, text.encode('utf-8', 'replace'))
            except OSError:
                return False
            return True

    def close(self):
        try:
            if self.proc:
                os.killpg(os.getpgid(self.proc.pid), 15)
        except OSError:
            pass
        self.append_marker('*** 会话被手动关闭 ***')

    def append_marker(self, text):
        self._append('\r\n%s\r\n' % text)

    def snapshot(self):
        return {'sid': self.sid, 'kind': 'ssh', 'ip': self.ip,
                'alive': self.fd is not None, 'started': self.started,
                'bytes': len(self.buf)}


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
        self.probing = False              # 识别/开 SSH 进行中(串口是独占的,别并发)
        self.probe_info = {}              # 最近一次识别结果
        self.ssh_sessions = {}            # sid -> SshSession(与串口并存的独立会话)
        self.ssh_seq = 0
        self.logf = None
        self.preload()

    def preload(self):
        """启动时把最近一份串口日志的尾部装回内存缓冲。

        否则服务一重启、板子又安静,缓冲就是空的——连提示符都判不出来,
        "识别设备"会在一块明明登录着的板子上失败(实测踩到)。
        """
        try:
            d = self.stable_dir()
            files = sorted(f for f in os.listdir(d)
                           if f.startswith('serial-') and f.endswith('.log'))
            if not files:
                return
            with open(os.path.join(d, files[-1]), encoding='utf-8', errors='replace') as f:
                tail = f.read()[-SCROLLBACK // 4:]
            self.buf += ('\r\n--- 以下为历史串口日志(%s) ---\r\n' % files[-1]).encode()
            self.buf += tail.encode('utf-8')
            self.buf += '\r\n--- 历史结束 ---\r\n'.encode()
        except OSError:
            pass
        self.logpath = None
        self.dead = False

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
        d = os.path.join(LOG_ROOT, safe_name(self.key.rsplit(':', 1)[0], 'unnamed'))
        os.makedirs(d, exist_ok=True)
        return d

    def link_label(self):
        """在稳定目录旁挂一个以编号命名的软链,方便人按编号找日志。"""
        try:
            want = self.label
            real = self.stable_dir()
            if not want or os.path.basename(real) == want:
                return
            link = os.path.join(LOG_ROOT, want)
            if os.path.islink(link):
                if os.readlink(link) == real:
                    return
                os.unlink(link)
            elif os.path.exists(link):
                return                     # 已有同名实体目录,别动
            os.symlink(real, link)
        except OSError:
            pass

    def open_log(self):
        if self.logf:
            return
        self.link_label()
        path = os.path.join(self.stable_dir(), 'serial-%s.log' % time.strftime(DATE_FMT))
        if self.logpath != path:                     # 跨天换文件
            if self.logf:
                self.logf.close()
            self.logf = open(path, 'a', encoding='utf-8', buffering=1)
            self.logpath = path

    def append(self, data):
        self.open_log()
        b = data if isinstance(data, bytes) else data.encode('utf-8', 'replace')
        # 落盘:按行时间戳、剥 ANSI;先 rstrip('\r') 否则 \r\n 行尾会整行变空被丢掉
        try:
            text = b.decode('utf-8', 'replace')
            stamp = time.strftime('%Y-%m-%d %H:%M:%S')
            for raw in text.split('\n'):
                line = ANSI.sub('', raw.rstrip('\r').split('\r')[-1]).rstrip()
                if line:
                    self.logf.write('%s  %s\n' % (stamp, line))
        except (OSError, ValueError):
            pass
        with self.cv:
            self.buf += b
            if len(self.buf) > SCROLLBACK:
                drop = len(self.buf) - SCROLLBACK
                del self.buf[:drop]
                self.base += drop
            self.bytes += len(b)
            self.last_rx = time.time()
            self.sniff(text)
            self.cv.notify_all()

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
        deadline = time.time() + wait
        with self.cv:
            while (self.fd is not None and off >= self.base + len(self.buf)
                   and time.time() < deadline):
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
        self.append('\r\n*** %s ***\r\n' % msg)

    def _wait(self, base, pattern, timeout=20.0):
        rx = re.compile(pattern)
        end = time.time() + timeout
        while time.time() < end:
            m = rx.search(self._since(base))
            if m:
                return m
            time.sleep(0.3)
        return None

    def ensure_shell(self, base):
        """确保板子停在 shell 提示符:在登录提示符就自动登录(默认 root/bianbu)。

        这些开发板的出厂默认凭据,用户指定用它做一键操作。
        返回 (ok, 新的基准偏移)。
        """
        def clean(t):
            # 提示符后面常紧跟 bracketed-paste 之类的控制序列,先剥掉再判尾
            return ANSI.sub('', t)

        tail = clean(self._since(max(base - 4096, 0)))
        if re.search(RE_PROMPT, tail.rstrip()[-60:]):
            return True, base                       # 已经登录着

        # 状态判断看**已积累的缓冲**;下面的 _wait 只用来等"将要发生的事"。
        # (踩过两次:先判断出"在登录提示符",又去 _wait 找它 —— 提示符在 base 之前,
        #  向前找永远找不到,白等到超时,把明明可登录的板子判成失败)
        at_login = bool(re.search(RE_LOGIN + r'\s*$', tail.rstrip()[-60:]))

        if not at_login:
            base = self.now_off()                   # 基准必须在写之前取
            write_serial(self.dev, '\r')
            m = self._wait(base, r'([#$]|' + RE_LOGIN + r')\s*$', 8)
            if not m:
                return False, base
            if not re.search(RE_LOGIN, m.group(1)):
                return True, self.now_off()         # 拿到了 shell 提示符 → 本来就登录着
            at_login = True

        if at_login:
            self._say('停在登录提示符,自动登录 root')
            base = self.now_off()
            write_serial(self.dev, 'root\r')
            if not self._wait(base, RE_PASSWD, 12):
                return False, base
            base = self.now_off()
            write_serial(self.dev, 'bianbu\r')
            if not self._wait(base, RE_PROMPT, 20):
                return False, base
            time.sleep(0.5)
            return True, self.now_off()
        return False, self.now_off()

    def _run_cmd(self, cmd, end_marker, timeout=30.0):
        """发一条命令并等结束标记。串口是独占的,所以用 probing 标志互斥。"""
        if self.fd is None:
            return False, '设备未打开(串口没接上或已被拔掉)'
        if self.probing:
            return False, '已有一次识别/操作在进行中,稍候'
        self.probing = True
        try:
            ok, base = self.ensure_shell(self.now_off())
            if not ok:
                return False, '板子不在 shell 提示符,且自动登录失败(可能需要手动处理)'
            base = self.now_off()
            write_serial(self.dev, cmd + '\r')
            if not self._wait(base, end_marker, timeout):
                return False, '等待板子响应超时'
            return True, self._since(base)
        finally:
            self.probing = False

    def run_probe(self):
        """识别:hostname / 板子序列号(device-tree serial-number) / IP / sshd 状态。"""
        ok, raw = self._run_cmd(PROBE_CMD, PROBE_END_RE, 30)
        if not ok:
            return {'ok': False, 'error': raw}
        info = parse_probe(raw)
        info['ok'] = True
        info['ts'] = time.time()
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
        self.ssh_seq += 1
        sid = 'ssh%d' % self.ssh_seq
        s = SshSession(sid, ip, self.label, self.stable_dir())
        self.ssh_sessions[sid] = s
        s.ensure()
        return s, ''

    def run_ssh_enable(self):
        """一键开 SSH:装了就直接启,没装先装(默认 root/bianbu 登录后执行)。"""
        ok, raw = self._run_cmd(SSH_CMD, SSH_END_RE, 240)
        if not ok:
            return {'ok': False, 'error': raw}
        m = None
        for m in re.finditer(r'__SD_SSH__\d+=(\S+)', raw or ''):
            pass
        state = m.group(1).strip() if m else 'unknown'
        good = state in ('active', 'activating')
        self._say('一键开 SSH: sshd=%s' % state)
        return {'ok': good, 'sshd': state,
                'error': '' if good else 'sshd 未能进入 active(可能装包失败或没有 apt 源)'}

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
    """设备发现 + 每设备一个采集线程。"""

    def __init__(self):
        self.devices = {}
        self.lock = threading.Lock()

    def scan(self):
        """按 by-path 枚举,与设备名无关 —— 这是"插拔乱序也能认出来"的根本。"""
        found = {}
        try:
            for name in sorted(os.listdir(BY_PATH)):
                try:
                    found[name] = os.path.realpath(os.path.join(BY_PATH, name))
                except OSError:
                    continue
        except OSError:
            pass
        with self.lock:
            for key, dev in found.items():
                if key not in self.devices:
                    d = Device(key, dev)
                    self.devices[key] = d
                    threading.Thread(target=self._capture, args=(d,), daemon=True).start()
                else:
                    self.devices[key].dev = dev          # 设备名可能变了
        return found

    def _capture(self, d):
        """单读者采集线程 —— 铁律 1。设备消失则线程结束,下轮 scan 会重建。"""
        first = True
        while True:
            if d.fd is None:
                try:
                    d.fd = open_serial(d.dev, flush=first)
                    first = False
                    d.fail_count = 0
                    d.append('\r\n*** seriald 已接入 %s ***\r\n' % d.dev)
                except OSError as e:
                    # 退避重试;并只在首次失败及之后每 60s 记一条 ——
                    # 否则一个坏口(如 Errno 5 那个)会每 5 秒往日志里灌一行,一天 1.7 万行
                    d.fail_count += 1
                    wait = min(5 * (2 ** min(d.fail_count - 1, 4)), 60)
                    if d.fail_count == 1 or time.time() - d.fail_logged > 60:
                        d.fail_logged = time.time()
                        d.append('\r\n*** 打不开 %s: %s (退避 %ds 重试,累计失败 %d 次) ***\r\n'
                                 % (d.dev, e, wait, d.fail_count))
                    # 连续失败到阈值就尝试自愈。
                    # 实测结论(2026-09-19,3-4.2.2):只关 autosuspend 无效 —— 设备已从
                    # suspended 唤醒到 active,口依然 EIO;真正救活它的是软拔插。
                    # 所以 soft 只是"最轻的先试一下",失败后很快(不是 20 分钟后)就升级到 rebind。
                    if (d.fail_count in (3, 6) or d.fail_count % 20 == 0) \
                            and time.time() - d.last_rescue > 120:
                        d.last_rescue = time.time()
                        rc_mode = 'soft' if d.fail_count == 3 else 'rebind'
                        ok, note = try_rescue(d.dev, rc_mode)
                        d.rescue_note = '自愈(%s)%s: %s' % (rc_mode, '成功' if ok else '未生效', note)
                        d.append('\r\n*** %s ***\r\n' % d.rescue_note)
                    time.sleep(wait)
                    continue
            try:
                r, _, _ = select.select([d.fd], [], [], 1.0)
                if not r:
                    continue
                try:
                    data = os.read(d.fd, 65536)
                except (BlockingIOError, InterruptedError):
                    continue
                if data:
                    d.append(data)
            except OSError:
                try:
                    os.close(d.fd)
                except OSError:
                    pass
                d.fd = None
                d.append('\r\n*** %s 读取中断 ***\r\n' % d.dev)

    def probe_all(self):
        """对"还没有 hostname 且已安静"的设备发一个回车,让 getty 重画提示符。

        这是本服务唯一会往板子控制台写东西的地方,默认自动执行(用户已确认)。
        仅对空闲线路生效,避免打断正在输出的板子。
        """
        now = time.time()
        with self.lock:
            devs = list(self.devices.values())
        for d in devs:
            if d.hostname or d.fd is None:
                continue
            if now - d.started < 20:            # 刚接入先等等,别看走眼
                continue
            if now - d.last_rx < 15:            # 线路还在动,别插嘴
                continue
            if d.probed >= 5 or now - d.last_probe < 60:
                continue
            d.last_probe = now
            d.probed += 1
            # 交替发 CR / LF:Linux getty 认 CR,但某些板子的 shell 只认 LF(实测
            # k1 那块只发 \r 完全无反应),交替两次就能覆盖两种情况。
            ch = '\r' if d.probed % 2 == 1 else '\n'
            write_serial(d.dev, ch)
            d.append('\r\n*** seriald 探测 #%d:已发送 %s ***\r\n'
                     % (d.probed, 'CR' if ch == '\r' else 'LF'))

    def loop(self):
        while True:
            try:
                self.scan()
                self.probe_all()
                with self.lock:
                    for d in self.devices.values():
                        d.link_label()       # 编号变了就刷新软链(已正确则直接返回)
            except Exception:
                pass
            time.sleep(3)


MGR = Manager()


class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = 'seriald/1.0'

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _dev(self, q):
        key = (q.get('key') or [''])[0]
        with MGR.lock:
            return MGR.devices.get(key)

    def do_GET(self):
        u = urlparse(self.path)
        route, q = u.path, parse_qs(u.query)

        if route == '/api/devices':
            with MGR.lock:
                devs = [d.snapshot() for d in MGR.devices.values()]
            devs.sort(key=lambda x: x['key'])
            return self._json(200, {'devices': devs, 'by_path': BY_PATH,
                                    'server_time': time.time()})

        if route == '/api/serial':
            d = self._dev(q)
            if not d:
                return self._json(404, {'error': 'no such device'})
            off = int((q.get('off') or ['0'])[0] or 0)
            new_off, data, alive = d.read(off)
            return self._json(200, {'key': d.key, 'dev': d.dev, 'off': new_off,
                                    'alive': alive, 'data': data.decode('utf-8', 'replace')})

        if route == '/api/session':
            d = self._dev(q)
            if not d:
                return self._json(404, {'error': 'no such device'})
            sid = (q.get('sid') or ['serial'])[0]
            off = int((q.get('off') or ['0'])[0] or 0)
            if sid == 'serial':
                new_off, data, alive = d.read(off)
                return self._json(200, {'sid': 'serial', 'dev': d.dev, 'off': new_off,
                                        'alive': alive, 'data': data.decode('utf-8', 'replace')})
            s = d.ssh_sessions.get(sid)
            if not s:
                return self._json(404, {'error': 'no such session'})
            new_off, data, alive = s.read(off)
            return self._json(200, {'sid': sid, 'ip': s.ip, 'off': new_off,
                                    'alive': alive, 'data': data.decode('utf-8', 'replace')})

        if route == '/api/sessions':
            d = self._dev(q)
            if not d:
                return self._json(404, {'error': 'no such device'})
            out = [{'sid': 'serial', 'kind': 'serial', 'dev': d.dev,
                    'alive': d.fd is not None}]
            out += [s.snapshot() for s in d.ssh_sessions.values()]
            return self._json(200, {'sessions': out, 'label': d.label})

        if route == '/api/logs':
            d = self._dev(q)
            if not d:
                return self._json(404, {'error': 'no such device'})
            d.link_label()
            real = d.stable_dir()
            out = []
            for fn in sorted(os.listdir(real), reverse=True):
                p = os.path.join(real, fn)
                try:
                    out.append({'name': fn, 'size': os.path.getsize(p),
                                'path': 'logs/%s/%s' % (os.path.basename(real), fn)})
                except OSError:
                    pass
            return self._json(200, {'label': d.label, 'dir': os.path.basename(real),
                                    'alias': d.label, 'files': out})

        if route == '/':
            return self._page()
        return super().do_GET()

    def do_POST(self):
        route = urlparse(self.path).path
        try:
            n = int(self.headers.get('Content-Length') or 0)
            payload = json.loads(self.rfile.read(n).decode('utf-8')) if n else {}
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {'error': 'bad json'})
        q = {'key': [str(payload.get('key') or '')]}
        d = self._dev(q)
        if not d:
            return self._json(404, {'error': 'no such device'})

        if route == '/api/send':
            text = str(payload.get('data') or '')
            if not text:
                return self._json(400, {'error': 'data required'})
            ok, err = write_serial(d.dev, text)
            return self._json(200 if ok else 503, {'ok': ok, 'error': err, 'dev': d.dev})

        if route == '/api/probe':
            d.last_probe = time.time()
            d.probed += 1
            ok, err = write_serial(d.dev, '\r')
            d.append('\r\n*** 手动探测:已发送回车 ***\r\n')
            return self._json(200, {'ok': ok, 'error': err})

        if route == '/api/rescue':
            # 手动自愈。mode: soft=只关 autosuspend(最轻), rebind=追加软拔插。
            # 分两步是为了能区分病因,见 rescue.sh 注释。
            mode = str(payload.get('mode') or 'soft')
            if mode not in ('soft', 'rebind'):
                return self._json(400, {'error': 'mode must be soft|rebind'})
            d.last_rescue = time.time()
            ok, note = try_rescue(d.dev, mode)
            d.rescue_note = note
            d.fail_count = 0               # 让它立刻重试
            d.append('\r\n*** 手动自愈(%s)%s: %s ***\r\n'
                     % (mode, '成功' if ok else '未生效', note))
            hint = ''
            if not ok:
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
            return self._json(200, {'ok': ok, 'note': note, 'hint': hint, 'mode': mode})

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
                ok, err = write_serial(d.dev, text)
            else:
                s = d.ssh_sessions.get(sid)
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
            s = d.ssh_sessions.get(sid)
            if not s:
                return self._json(404, {'error': 'no such session'})
            return self._json(200, {'ok': s.resize(cols, rows), 'cols': s.cols, 'rows': s.rows})

        if route == '/api/session-close':
            sid = str(payload.get('sid') or '')
            s = d.ssh_sessions.pop(sid, None)
            if not s:
                return self._json(404, {'error': 'no such session'})
            s.close()
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
        path = os.path.join(BASE, 'index.html')
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


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8898
    bind = sys.argv[2] if len(sys.argv) > 2 else '10.0.90.177'
    os.makedirs(LOG_ROOT, exist_ok=True)
    threading.Thread(target=MGR.loop, daemon=True).start()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    handler = functools.partial(Handler, directory=BASE)
    with socketserver.ThreadingTCPServer((bind, port), handler) as httpd:
        print('seriald on http://%s:%d/  (by-path=%s)' % (bind, port, BY_PATH), flush=True)
        httpd.serve_forever()


if __name__ == '__main__':
    main()
