"""Device discovery, collector ownership and persistent identity binding."""
import errno
import os
import select
import stat
import sys
import termios
import threading
import time

from .archive import finish_staged, stage_logs
from .config import BY_PATH, LOG_ROOT
from .device import Device
from .identity import eeprom_serial, hardware_serial, select_identity
from .storage import IdentityError, archive_component
from .transport import open_serial
from .boot_marker import marker_serial

class Manager:
    """设备发现 + 每个实际 tty 一个采集线程;旧线程退出后才转移所有权。"""

    def __init__(self, store=None):
        self.store = store
        self.devices = {}                 # physical by-path -> current connection
        self.boards = {}                  # immutable serial -> known board / last connection
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.stopping = threading.Event()
        if store:
            for record in store.boards():
                self.boards[record['serial']] = Device('', '', self, record)

    def all_devices(self):
        with self.lock:
            known = list(self.boards.values())
            pending = [d for d in self.devices.values() if not d.serial and d.present]
            return known + pending

    def resolve(self, key):
        with self.lock:
            return next((d for d in self.all_devices() if d.key == key), None)

    def identify(self, device, info):
        with self.lock:
            provisioned = eeprom_serial(info)
            marker_record = self.store.get(marker_serial(info))
            known_marker = bool(marker_record and marker_record['info'].get('serial_source') == 'boot-marker')
            # A copied /boot must not redirect an already known hardware identity into another archive.
            if known_marker:
                for candidate in (str(info.get('serial') or '').strip(), hardware_serial(info)):
                    if (candidate != marker_record['serial'] and not self.store.has_conflict(candidate)
                            and self.store.get(candidate)):
                        raise IdentityError('/boot 标记与已有硬件档案冲突，可能复制了其他板子的标记文件')
            selected = select_identity(info,
                conflicted=self.store.has_conflict(str(info.get('serial') or '').strip()),
                known_soc=self.store.get(hardware_serial(info)) is not None,
                known_marker=known_marker,
                soc_conflicted=self.store.has_conflict(hardware_serial(info)),
                eeprom_conflicted=self.store.has_conflict(provisioned))
            serial = selected['serial']
            if provisioned:
                aliases = (str(info.get('serial') or '').strip(), hardware_serial(info), marker_serial(info))
                for alias in aliases:
                    alias_record = self.store.get(alias) if alias and alias != serial else None
                    if (alias_record and alias_record['info'].get('identity_status') == 'provisional'
                            and not self.store.get(serial)):
                        raise IdentityError('已发现正式 EEPROM 编号 %s，但该板仍有候选档案 %s；'
                                            '请停服务后用 correct-serial 迁移' % (serial, alias))
            previous = self.boards.get(serial)
            if previous is not None and previous is not device and previous.present:
                self.store.mark_conflict(serial)
                raise IdentityError('同一序列号已在另一个串口在线，拒绝合并设备')
            if self.store.has_conflict(serial) and device.serial != serial:
                raise IdentityError('该序列号已确认不唯一，需核实真实板载唯一编号后再绑定')
            if device.serial and device.serial != serial:
                raise IdentityError('当前连接的序列号改变，请重新连接后识别')
            previous_record = self.store.get(serial)
            with device.log_lock:
                pending_dir = device.stable_dir()
                device.close_log()
                target = os.path.join(LOG_ROOT, archive_component(serial, selected.get('serial_source', '')))
                previous_dir = (os.path.join(LOG_ROOT, archive_component(
                    serial, previous_record['info'].get('serial_source', '')))
                    if previous_record else '')
                sources = list(dict.fromkeys(path for path in (previous_dir, pending_dir)
                                             if path and path != target))
                staged = []
                try:
                    if previous_dir and previous_dir != target:
                        staged += stage_logs(previous_dir, target)
                    if pending_dir != target and pending_dir != previous_dir:
                        staged += stage_logs(pending_dir, target,
                                             'serial-' + device.connection_id + '-')
                    record = self.store.bind(selected, device.by_path, device.dev)
                except Exception:
                    # Staged copies are safe to reuse on retry; source files remain authoritative.
                    raise
                device.record = record
                finish_staged(staged, sources)
            if previous is not None and previous is not device:
                with previous.sessions_lock, device.sessions_lock:
                    device.ssh_sessions.update(previous.ssh_sessions)
                    previous.ssh_sessions.clear()
            self.boards[serial] = device
            info.update(selected)
            self.store.event(serial, 'service', 'identified', device.by_path)

    def rename(self, device, label):
        if not device.serial:
            raise IdentityError('先识别开发板序列号，再设置标签')
        with self.lock:
            device.record = self.store.rename(device.serial, label)
            return device.key

    @staticmethod
    def _matches(d):
        try:
            s = os.stat(os.path.join(BY_PATH, d.by_path))
            return (s.st_dev, s.st_ino, s.st_rdev) == d.identity
        except OSError:
            return False

    def scan(self):
        with self.scan_lock:
            if self.stopping.is_set():
                return {}
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
                if d is not None and (d.stop.is_set() or d.identity != identity):
                    if d.thread and d.thread.is_alive():
                        continue
                    if not d.operation_lock.acquire(blocking=False):
                        continue
                    d.operation_lock.release()
                    d.present = False
                    d = None
                if d is None:
                    d = Device(key, dev, self if self.store else None)
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
                            d.fd = open_serial(os.path.join(BY_PATH, d.by_path),
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
            d.present = False
            d.close_log()
            if self.store and d.serial:
                d.record['last_seen'] = self.store.touch(d.serial)

    def close(self):
        self.stopping.set()
        # Wait for an in-flight scan before taking the final set of collectors.
        with self.scan_lock, self.lock:
            devices = list({id(d): d for d in list(self.devices.values()) + list(self.boards.values())}.values())
        for d in devices:
            d.stop.set()
        for d in devices:
            if d.thread:
                d.thread.join(timeout=6)
            for s in list(d.ssh_sessions):
                d.close_session(s)
            d.close_log()

    def loop(self):
        while not self.stopping.is_set():
            try:
                self.scan()
                for d in self.all_devices():
                    d.reap_sessions()
                    if self.store and d.serial and d.present:
                        d.record['last_seen'] = self.store.touch(d.serial)
                # 启动/重连只被动采集;自动回车或 USB 软拔插会干扰正在使用的控制台。
            except Exception as e:
                print('seriald scan: %s' % e, file=sys.stderr, flush=True)
            self.stopping.wait(3)
