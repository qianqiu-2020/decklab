"""One board connection: console stream, probe transactions and SSH sessions."""
from dataclasses import asdict
import hashlib
import os
import re
import secrets
import threading
import time
import sys

from .config import ANSI, BY_PATH, HOST_PATTERNS, LOG_ROOT, SCROLLBACK, SSH_IDLE, SSH_LIMIT
from .transport import write_serial
from .console_protocol import ConsoleProtocol
from .terminal_state import SerialTerminalState
from .serial_log import SerialLog
from .protocol import PROBE_CMD, RE_LOGIN, RE_PROMPT, SSH_CMD, parse_probe
if sys.platform == 'win32':
    from serial import SerialException
    from .sessions_windows import SshSession, check_password_login
else:
    from .sessions import SshSession, check_password_login
from .storage import IdentityError
from .identity import eeprom_serial, usable_serial
from .boot_marker import init_command
from .log_archive import boot_epochs
from .file_transfer import SerialFileTransfer
from .profiles import ProfileBook
from .models import Board, SessionRegistry
from .transactions import SerialTransactions, TransactionError


class Device:
    """One physical connection, optionally bound to a persistent board serial."""
    def __init__(self, key, dev, manager=None, record=None, board=None):
        self.manager = manager
        self.console = ConsoleProtocol(self)
        self.log = SerialLog(self.stable_dir, opener=lambda: self.open_log())
        self.board = board if board is not None else Board(record) if record else None
        self._sessions = self.board.sessions if self.board else SessionRegistry()
        record = self.record
        self.connection_id = secrets.token_hex(12)
        self.by_path = key                    # physical attachment, not board identity
        self.temporary_id = hashlib.sha256(key.encode()).hexdigest()[:16] if key else self.connection_id[:16]
        self.dev = dev                    # 当前 /dev/ttyUSBx(会变)
        self.hostname = record['info'].get('hostname', '') if record else ''
        self.cv = threading.Condition()
        self.buf = bytearray()
        self.base = 0
        self.fd = None
        self.serial_settings = None
        self.bytes = 0
        self.last_rx = 0.0
        self.last_health_check = 0.0   # host-side UART check; does not probe the board
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
        self.transactions = SerialTransactions(self, self.operation_lock)
        self.file_transfer = SerialFileTransfer(self)
        self.write_lock = threading.Lock()      # fd 退役与写入串行,绝不占用输出 cv
        self.boot_index_lock = threading.Lock()
        self.boot_index_cache = {}
        self.stop = threading.Event()
        self.capture_paused = threading.Event()
        self.thread = None
        self.identity = None
        self.present = False
        self.console_tail = ''
        self.terminal = SerialTerminalState(self)
        self.newline = None            # 收到新提示符后才确认本连接的 CR/LF 策略
        self.probe_info = dict(record['info']) if record else {}
        # preload 可能在没有日志时早退;所有状态必须先初始化。
        self.dead = False
        self.history_loaded = record is None
        self.input_lines = {}
        self.input_sensitive = set()

    @property
    def log_lock(self):
        return self.log.log_lock

    @property
    def logf(self):
        return self.log.logf

    @logf.setter
    def logf(self, value):
        self.log.logf = value

    @property
    def logpath(self):
        return self.log.logpath

    @logpath.setter
    def logpath(self, value):
        self.log.logpath = value

    @property
    def log_error(self):
        return self.log.log_error

    @log_error.setter
    def log_error(self, value):
        self.log.log_error = value

    @property
    def record(self):
        return self.board.record if self.board else None

    @record.setter
    def record(self, record):
        if record is None:
            if self.board:
                self.board.detach(self)
            self.board = None
            self._sessions = SessionRegistry()
        elif self.board:
            self.board.update(record)
        else:
            self.bind_board(Board(record))

    def bind_board(self, board):
        if self.board is not None and self.board.serial != board.serial:
            raise IdentityError('Detach the current board before binding a different serial')
        self._sessions.transfer_to(board.sessions)
        self.board = board
        self._sessions = board.sessions

    @property
    def sessions_lock(self):
        return self._sessions.lock

    @sessions_lock.setter
    def sessions_lock(self, lock):
        self._sessions.lock = lock

    @property
    def ssh_sessions(self):
        return self._sessions.sessions

    @ssh_sessions.setter
    def ssh_sessions(self, sessions):
        self._sessions.sessions = sessions

    @property
    def ssh_pending(self):
        return self._sessions.pending

    @ssh_pending.setter
    def ssh_pending(self, pending):
        self._sessions.pending = pending

    @property
    def profile(self):
        book = getattr(self.manager, 'profiles', None)
        if not isinstance(book, ProfileBook):
            book = ProfileBook.from_env()
        return book.resolve(self.serial, self.by_path)

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
            # Archived lines are written as text with LF-only separators.  The
            # live serial terminal deliberately keeps raw line endings, so
            # normalize only this historical snapshot before mixing it into
            # the same byte buffer; otherwise xterm renders every line after
            # the first one with an increasing horizontal offset.
            tail = tail.replace(b'\r\n', b'\n').replace(b'\r', b'\n').replace(b'\n', b'\r\n')
            self.buf += ('\r\n--- 以下为历史串口日志(%s) ---\r\n' % files[-1]).encode()
            self.buf += tail
            self.buf += '\r\n--- 历史结束 ---\r\n'.encode()
            # 历史装饰文字不能参与提示符判断;只用新收到的 console_tail 确认现场。
        except OSError:
            pass

    @property
    def serial(self):
        return self.board.serial if self.board else ''

    @property
    def key(self):
        return self.record['label'] if self.record else 'unprovisioned-' + self.temporary_id

    @property
    def label(self):
        return self.key

    def stable_dir(self):
        if self.serial:
            directory = self.board.archive_path
        else:
            directory = os.path.join('_unprovisioned', self.temporary_id, self.connection_id)
        path = os.path.join(LOG_ROOT, directory)
        os.makedirs(path, exist_ok=True)
        return path

    def detach_identity(self):
        """Start a new logical connection on the same UART before a physical board swap."""
        self.record = None
        self.newline = self.profile.line_ending
        self.connection_id = secrets.token_hex(12)
        self.hostname = ''
        self.probe_info = {}
        self.reported_serial = ''
        self.identity_error = '已进入换板识别模式；新板出现登录提示后自动识别一次'
        self.auto_identify_started = False
        self.started = time.time()
        self.bytes = 0
        self.last_rx = 0.0
        self.last_health_check = 0.0
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

    def boot_epochs(self, files=None):
        with self.boot_index_lock:
            selected = files if files is not None else self.log_files()
            result = boot_epochs(self.stable_dir(), selected, cache=self.boot_index_cache)
            names = {entry['name'] for entry in selected if entry.get('kind') == 'serial'}
            for name in set(self.boot_index_cache) - names:
                del self.boot_index_cache[name]
            return result

    def _output_tail(self, sid):
        if sid == 'serial':
            return self.console_tail
        session = self.get_session(sid)
        if not session:
            return ''
        with session.cv:
            return bytes(session.buf[-2048:]).decode('utf-8', 'replace')

    def record_input(self, sid, actor, text):
        """Reconstruct submitted command lines while refusing password prompts."""
        if not self.serial or not self.manager or not text:
            return
        key = (sid, actor)
        tail = ANSI.sub('', self._output_tail(sid))
        if re.search(r'(?:password|passphrase|密码)[^\n\r:：]{0,80}[:：]\s*$', tail, re.I):
            self.input_sensitive.add(key)
        line = self.input_lines.get(key, '')
        # Remove cursor/function-key CSI sequences.  Their editing semantics are
        # ambiguous, so they must never become fake reusable commands.
        text = re.sub(r'\x1b\[[0-9;?]*[ -/]*[@-~]', '', text)
        for char in text:
            if char in '\r\n':
                command = line.strip()
                contains_secret = re.search(
                    r'(?i)(?:password|passwd|passphrase|token|secret|api[_-]?key)\s*=', line)
                if (command and not line.startswith(' ') and not contains_secret
                        and key not in self.input_sensitive):
                    self.manager.store.record_command(self.serial, command)
                line = ''
                self.input_sensitive.discard(key)
            elif char in ('\x08', '\x7f'):
                line = line[:-1]
            elif char == '\x03':
                line = ''
                self.input_sensitive.discard(key)
            elif char == '\t' or ord(char) >= 32:
                if len(line) < 2048:
                    line += char
        self.input_lines[key] = line

    def open_log(self):
        self.log.open()

    def append(self, data, received=True):
        b = data if isinstance(data, bytes) else data.encode('utf-8', 'replace')
        with self.log_lock:
            text = self.log.consume(b, received)
            if not received:
                # Local status belongs only in archives, never in the UART bytes.
                return
            with self.cv:
                self.terminal.observe(b)
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
        self.log.close()

    def _write(self, text):
        with self.cv:
            self.terminal.input_pending = True
        with self.write_lock:
            lease = self.transactions.current
            if lease:
                lease.check()
            if self.stop.is_set() or not self.present or self.fd is None:
                return False, '设备未打开或已重新枚举'
            if sys.platform == 'win32':
                try:
                    encoded = text.encode('utf-8', 'replace')
                    if self.fd.write(encoded) != len(encoded):
                        return False, 'COM 写入不完整'
                    return True, ''
                except (OSError, SerialException) as error:
                    return False, str(error)
            return write_serial(os.path.join(BY_PATH, self.by_path), text,
                                identity=self.identity, cancel=lease or self.stop)

    def send(self, text):
        # probing 布尔值的检查/赋值并不原子;所有写入口共用事务锁才不会串话。
        if self.file_transfer.active:
            return False, '串口文件传输中，请完成或取消后再输入'
        try:
            with self.transactions.begin('input', 5) as lease:
                with self.cv:
                    self.terminal.input_pending = True
                    self.terminal.authority = (lease.actor, lease.client_id)
                return self._write(text)
        except TransactionError as error:
            return False, str(error)

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
        if (self.profile.probe != 'linux' or not self.auto_identify or self.auto_identify_started or self.serial
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
        return self.console.wait(base, pattern, timeout)

    def ensure_shell(self, base):
        return self.console.ensure_shell(base)

    def _run_cmd(self, cmd, timeout=30.0):
        return self.console.run_command(cmd, timeout)

    def _run_cmd_locked(self, cmd, timeout, ensure_shell=True):
        return self.console.run_locked(cmd, timeout, ensure_shell)

    def run_probe(self, init_boot_marker=False):
        """Read identity metadata; create a /boot marker only on explicit request."""
        if self.profile.probe != 'linux':
            return {'ok': False, 'error': '当前配置仅提供原始串口，未启用 Linux shell 探测'}
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

    def configure_root_ssh(self):
        return self.console.configure_root_ssh()

    def ssh_connect(self, actor='web', title='', ssh_user=None, ssh_password=None):
        """一键 SSH:用识别到的 IP 起一条**独立会话**(默认 root/bianbu)。

        没有 IP 就先识别一次 —— 这正是"识别设备"存在的意义:串口负责拿到
        IP/序列号/sshd 状态,SSH 会话负责后续高效操作。两者并存、各记各的日志。
        """
        origin = self.stop
        connection_id = self.connection_id
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
        registry = self._sessions
        # A named agent gets one long-lived shell per board.  Reusing the PTY
        # preserves shell state (cwd, exported variables, tmux, etc.) and lets
        # the human UI keep one visible tab for that agent.  The web UI uses
        # actor=web and therefore continues to create an independent session.
        persistent = bool(actor and actor != 'web')
        if persistent:
            with self.sessions_lock:
                for existing in self.ssh_sessions.values():
                    if (getattr(existing, 'actor', '') == actor
                            and not getattr(existing, 'closed', True)
                            and getattr(existing, 'fd', None) is not None):
                        if title:
                            existing.title = title
                        return existing, ''
        with registry.lock:
            if self._sessions is not registry:
                return None, '设备身份已变化，请重新建立 SSH 会话'
            if len(registry.sessions) + registry.pending >= SSH_LIMIT:
                return None, 'SSH 会话已达上限(%d),请先关闭闲置会话' % SSH_LIMIT
            sid = 'ssh' + secrets.token_hex(12)
            registry.pending += 1
        session = None
        try:
            if self.stop is not origin or origin.is_set() or self.connection_id != connection_id:
                return None, '设备已重新枚举,请重新识别 IP'
            profile = self.profile
            user = ssh_user if ssh_user is not None else profile.ssh_user
            password = (ssh_password if ssh_password is not None
                        else profile.ssh_password)
            auto_configure = (profile.auto_configure_ssh and user == 'root'
                              and profile.ssh_user == 'root' and bool(password)
                              and password == profile.ssh_password)
            if auto_configure:
                authenticated, denied, detail = check_password_login(ip, user, password)
                if denied:
                    if self.stop is not origin or origin.is_set() or self.connection_id != connection_id:
                        return None, '设备已更换，取消 SSH 自动配置'
                    self._say('SSH root 认证失败，尝试通过串口自动配置')
                    if self.manager and self.serial:
                        self.manager.store.event(self.serial, actor, 'ssh-auto-config',
                                                 'root 认证失败；开始串口配置')
                    configured, error = self.configure_root_ssh()
                    if self.manager and self.serial:
                        self.manager.store.event(self.serial, actor, 'ssh-auto-config',
                                                 '成功' if configured else '失败：' + error)
                    if not configured:
                        return None, error
                    authenticated, _, detail = check_password_login(ip, user, password)
                if not authenticated:
                    return None, detail
            session_args = {'user': user, 'password': password}
            session = SshSession(sid, ip, self.label, self.stable_dir(), **session_args)
            session.auto_configured = bool(auto_configure and denied)
            session.actor, session.title = actor, title
            session.persistent = persistent
            if self.manager and self.serial:
                session.store = self.manager.store
                session.store.open_session(sid, self.serial, actor, title, ip)
            if not session.ensure():
                session.close()
                return None, '建立 SSH 会话失败'
            with self.operation_lock:
                if (self.stop is not origin or origin.is_set() or self.connection_id != connection_id
                        or self._sessions is not registry):
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
            with registry.lock:
                registry.pending -= 1

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
                    # Persistent agent PTYs stay until an explicit close.  A
                    # dead persistent session is still removed immediately so
                    # the next agent call can establish a fresh one.
                    if not session.closed and (getattr(session, 'persistent', False)
                                               or not session.is_idle(now, SSH_IDLE)):
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
        profile = self.profile
        return {'operation': self.transactions.snapshot(), 'input_owner': self.transactions.ownership(),
                'profile': profile.name,
                'serial_settings': asdict(self.serial_settings or profile.serial),
                'settings_pending': self.serial_settings is not None and self.serial_settings != profile.serial,
                'key': self.key, 'serial': self.serial, 'connection_id': self.connection_id, 'dev': self.dev if self.present else '',
                'by_path': self.by_path if self.present else '',
                'last_dev': self.record['last_dev'] if self.record else self.dev,
                'last_path': self.record['last_path'] if self.record else self.by_path,
                'last_seen': self.record['last_seen'] if self.record else self.last_rx,
                'state': ('online' if self.fd is not None and self.present else 'offline') if self.serial else 'pending',
                'label': self.label,
                'hostname': self.hostname, 'alive': self.fd is not None and self.present,
                'bytes': self.bytes, 'last_rx': self.last_rx,
                'last_health_check': self.last_health_check, 'capture_started': self.started,
                'capture_paused': self.capture_paused.is_set(),
                'last_line': tail, 'log': os.path.basename(self.logpath or ''),
                'log_error': self.log_error,
                'probes': self.probed, 'fail_count': self.fail_count,
                'rescue_note': self.rescue_note, 'busy': self.probing,
                'identity_error': self.identity_error, 'reported_serial': self.reported_serial,
                'needs_provisioning': bool(self.probe_info.get('needs_provisioning') or not self.serial),
                'info': self.probe_info}
