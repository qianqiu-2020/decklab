"""Device discovery, collector ownership and persistent identity binding."""
import errno
from contextlib import ExitStack, contextmanager
import json
import os
import secrets
import select
import stat
import sys
try:
    import termios
except ImportError:
    termios = None
import threading
import time

from .archive import finish_staged, stage_logs
from .config import BY_PATH, LOG_ROOT
from .device import Device
from .identity import eeprom_serial, hardware_serial, select_identity
from .storage import (IdentityConfirmationRequired, IdentityError, archive_component,
                      validate_conflict_reason)
from .transport import open_serial
from .boot_marker import marker_serial
from .profiles import ProfileBook
from .models import Board
from .session_base import relocate_session_logs
from .maintenance import LogMaintenance, MaintenancePolicy
from .tasks import TaskManager, TaskError
from .transfer_artifacts import TransferArtifacts
from .hardware import HardwareBook

class Manager:
    """设备发现 + 每个实际 tty 一个采集线程;旧线程退出后才转移所有权。"""

    def __init__(self, store=None, handoff=None, profiles=None, artifact_dir=None, hardware=None):
        self.hardware = hardware if hardware is not None else HardwareBook.from_env()
        self.profiles = profiles if profiles is not None else ProfileBook.from_env()
        self.maintenance = LogMaintenance(MaintenancePolicy.from_env())
        self.store = store
        self.tasks = TaskManager(store) if store is not None else None
        self.artifacts = None
        if store is not None and (artifact_dir is not None or store.path != ':memory:'):
            directory = artifact_dir if artifact_dir is not None else os.path.join(
                os.path.dirname(os.path.abspath(store.path)), 'transfers')
            self.artifacts = TransferArtifacts(store, directory)
            self.artifacts.recover()
        self.artifacts_checked = 0.0
        self.devices = {}                 # physical by-path -> current connection
        self.boards = {}                  # immutable serial -> persistent Board
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.stopping = threading.Event()
        self.handoff = {}
        self.armed_paths = {}
        self._flash_presence = {}
        if store:
            for record in store.boards():
                self.boards[record['serial']] = Board(record)
        for entry in handoff or []:
            if (isinstance(entry, dict) and isinstance(entry.get('by_path'), str)
                    and isinstance(entry.get('dev'), str)
                    and isinstance(entry.get('serial'), str)
                    and isinstance(entry.get('identity'), list)
                    and len(entry['identity']) == 3
                    and all(type(value) is int for value in entry['identity'])):
                self.handoff[entry['by_path']] = entry

    def all_devices(self):
        with self.lock:
            known = [self._board_view(board) for board in self.boards.values()]
            for device in known:
                presence = self._flash_presence.get(device.serial, {})
                device.flash_connected = bool(presence.get('connected'))
                device.flash_mode = presence.get('mode', '')
                device.flash_last_seen = presence.get('seen', 0)
            pending = [d for d in self.devices.values() if not d.serial and d.present]
            return known + pending

    def refresh_flash_presence(self):
        """Cache recent USB observations without changing UART connection state."""
        if self.store is None:
            return
        with self.store.lock:
            rows = self.store.db.execute('SELECT serial,document,seen,present FROM flash_targets t '
                'WHERE serial IS NOT NULL AND NOT EXISTS '
                '(SELECT 1 FROM identity_conflicts c WHERE c.serial=t.serial)').fetchall()
        presence = {}
        now = time.time()
        for row in rows:
            old = presence.get(row['serial'])
            if old is None or old['seen'] < row['seen']:
                document = json.loads(row['document'])
                presence[row['serial']] = {'connected': bool(row['present'] and now - row['seen'] <= 15),
                    'mode': document.get('mode', 'unknown'), 'seen': row['seen']}
        with self.lock:
            self._flash_presence = presence

    def _board_view(self, board):
        if board.connection is not None:
            return board.connection
        if board.history is None:
            board.history = Device('', '', self, board=board)
        return board.history

    def resolve(self, key):
        with self.lock:
            return next((d for d in self.all_devices() if d.key == key), None)

    def _select_identity(self, device, info, resolved_serial=''):
        def conflicted(serial):
            return serial != resolved_serial and self.store.has_conflict(serial)

        provisioned = eeprom_serial(info)
        marker_record = self.store.get(marker_serial(info))
        known_marker = bool(marker_record and marker_record['info'].get('serial_source') == 'boot-marker')
        # A copied /boot must not redirect an already known hardware identity into another archive.
        if known_marker:
            for candidate in (str(info.get('serial') or '').strip(), hardware_serial(info)):
                if (candidate != marker_record['serial'] and not conflicted(candidate)
                        and self.store.get(candidate)):
                    raise IdentityError('/boot 标记与已有硬件档案冲突，可能复制了其他板子的标记文件')
        return select_identity(info,
            conflicted=conflicted(str(info.get('serial') or '').strip()),
            known_soc=self.store.get(hardware_serial(info)) is not None,
            known_marker=known_marker,
            soc_conflicted=conflicted(hardware_serial(info)),
            eeprom_conflicted=conflicted(provisioned))

    def _other_identity_online(self, device, serial):
        connections = list(self.devices.values()) + [board.connection for board in self.boards.values()]
        return any(other is not None and other is not device and other.present
                   and (other.serial == serial or not other.serial
                        and (other.reported_serial == serial
                             or getattr(other, 'identity_candidate_serial', '') == serial))
                   for other in connections)

    def _association_offer(self, device, info):
        """Offer only an existing conflicted archive, never guess another serial."""
        serial = eeprom_serial(info) or str(info.get('serial') or '').strip()
        record = self.store.get(serial)
        if (device.serial or not record or not self.store.has_conflict(serial)
                or not device.present or device.stop.is_set()):
            return None
        device.identity_candidate_serial = serial
        if self._other_identity_online(device, serial):
            return None
        try:
            if self._select_identity(device, info, serial)['serial'] != serial:
                return None
        except IdentityError:
            return None
        with self.store.lock:
            conflict = self.store.db.execute('SELECT detected FROM identity_conflicts WHERE serial=?',
                                             (serial,)).fetchone()
        if conflict is None:
            return None
        offer = {'nonce': secrets.token_hex(24), 'serial': serial, 'label': record['label'],
                 'hostname': record['info'].get('hostname', ''), 'last_seen': record['last_seen'],
                 'last_dev': record['last_dev'], 'last_path': record['last_path'],
                 'detected_at': conflict['detected'], 'expires_at': time.time() + 600,
                 'connection_id': device.connection_id}
        device.identity_association = {'offer': offer,
                                      'record': json.dumps(record, sort_keys=True)}
        return dict(offer)

    def _confirm_association(self, device, info, association, actor):
        if actor != 'web':
            raise IdentityError('只有网页操作者可以确认关联历史档案')
        if not isinstance(association, dict) or set(association) != {'nonce', 'serial', 'reason'}:
            raise IdentityError('无效的档案关联确认')
        reason = validate_conflict_reason(association.get('reason'))
        state = getattr(device, 'identity_association', None)
        offer = state and state['offer']
        if (not offer or association.get('nonce') != offer['nonce']
                or association.get('serial') != offer['serial']
                or time.time() >= offer['expires_at'] or device.serial
                or device.connection_id != offer['connection_id']
                or not device.present or device.stop.is_set()
                or self.devices.get(device.by_path) is not device):
            raise IdentityError('档案关联确认已失效，请重新识别')
        serial = offer['serial']
        if json.dumps(self.store.get(serial), sort_keys=True) != state['record']:
            raise IdentityError('历史档案已变化，请重新识别并确认')
        with self.store.lock:
            conflict = self.store.db.execute('SELECT detected FROM identity_conflicts WHERE serial=?',
                                             (serial,)).fetchone()
        if conflict is None or conflict['detected'] != offer['detected_at']:
            raise IdentityError('冲突记录已变化，请重新识别并确认')
        if self._other_identity_online(device, serial):
            raise IdentityError('同一序列号仍在另一个串口在线，拒绝关联历史档案')
        if self._select_identity(device, info, serial)['serial'] != serial:
            raise IdentityError('重新读取的序列号已变化，请重新识别并确认')
        return {'serial': serial, 'reason': reason, 'actor': actor,
                'detected_at': offer['detected_at']}

    def identify(self, device, info, association=None, actor='web'):
        with self.lock:
            provisioned = eeprom_serial(info)
            resolution = self._confirm_association(device, info, association, actor) if association is not None else None
            try:
                selected = self._select_identity(device, info, resolution['serial'] if resolution else '')
            except IdentityError as error:
                offer = self._association_offer(device, info) if association is None else None
                if offer:
                    raise IdentityConfirmationRequired('该序列号已确认不唯一，请核实后确认关联原档案', offer) from error
                raise
            serial = selected['serial']
            established = self.store.get(serial)
            if (established and established['info'].get('identity_status') == 'confirmed'
                    and selected.get('serial_source') in ('device-tree', 'soc-sysfs', 'boot-marker')):
                # A weaker kernel interface must not revoke identity proof recorded
                # earlier through EEPROM, U-Boot provenance or explicit verification.
                selected['serial_source'] = established['info'].get('serial_source')
                selected['identity_status'] = 'confirmed'
            promotion_record = None
            if provisioned:
                aliases = (str(info.get('serial') or '').strip(), hardware_serial(info), marker_serial(info))
                for alias in aliases:
                    alias_record = self.store.get(alias) if alias and alias != serial else None
                    if (alias_record and alias_record['info'].get('identity_status') == 'provisional'
                            and not self.store.get(serial)):
                        # A matching software marker on the same physical attachment is enough
                        # to promote the archive written before EEPROM provisioning.  Other
                        # aliases remain manual: firmware/SoC values can be duplicated or change.
                        safe_marker = (alias == marker_serial(info)
                            and alias_record['info'].get('serial_source') == 'boot-marker'
                            and alias_record.get('last_path') == device.by_path)
                        if safe_marker and (promotion_record is None
                                            or promotion_record['serial'] == alias):
                            promotion_record = alias_record
                        else:
                            raise IdentityError('已发现正式 EEPROM 编号 %s，但该板仍有候选档案 %s；'
                                                '请停服务后用 correct-serial 迁移' % (serial, alias))
            previous_board = self.boards.get(serial)
            device.identity_candidate_serial = serial
            previous = previous_board.connection if previous_board else None
            if previous is not None and previous is not device and previous.present:
                self.store.mark_conflict(serial)
                raise IdentityError('同一序列号已在另一个串口在线，拒绝合并设备')
            if self.store.has_conflict(serial) and device.serial != serial and resolution is None:
                offer = self._association_offer(device, info)
                if offer:
                    raise IdentityConfirmationRequired('该序列号已确认不唯一，请核实后确认关联原档案', offer)
                raise IdentityError('该序列号已确认不唯一；排除重复并核实档案归属后，'
                                    '请管理员用 resolve-conflict 解除冲突')
            if device.serial and device.serial != serial:
                raise IdentityError('当前连接的序列号改变，请重新连接后识别')
            board = self._bind_identity_archive(device, selected, previous_board, promotion_record, resolution)
            if promotion_record:
                self.boards.pop(promotion_record['serial'], None)
            self.boards[serial] = board
            self.armed_paths.pop(device.by_path, None)
            device.auto_identify = False
            device.identity_association = None
            device.identity_candidate_serial = ''
            info.update(selected)
            self.store.event(serial, 'service', 'identified', device.by_path)

    def _bind_identity_archive(self, device, selected, previous_board, promotion_record, resolution=None):
        """Commit identity and relocate all live log writers under stable ownership."""
        serial = selected['serial']
        previous_record = self.store.get(serial)
        promoted_board = self.boards.get(promotion_record['serial']) if promotion_record else None
        registries = {device._sessions}
        for board in (previous_board, promoted_board):
            if board:
                registries.add(board.sessions)
        # Same ordering as SessionRegistry.transfer_to; SSH creation cannot reserve
        # an old catalog while identity binding switches it to a different board.
        with ExitStack() as ownership:
            for registry in sorted(registries, key=id):
                ownership.enter_context(registry.lock)
            with device.log_lock:
                pending_dir = device.stable_dir()
                target = os.path.join(LOG_ROOT, archive_component(serial, selected.get('serial_source', '')))
                previous_dir = (os.path.join(LOG_ROOT, archive_component(
                    serial, previous_record['info'].get('serial_source', '')))
                    if previous_record else '')
                promotion_dir = (os.path.join(LOG_ROOT, archive_component(
                    promotion_record['serial'], promotion_record['info'].get('serial_source', '')))
                    if promotion_record else '')
                sources = list(dict.fromkeys(path for path in (
                    previous_dir, promotion_dir, pending_dir) if path and path != target))
                target_registry = previous_board.sessions if previous_board else device._sessions
                if (sources or device._sessions is not target_registry) and any(
                        registry.pending for registry in registries):
                    raise IdentityError('SSH 会话正在建立，稍后再迁移身份档案')
                ids = []
                for registry in registries:
                    ids.extend(registry.sessions)
                if len(ids) != len(set(ids)):
                    raise IdentityError('SSH session ID collision; refusing identity migration')
                sessions = [session for registry in registries for session in registry.sessions.values()]
                device.close_log()
                with relocate_session_logs(sessions, target) as relocation:
                    staged = []
                    if previous_dir and previous_dir != target:
                        staged += stage_logs(previous_dir, target)
                    if promotion_dir and promotion_dir not in (target, previous_dir):
                        staged += stage_logs(promotion_dir, target)
                    if pending_dir not in (target, previous_dir, promotion_dir):
                        staged += stage_logs(pending_dir, target,
                                             'serial-' + device.connection_id + '-')
                    if promotion_record:
                        record = self.store.promote_eeprom_identity(
                            promotion_record['serial'], selected, device.by_path, device.dev,
                            connection_id=device.connection_id)
                    else:
                        bind_options = {'connection_id': device.connection_id}
                        if resolution is not None:
                            bind_options['conflict_resolution'] = resolution
                        record = self.store.bind(selected, device.by_path, device.dev, **bind_options)
                    relocation.commit()  # Later cleanup failure cannot revert committed log ownership.
                    board = previous_board or device.board or Board(record)
                    board.update(record)
                    board.attach(device)
                    if promoted_board is not None and promoted_board is not board:
                        promoted_board.sessions.transfer_to(board.sessions)
                    finish_staged(staged, sources)
                return board

    def rename(self, device, label, connection_id=None, expected_serial=None):
        with self.lock:
            if expected_serial is not None and device.serial != expected_serial:
                raise IdentityError('设备身份已变化，请刷新后修改标签')
            if not device.serial:
                if (expected_serial != '' or connection_id != device.connection_id
                        or not device.present or device.stop.is_set()
                        or self.devices.get(device.by_path) is not device):
                    raise IdentityError('串口连接已变化，请刷新后修改标签')
                device.attachment_label = self.store.rename_attachment(device.by_path, label)
                return device.key
            device.record = self.store.rename(device.serial, label)
            return device.key

    def prepare_switch(self, device, actor='web'):
        """Detach a board from a still-live UART and arm one automatic identification."""
        if device.file_transfer.active:
            raise IdentityError('串口文件传输中，请完成或取消后再换板')
        if not device.operation_lock.acquire(blocking=False):
            raise IdentityError('已有控制台操作在进行中，稍后再进入换板模式')
        try:
            with self.lock:
                if not device.serial or not device.present:
                    raise IdentityError('只有已识别且在线的设备可以进入换板识别模式')
                old_serial, old_record = device.serial, device.record
                for sid in list(device.ssh_sessions):
                    device.close_session(sid)
                with device.log_lock:
                    device.close_log()
                    old_record['last_seen'] = self.store.touch(old_serial)
                    self.boards[old_serial].detach(device)
                    self.boards[old_serial].history = None
                    self.store.event(old_serial, actor, 'switch-armed', device.by_path)
                    device.detach_identity()
                deadline = time.monotonic() + 15 * 60
                self.armed_paths[device.by_path] = deadline
                device.auto_identify = True
                device.auto_identify_until = deadline
                return device.key
        finally:
            device.operation_lock.release()

    def disarm(self, by_path):
        with self.lock:
            self.armed_paths.pop(by_path, None)

    @staticmethod
    def _handoff_matches(entry, dev, identity):
        return tuple(entry['identity']) == identity and entry['dev'] == dev

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
            found = self._discover()
            with self.lock:
                existing = dict(self.devices)
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
                if d is not None and d.capture_paused.is_set():
                    continue
                if d is not None and (d.stop.is_set() or d.identity != identity):
                    if d.thread and d.thread.is_alive():
                        continue
                    if not d.operation_lock.acquire(blocking=False):
                        continue
                    d.operation_lock.release()
                    d.present = False
                    d = None
                if d is None:
                    with self.lock:
                        record = None
                        entry = None
                        previous = existing.get(key)
                        armed = self.armed_paths.get(key, 0) >= time.monotonic()
                        board = self.boards.get(previous.serial) if previous and previous.serial else None
                        active_board = board.connection if board else None
                        if (previous and previous.record and not armed
                                and (active_board is previous or active_board is None or not active_board.present)
                                and not (self.store and self.store.has_conflict(previous.serial))):
                            record = previous.record
                        else:
                            entry = self.handoff.pop(key, None)
                            if (entry and self._handoff_matches(entry, dev, identity)
                                    and self.store and not armed):
                                record = self.store.get(entry['serial'])
                        board = self.boards.get(record['serial']) if record else None
                        d = Device(key, dev, self if self.store else None, record, board=board)
                        snapshot = entry.get('terminal_snapshot') if entry and record else None
                        geometry = entry.get('terminal_geometry') if entry and record else None
                        if previous and record is previous.record and not armed:
                            snapshot = previous.terminal.screen.snapshot()
                            geometry = previous.terminal.geometry
                        if snapshot and d.terminal.screen.restore(snapshot):
                            d.base = snapshot['off']
                            d.terminal.geometry = geometry
                            d.terminal.input_pending = bool(previous and previous.terminal.input_pending)
                            d.history_loaded = True
                        if record:
                            d.board.attach(d)
                            self.boards[record['serial']] = d.board
                        if armed:
                            d.auto_identify = True
                            d.auto_identify_until = self.armed_paths[key]
                        else:
                            self.armed_paths.pop(key, None)
                        self.devices[key] = d
                if d.thread and d.thread.is_alive():
                    continue
                if not d.operation_lock.acquire(blocking=False):
                    continue
                try:
                    if d.identity != identity:
                        d.probe_info = dict(d.record['info']) if d.record else {}
                        d.hostname = d.probe_info.get('hostname', '')
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

    def _discover(self):
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
        return found

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
            while not d.stop.is_set() and not d.capture_paused.is_set() and self._matches(d):
                if d.fd is None:
                    try:
                        with d.write_lock:
                            if d.stop.is_set() or d.capture_paused.is_set() or not self._matches(d):
                                break
                            with d.cv:
                                d.console_tail = ''
                                d.newline = d.profile.line_ending
                            settings = d.profile.serial
                            d.fd = open_serial(os.path.join(BY_PATH, d.by_path),
                                               flush=False, identity=d.identity, settings=settings)
                            d.serial_settings = settings
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
                        self._capture_wait(d, delay)
                        continue
                try:
                    ready, _, _ = select.select([d.fd], [], [], 0.5)
                    if not ready:
                        # A quiet board is normal, but the host-side tty can
                        # disappear without a readable EOF.  Check the driver
                        # periodically without sending anything to the board.
                        if time.time() - d.last_health_check >= 30:
                            termios.tcgetattr(d.fd)
                            d.last_health_check = time.time()
                        continue
                    data = os.read(d.fd, 65536)
                    if not data:
                        # EOF 的 fd 会一直 readable;不关闭/退避会空转占满 CPU。
                        raise OSError(errno.EIO, '串口 EOF')
                    d.append(data)
                    d.last_health_check = time.time()
                except (BlockingIOError, InterruptedError):
                    continue
                except (OSError, ValueError, termios.error) as e:
                    self._close_reader(d)
                    d._say('%s 读取中断: %s' % (d.dev, e))
                    self._capture_wait(d, 1.0)
                    # 断线后的重开也属于新连接;交回 scan 等旧操作退出,不能原地换 fd。
                    break
        finally:
            self._close_reader(d)
            if not d.capture_paused.is_set() or d.stop.is_set():
                d.stop.set()
                d.dead = True
                d.present = False
                d.close_log()
                d.terminal.screen.close()
                if self.store and d.serial:
                    d.record['last_seen'] = self.store.touch(d.serial)

    @staticmethod
    def _capture_wait(device, seconds):
        deadline = time.monotonic() + seconds
        while not device.stop.is_set() and not device.capture_paused.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            device.stop.wait(min(remaining, 0.1))

    @contextmanager
    def exclusive_serial(self, device, check):
        """Caller holds the console lease; never reopen before helper cleanup."""
        with self.scan_lock:
            check()
            if (self.stopping.is_set() or device.stop.is_set() or not device.present
                    or not device.thread or not device.thread.is_alive()):
                raise TaskError('exclusive serial requires an active collector', 409)
            device.capture_paused.set()
            thread, stop = device.thread, device.stop
        permit = {'resume': True}
        try:
            deadline = time.monotonic() + 5
            while thread.is_alive():
                check()
                if time.monotonic() >= deadline:
                    raise TaskError('collector did not release serial port', 504)
                thread.join(timeout=0.05)
            check()
            if device.fd is not None:
                raise TaskError('collector serial handle is still open', 409)
            device._say('seriald 串口交给外部工具；此期间不采集输出')
            yield permit
        finally:
            with self.scan_lock:
                if permit['resume']:
                    device.capture_paused.clear()
                else:
                    device._say('seriald 外部工具清理失败；串口保持暂停，请确认工具退出后重启采集器')
                # A late/changed attachment is left for normal discovery. Never
                # start a second reader while the old one is still exiting.
                if (permit['resume'] and not self.stopping.is_set() and device.stop is stop and not stop.is_set()
                        and self.devices.get(device.by_path) is device and self._matches(device)):
                    if not thread.is_alive():
                        device._say('seriald 外部工具结束，恢复串口采集')
                        device.thread = threading.Thread(target=self._capture, args=(device,), daemon=True)
                        device.thread.start()
                elif permit['resume'] and not thread.is_alive():
                    device.stop.set()
                    device.dead = True
                    device.present = False
                    device.close_log()

    def close(self):
        self.stopping.set()
        # Wait for an in-flight scan before taking the final set of collectors.
        with self.scan_lock, self.lock:
            views = [self._board_view(board) for board in self.boards.values()]
            devices = list({id(d): d for d in list(self.devices.values()) + views}.values())
            handoff = [{'by_path': d.by_path, 'dev': d.dev, 'serial': d.serial,
                        'identity': list(d.identity), 'device': d}
                       for d in self.devices.values()
                       if d.present and d.serial and d.identity]
        for d in devices:
            d.stop.set()
        if self.tasks is not None and not self.tasks.close():
            raise RuntimeError('task workers are still stopping; keep the registry open')
        for d in devices:
            if d.thread:
                d.thread.join(timeout=6)
            for s in list(d.ssh_sessions):
                d.close_session(s)
            d.close_log()
            d.terminal.screen.close()
        for entry in handoff:
            device = entry.pop('device')
            snapshot = device.terminal.screen.snapshot()
            if snapshot:
                entry.update(terminal_snapshot=snapshot, terminal_geometry=device.terminal.geometry)
        return handoff

    def loop(self):
        while not self.stopping.is_set():
            try:
                self.scan()
                self.refresh_flash_presence()
                for d in self.all_devices():
                    d.reap_sessions()
                    d.health.tick()
                    if self.store and d.serial and d.present:
                        d.record['last_seen'] = self.store.touch(d.serial)
                self.maintenance.run_due(self, LOG_ROOT)
                if self.artifacts is not None and time.monotonic() - self.artifacts_checked >= 60:
                    self.artifacts.expire()
                    from .flash_tasks import FlashActions
                    FlashActions(self).expire()
                    self.artifacts_checked = time.monotonic()
                # 启动/重连只被动采集;自动回车或 USB 软拔插会干扰正在使用的控制台。
            except Exception as e:
                print('seriald scan: %s' % e, file=sys.stderr, flush=True)
            self.stopping.wait(3)
