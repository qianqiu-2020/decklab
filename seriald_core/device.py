"""One board connection: console stream, probe transactions and SSH sessions."""
import codecs
import hashlib
import os
import re
import secrets
import threading
import time

from .config import ANSI, BY_PATH, DATE_FMT, HOST_PATTERNS, LOG_ROOT, SCROLLBACK, SSH_IDLE, SSH_LIMIT
from .transport import write_serial
from .protocol import PROBE_CMD, RE_LOGIN, RE_PASSWD, RE_PROMPT, SSH_CMD, parse_probe
from .sessions import SshSession
from .storage import IdentityError, archive_component
from .identity import eeprom_serial, usable_serial
from .boot_marker import init_command

class Device:
    """One physical connection, optionally bound to a persistent board serial."""
    def __init__(self, key, dev, manager=None, record=None):
        self.manager = manager
        self.record = record
        self.connection_id = secrets.token_hex(12)
        self.by_path = key                    # physical attachment, not board identity
        self.temporary_id = hashlib.sha256(key.encode()).hexdigest()[:16] if key else self.connection_id[:16]
        self.dev = dev                    # 当前 /dev/ttyUSBx(会变)
        self.hostname = record['info'].get('hostname', '') if record else ''
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
        self.identity_error = ''
        self.reported_serial = ''         # unbound probe result, never a persistence key
        self.auto_identify = False
        self.auto_identify_started = False
        self.auto_identify_until = 0.0
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
        self.probe_info = dict(record['info']) if record else {}
        self.ssh_sessions = {}            # sid -> SshSession(与串口并存的独立会话)
        self.ssh_pending = 0
        self.logf = None
        # preload 可能在没有日志时早退;所有状态必须先初始化。
        self.logpath = None
        self.dead = False
        self.history_loaded = record is None

    def preload(self):
        """启动时把最近一份串口日志的尾部装回内存缓冲。

        历史只供页面回看;带时间戳和结束标记的旧日志不能代表现场登录状态。
        识别时先从新收到的 console_tail 确认,必要时请求板子重画提示符。
        """
        try:
            d = self.stable_dir()
            files = sorted(f for f in os.listdir(d)
                           if f.startswith('serial-') and f.endswith('.log'))
            files.sort(key=lambda name: os.path.getmtime(os.path.join(d, name)))
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
    def serial(self):
        return self.record['serial'] if self.record else ''

    @property
    def key(self):
        return self.record['label'] if self.record else 'unprovisioned-' + self.temporary_id

    @property
    def label(self):
        return self.key

    def stable_dir(self):
        if self.serial:
            source = self.record['info'].get('serial_source', '')
            directory = archive_component(self.serial, source)
        else:
            directory = os.path.join('_unprovisioned', self.temporary_id, self.connection_id)
        path = os.path.join(LOG_ROOT, directory)
        os.makedirs(path, exist_ok=True)
        return path

    def detach_identity(self):
        """Start a new logical connection on the same UART before a physical board swap."""
        self.record = None
        self.connection_id = secrets.token_hex(12)
        self.hostname = ''
        self.probe_info = {}
        self.reported_serial = ''
        self.identity_error = '已进入换板识别模式；新板出现登录提示后自动识别一次'
        self.auto_identify_started = False
        self.started = time.time()
        self.bytes = 0
        self.last_rx = 0.0
        self.console_tail = ''
        self.history_loaded = True
        self.logpath = None
        with self.cv:
            self.base += len(self.buf)
            self.buf.clear()
            self.cv.notify_all()

    def log_files(self):
        root = self.stable_dir()
        files = []
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if not name.endswith('.log') or not os.path.isfile(path) or os.path.islink(path):
                continue
            stamp = re.search(r'(\d{8})\.log$', name)
            date = stamp.group(1) if stamp else ''
            stat_result = os.stat(path)
            files.append({'name': name, 'size': stat_result.st_size,
                          'mtime': stat_result.st_mtime,
                          'date': ('%s-%s-%s' % (date[:4], date[4:6], date[6:]) if date else ''),
                          'kind': 'ssh' if name.startswith('ssh-') else 'serial'})
        # Filename prefixes mix serial/SSH logs, so lexicographic order is not time order.
        files.sort(key=lambda item: (item['date'], item['mtime'], item['name']), reverse=True)
        return files

    def open_log(self):
        path = os.path.join(self.stable_dir(), 'serial-%s.log' % time.strftime(DATE_FMT))
        if self.logf and self.logpath == path:
            return
        if self.logf:
            self.logf.close()
            self.logf = None
        self.logf = open(path, 'a', encoding='utf-8', buffering=1)
        self.logpath = path

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
                    self._maybe_auto_identify(self.console_tail)
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
            return write_serial(os.path.join(BY_PATH, self.by_path), text,
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

    def _maybe_auto_identify(self, text):
        """Run one armed probe after the replacement board reaches login or a shell."""
        if (not self.auto_identify or self.auto_identify_started or self.serial
                or not self.manager or self.stop.is_set()):
            return
        if time.monotonic() > self.auto_identify_until:
            self.auto_identify = False
            self.manager.disarm(self.by_path)
            return
        clean = ANSI.sub('', text)
        if not (re.search(RE_PROMPT, clean, re.M) or re.search(RE_LOGIN, clean, re.I)):
            return
        self.auto_identify_started = True

        def identify_once():
            try:
                result = self.run_probe()
                if not result.get('ok') and not self.stop.is_set():
                    self.identity_error = '自动识别失败：' + str(result.get('error') or '未知错误')
            finally:
                self.auto_identify = False
                self.manager.disarm(self.by_path)

        threading.Thread(target=identify_once, daemon=True).start()

    def read(self, off, wait=15.0):
        if not self.history_loaded:
            with self.log_lock:
                if not self.history_loaded:
                    self.preload()
                    self.history_loaded = True
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
        self._say('停在登录提示符,使用配置的账户自动登录')
        base = self.now_off()
        if not self._write(os.environ.get('SERIALD_SSH_USER', 'root') + self.newline)[0]:
            return False, base
        failure = r'(?:[Ll]ogin incorrect|登录错误|认证失败|Authentication failure)'
        match = self._wait(base, RE_PASSWD + r'\s*$|' + RE_PROMPT + '|' + failure, 12)
        if not match or re.search(failure, match.group(0)):
            return False, base
        if re.search(RE_PROMPT, match.group(0)):
            return True, self.now_off()       # 免密码登录不能继续盲发默认密码
        base = self.now_off()
        if not self._write(os.environ.get('SERIALD_SSH_PASSWORD', 'bianbu') + self.newline)[0]:
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

    def run_probe(self, init_boot_marker=False):
        """Read identity metadata; create a /boot marker only on explicit request."""
        if init_boot_marker and self.serial and self.probe_info.get('serial_source') != 'boot-marker':
            return {'ok': False, 'error': '设备已有身份，无需创建 /boot 软件标记'}
        origin = self.stop
        cmd = (init_command() + ' && { ' + PROBE_CMD + '; }') if init_boot_marker else PROBE_CMD
        ok, raw = self._run_cmd(cmd, timeout=30)
        if not ok:
            return {'ok': False, 'error': raw}
        error = re.search(r'^BOOT_MARKER_ERROR=(.*)$', raw, re.M)
        if error:
            return {'ok': False, 'error': '/boot 标记初始化失败: ' + error.group(1)}
        try:
            info = parse_probe(raw)
        except ValueError as e:
            return {'ok': False, 'error': str(e)}
        info['ok'] = True
        info['ts'] = time.time()
        info['needs_provisioning'] = not bool(eeprom_serial(info) or usable_serial(info.get('serial')))
        with self.operation_lock:
            if self.stop is not origin or origin.is_set():
                return {'ok': False, 'error': '识别期间设备已重新枚举'}
            if self.manager:
                try:
                    self.manager.identify(self, info)
                except (IdentityError, OSError) as error:
                    self.identity_error = str(error)
                    self.reported_serial = info.get('serial', '')
                    return {'ok': False, 'error': str(error)}
            self.identity_error = ''
            self.reported_serial = info.get('reported_serial', info.get('serial', ''))
            self.probe_info = info
            # Identity and storage follow the confirmed serial; hostname is display metadata.
            if info.get('serial') and not self.hostname:
                self.hostname = info['serial']
            self._say('识别结果: 序列号=%s 主机名=%s IP=%s sshd=%s'
                      % (info.get('serial') or '-', info.get('hostname') or '-',
                         ','.join(info.get('ips') or []) or '-', info.get('sshd') or '-'))
        return info

    def ssh_connect(self, actor='web', title=''):
        """一键 SSH:用识别到的 IP 起一条**独立会话**(默认 root/bianbu)。

        没有 IP 就先识别一次 —— 这正是"识别设备"存在的意义:串口负责拿到
        IP/序列号/sshd 状态,SSH 会话负责后续高效操作。两者并存、各记各的日志。
        """
        origin = self.stop
        info = self.probe_info
        if not info.get('ips') or (self.manager and not self.serial):
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
            sid = 'ssh' + secrets.token_hex(12)
            self.ssh_pending += 1
        session = None
        try:
            if self.stop is not origin or origin.is_set():
                return None, '设备已重新枚举,请重新识别 IP'
            session = SshSession(sid, ip, self.label, self.stable_dir())
            session.actor, session.title = actor, title
            if self.manager and self.serial:
                session.store = self.manager.store
                session.store.open_session(sid, self.serial, actor, title, ip)
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
        return {'key': self.key, 'serial': self.serial, 'connection_id': self.connection_id, 'dev': self.dev if self.present else '',
                'by_path': self.by_path if self.present else '',
                'last_dev': self.record['last_dev'] if self.record else self.dev,
                'last_path': self.record['last_path'] if self.record else self.by_path,
                'last_seen': self.record['last_seen'] if self.record else self.last_rx,
                'state': ('online' if self.fd is not None and self.present else 'offline') if self.serial else 'pending',
                'label': self.label,
                'hostname': self.hostname, 'alive': self.fd is not None and self.present,
                'bytes': self.bytes, 'last_rx': self.last_rx,
                'last_line': tail, 'log': os.path.basename(self.logpath or ''),
                'probes': self.probed, 'fail_count': self.fail_count,
                'rescue_note': self.rescue_note, 'busy': self.probing,
                'identity_error': self.identity_error, 'reported_serial': self.reported_serial,
                'needs_provisioning': bool(self.probe_info.get('needs_provisioning') or not self.serial),
                'info': self.probe_info}
