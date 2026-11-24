"""Owner setup and atomic live updates to the existing hardware document."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import PurePath
import secrets
import threading
import time

from . import ha_slots
from .board_maintenance import maintenance_for
from .hardware import HardwareBook
from .tasks import TaskError
from .transactions import transactions_for


def legacy_slots(document, url, inventory):
    """Recognize only the fixed helper shipped before the slot editor existed."""
    by_entity = {entity: (key, action) for key, s in inventory.items() for action, entity in s['entities'].items()}
    result = {}
    for serial, actions in document.get('boards', {}).items():
        for action in ('reset', 'enter-download'):
            config = actions.get(action, {})
            argv = config.get('argv', [])
            if (len(argv) != 6 or argv[1] != '-I' or PurePath(argv[2]).name != 'home_assistant_control.py'
                    or argv[3].rstrip('/') != url or argv[-1] not in by_entity):
                continue
            key, expected = by_entity[argv[-1]]
            if action != expected:
                raise TaskError('旧配置的动作和控制器实体不符', 409)
            old = result.setdefault(key, {'serial': serial, 'actions': [], 'allow_agent': True})
            if old['serial'] != serial:
                raise TaskError('同一槽位的旧命令指向不同板卡，请先核对硬件配置', 409)
            old['actions'].append(action)
            old['allow_agent'] &= config.get('allow_agent', False)
    return result


class ControlSlots:
    def __init__(self, manager):
        self.manager = manager
        self.settings = None
        self.lock = threading.RLock()
        self.in_use = set()
        self.onboarding = set()
        self.scans = {}
        self.active_revision = None

    @contextmanager
    def using(self, serial, action, command):
        # Reserve just this board. Other controllers/slots can operate concurrently.
        with self.lock:
            if self.manager.hardware.boards.get(serial, {}).get(action) != command:
                raise TaskError('板卡接线已变化，请重新核对', 409)
            if serial in self.in_use:
                raise TaskError('板卡控制任务正在运行', 423)
            self.in_use.add(serial)
        try:
            yield
        finally:
            with self.lock:
                self.in_use.discard(serial)

    def _apply(self, edited, revision):
        active = HardwareBook(edited)
        saved = self.settings.save({'version': 1, 'documents': {'hardware': edited}}, revision)
        self.manager.hardware = active
        if self.active_revision == revision:
            self.active_revision = saved['revision']
        return saved['revision']

    def unbind(self, serial):
        """An explicit board change empties the old board's control slot."""
        with self.lock:
            book = self.manager.hardware
            if not any(s['serial'] == serial for c in book.document.get('home_assistant', {}).values()
                       for s in c['slots'].values()):
                return
            if serial in self.in_use:
                raise TaskError('控制器动作正在执行，请稍后换板', 423)
            if self.settings is None:
                raise TaskError('控制机未启用配置管理', 503)
            snapshot = self.settings.snapshot(secrets=True)
            edited = deepcopy(snapshot['configuration']['documents'].get('hardware', {}))
            if edited != book.document:
                raise TaskError('硬件配置另有待应用的更改，请先应用', 409)
            for c in edited['home_assistant'].values():
                for key in [key for key, slot in c['slots'].items() if slot['serial'] == serial]:
                    del c['slots'][key]
            self._apply(edited, snapshot['revision'])

    def _online(self, serial):
        board = self.manager.boards.get(serial)
        connection = getattr(board, 'connection', None)
        flash = self.manager._flash_presence.get(serial, {})
        return bool(connection and connection.present or flash.get('connected') and time.time() - flash.get('seen', 0) <= 15)

    def execute(self, payload):
        if self.settings is None:
            raise TaskError('控制机未启用配置管理', 503)
        op = payload.get('action', 'list')
        snapshot = self.settings.snapshot(secrets=True)
        document = snapshot['configuration']['documents'].get('hardware', {})
        connections = document.get('home_assistant', {})
        with self.manager.lock:
            boards = []
            handoff_port = None
            for board in self.manager.boards.values():
                connection = getattr(board, 'connection', None)
                uart = connection if connection and connection.present else None
                if uart and board.serial == payload.get('serial'):
                    handoff_port = uart.by_path
                flash = self.manager._flash_presence.get(board.serial, {})
                boards.append({'serial': board.serial, 'label': board.label,
                               'uart': uart.dev if uart else '',
                               'flash_usb': flash.get('endpoint', '') if flash.get('connected') else ''})
            boards.sort(key=lambda row: (not row['uart'], row['label'].casefold(), row['serial']))
        result = {'revision': snapshot['revision'], 'connections': sorted(connections), 'boards': boards}
        if op == 'list':
            result['bindings'] = [{'origin': url, 'key': key, **v} for url, c in connections.items()
                                  for key, v in c['slots'].items()]
            if handoff_port:
                matches = [row for row in result['bindings'] if row.get('suspended_on') == handoff_port]
                if len(matches) == 1:
                    result['handoff'] = {'origin': matches[0]['origin'], 'key': matches[0]['key']}
            return result
        if payload.get('revision') != snapshot['revision']:
            raise TaskError('接线或配置已更新，请重新打开', 409)
        if op in ('discover', 'saved'):
            url = ha_slots.origin(payload.get('origin'))
            config = connections.get(url, {})
            token = payload.get('token') or config.get('secret_token')
            ha_slots.validate_token(token)
            if op == 'saved' and (not config or payload.get('token')):
                raise TaskError('仅可查看已保存接线')
            inventory = ha_slots.inventory(url, token) if op == 'discover' else {}
            old = legacy_slots(document, url, inventory)
            rows = []
            for key in sorted(inventory.keys() | config.get('slots', {}).keys()):
                s = inventory.get(key) or config['slots'][key]
                binding = config.get('slots', {}).get(key) or old.get(key, {})
                rows.append({'key': key, 'name': s['name'], 'area': s['area'], 'entities': s['entities'],
                             'present': key in inventory, 'serial': binding.get('serial', ''),
                             'occupied_online': self._online(binding.get('serial', '')),
                             'suspended': bool(binding.get('suspended_on')), 'legacy': key in old, 'allow_agent': binding.get('allow_agent', False)})
            scan = secrets.token_hex(16)
            with self.lock:
                now = time.monotonic()
                self.scans = {k: v for k, v in self.scans.items() if v[0] > now}
                if len(self.scans) >= 32:
                    self.scans.pop(next(iter(self.scans)))
                self.scans[scan] = (now + 600, snapshot['revision'], url, token, inventory, old)
            return dict(result, scan=scan, slots=rows, origin=url)
        if op == 'identify-slot':
            return self.identify_slot(payload, snapshot, document, connections)
        if op != 'save':
            raise TaskError('未知接线操作')
        with self.lock:
            scan = self.scans.get(payload.get('scan')) if isinstance(payload.get('scan'), str) else None
            if not scan or scan[0] < time.monotonic() or scan[1] != snapshot['revision']:
                raise TaskError('槽位清单已过期，请重新检测', 409)
            if self.manager.hardware.document != document:
                raise TaskError('硬件配置另有待应用的更改，请先应用', 409)
            _, _, url, token, inventory, old = scan
            if any(origin == url for origin, _, _ in self.onboarding):
                raise TaskError('此控制器正在识别新板，请等待任务结束', 423)
            changes = payload.get('changes')
            if not isinstance(changes, dict) or len(changes) > 512:
                raise TaskError('槽位变更无效')
            edited = deepcopy(document)
            conn = edited.setdefault('home_assistant', {}).setdefault(url, {'slots': {}})
            conn['secret_token'] = token
            slots = conn['slots']
            affected = set()
            for key, change in changes.items():
                if (key not in inventory and key not in slots or not isinstance(change, dict)
                        or not isinstance(change.get('serial'), str) or type(change.get('allow_agent')) is not bool):
                    raise TaskError('请选择清单中的槽位和板卡')
                serial = change['serial']
                current = slots.pop(key, None) or old.get(key)
                if current:
                    if serial and serial != current['serial'] and self._online(current['serial']):
                        raise TaskError('该槽位的原板卡仍在线，请先从原设备页换板', 423)
                    affected.add(current['serial'])
                if key in old:
                    previous = edited['boards'][old[key]['serial']]
                    for action in old[key]['actions']:
                        previous.pop(action)
                if serial:
                    if key not in inventory:
                        raise TaskError('尚未检测到此槽位，只能解除绑定', 409)
                    if serial not in self.manager.boards or self.manager.store.has_conflict(serial):
                        raise TaskError('请先确认板卡身份或解决序列号冲突', 409)
                    s = inventory[key]
                    slots[key] = dict(serial=serial, allow_agent=change['allow_agent'],
                                      name=s['name'], area=s['area'], entities=s['entities'])
                    affected.add(serial)
            maintenance = maintenance_for(self.manager)
            if affected & self.in_use or maintenance and any(maintenance.holds(sn) for sn in affected):
                raise TaskError('所选板卡仍被任务占用，请结束后再改接线', 423)
            revision = self._apply(edited, snapshot['revision'])
            self.scans.pop(payload['scan'])
            return {'ok': True, 'revision': revision}

    def identify_slot(self, payload, snapshot, document, connections):
        """Use an explicitly selected slot whose current occupant is offline."""
        from .flash_tasks import FlashActions, _discovery
        from .flash_store import FlashStore

        slot, request_id = payload.get('slot'), payload.get('request_id')
        connection_id = payload.get('connection_id')
        if not all(isinstance(value, str) and value for value in (slot, request_id, connection_id)):
            raise TaskError('请选择槽位和当前串口连接')
        tasks = self.manager.tasks
        digest = tasks.store.request_digest({key: payload.get(key) for key in ('action', 'slot', 'scan', 'connection_id')})
        previous = FlashStore(self.manager.store).find_request('web', request_id, digest)
        if previous:
            return {'task': tasks.store.get(previous)}
        with self.lock:
            scan = self.scans.get(payload.get('scan')) if isinstance(payload.get('scan'), str) else None
            if not scan or scan[0] < time.monotonic() or scan[1] != snapshot['revision'] or self.manager.hardware.document != document:
                raise TaskError('接线清单已变化，请重新检测槽位', 409)
            _, _, origin, token, inventory, old = scan
            live = inventory.get(slot, {})
            entity = live.get('entities', {}).get('enter-download')
            binding = connections.get(origin, {}).get('slots', {}).get(slot) or old.get(slot, {})
            prior_serial = binding.get('serial', '')
            if not entity:
                raise TaskError('请选择支持下载模式的槽位', 409)
            if prior_serial and self._online(prior_serial):
                raise TaskError('该槽位的原板卡仍在线，请先从原设备页换板', 423)
            if prior_serial and (prior_serial in self.in_use or maintenance_for(self.manager).holds(prior_serial)):
                raise TaskError('该槽位的原板卡仍被任务占用', 423)
        device = self.manager.resolve(payload.get('key'))
        if not device or not device.present or device.serial or device.connection_id != connection_id:
            raise TaskError('待识别串口已变化，请重新打开', 409)
        command = ha_slots.SlotAction(origin, token, slot, 'enter-download', entity, False)
        transactions = transactions_for(device)
        client_id = payload.get('client_id', '')

        def run(context):
            def check():
                context.check()
                if device.connection_id != connection_id or not device.present or device.stop.is_set():
                    raise TaskError('串口连接已变化，无法核对接线', 409)

            with self.lock:
                resource = (origin, slot, connection_id)
                if (any(item[:2] == resource[:2] or item[2] == connection_id for item in self.onboarding)
                        or self.manager.hardware.document != document):
                    raise TaskError('槽位正在识别或配置已变化', 423)
                self.onboarding.add(resource)
            try:
                discovery = _discovery(self.manager)
                if not discovery.argv:
                    raise TaskError('控制机未安装 USB 刷机执行器', 409)
                with transactions.actor_scope('web', client_id), transactions.begin('hardware-enter-download', 31, require_open=False):
                    check()
                    before = {target['target_generation'] for target in discovery.refresh()['targets'] if target['present']}
                    def check_before_control():
                        check()
                        if prior_serial and self._online(prior_serial):
                            raise TaskError('该槽位的原板卡已上线，未触发下载模式', 423)
                    command.run(check_before_control, outcome=lambda value: context.issued() if value == 'issued' else context.mark_outcome(value))
                context.store.update(context.id, result={'control_result': 'acknowledged'})
                context.progress(0, 0, 'checking-usb-identity')
                seen, deadline = set(), time.monotonic() + 15
                while time.monotonic() < deadline:
                    check()
                    for target in discovery.refresh()['targets']:
                        generation = target['target_generation']
                        if not target['present'] or generation in before or generation in seen:
                            continue
                        seen.add(generation)
                        target_id = target['target_id']
                        try:
                            probed = discovery.probe(target_id, generation)
                            row = discovery.get(target_id, generation)
                        except (TaskError, OSError) as error:
                            if getattr(error, 'recovery_task_id', None):
                                raise
                            continue
                        link = row['document'].get('uart_link') or {}
                        if link.get('connection_id') != connection_id:
                            continue
                        if link.get('state') == 'linked' and device.serial and device.serial == probed['serial']:
                            return {'serial': device.serial, 'key': device.key, 'control_result': 'acknowledged'}
                        if link.get('state') != 'paired' or probed['mode'] != 'bootrom' or probed['serial']:
                            raise TaskError('刷机 USB 与当前串口身份不一致，请核对接线', 409)
                        result = FlashActions(self.manager).backup({'target_id': target_id, 'target_generation': generation,
                            'binding_revision': 1, 'request_id': 'identify-usb-' + context.id, 'client_id': client_id},
                            None, identity_only=True)
                        if result.status != 202:
                            raise TaskError(result.payload['error'], result.status)
                        identity_task = result.payload['task']['id']
                        context.store.update(context.id, result={'identity_task_id': identity_task, 'control_result': 'acknowledged'})
                        context.progress(0, 0, 'reading-board-serial')
                        while True:
                            identity = tasks.store.get(identity_task)
                            if identity['state'] not in ('queued', 'running'):
                                break
                            time.sleep(.5)
                        check()
                        if identity['state'] != 'completed':
                            raise TaskError(identity['error'] or '未能从下载设备读取序列号', 409)
                        if not device.serial or device.serial != (identity.get('result') or {}).get('serial'):
                            raise TaskError('已读取 USB 序列号，但无法关联到当前串口', 409)
                        return {'serial': device.serial, 'key': device.key, 'control_result': 'acknowledged',
                                'identity_task_id': identity_task}
                    context.cancelled.wait(.5)
                raise TaskError('槽位已进入下载模式，但未能用串口确认新 USB 连接，请核对两根线', 409)
            finally:
                with self.lock:
                    self.onboarding.discard(resource)

        task = tasks.start('hardware-enter-download', 'web', client_id, [{'serial': '', 'connection_id': connection_id}],
                           run, request_id=request_id, input_digest=digest, tracks_outcome=True)
        return {'task': task}
