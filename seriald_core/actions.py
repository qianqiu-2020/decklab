"""Application actions shared by protocol adapters; no HTTP or global manager."""
from dataclasses import dataclass
import time

from .config import RESCUE_BIN
from .transport import try_rescue
from .storage import IdentityError
from .file_transfer import TransferError
from .transactions import transactions_for, TransactionError
from . import __version__


def capabilities():
    """Protocol features, not a promise that a particular board supports a shell."""
    return response(200, {
        'api_version': 1, 'version': __version__,
        'features': {'serial': True, 'ssh': True, 'sse_offsets': True,
                     'boot_archives': True, 'boot_compare': True,
                     'operation_status': True, 'operation_cancel': True},
        'file_transfer': {'transport': 'serial', 'requires_linux_shell': True,
                          'integrity': 'sha256'},
        'ssh_provisioning': 'profile-controlled',
        'input_ownership': 'shared',
    })


@dataclass(frozen=True)
class ActionResult:
    status: int
    payload: dict


def response(status, payload):
    if status >= 400 or payload.get('ok') is False:
        payload = dict(payload)
        payload.setdefault('code', {
            400: 'invalid-argument', 403: 'forbidden', 404: 'not-found',
            409: 'conflict', 413: 'too-large', 502: 'remote-failure',
            503: 'unavailable', 504: 'timeout',
        }.get(status, 'operation-failed'))
    return ActionResult(status, payload)


class DeviceActions:
    def __init__(self, manager, recover=None, recovery_binary=RESCUE_BIN):
        self.manager = manager
        self.recover = recover if recover is not None else try_rescue
        self.recovery_binary = recovery_binary

    def operation(self, device):
        return response(200, {'operation': transactions_for(device).snapshot()})

    def execute(self, action, payload, device, actor='web'):
        if not isinstance(payload, dict):
            return response(400, {'error': 'JSON object required'})
        if not isinstance(actor, str) or not actor or len(actor) > 80:
            return response(400, {'error': 'actor must be a non-empty string of at most 80 characters'})
        with transactions_for(device).actor_scope(actor):
            return self._dispatch(action, payload, device, actor)

    def _dispatch(self, action, payload, d, actor):
        if action == 'operation-cancel':
            try:
                transactions_for(d).cancel(str(payload.get('id') or ''), actor)
                if d.serial:
                    self.manager.store.event(d.serial, actor, 'operation-cancel', str(payload.get('id') or ''))
                return response(200, {'ok': True})
            except TransactionError as error:
                return response(error.status, {'ok': False, 'error': str(error), 'code': error.code})
        if action == 'session-result':
            try:
                if not d.serial:
                    raise IdentityError('设备尚未识别')
                self.manager.store.session_result(d.serial, str(payload.get('sid') or ''),
                                         payload.get('completed'), payload.get('exit_code'), actor=actor)
                return response(200, {'ok': True})
            except IdentityError as error:
                return response(400, {'ok': False, 'error': str(error)})

        if action == 'switch-mode':
            try:
                old_serial = d.serial
                key = self.manager.prepare_switch(d, actor)
                return response(200, {'ok': True, 'device_key': key, 'serial': '',
                                        'old_serial': old_serial})
            except IdentityError as error:
                return response(409, {'ok': False, 'error': str(error)})

        if action.startswith('file-transfer/'):
            action = action.rsplit('/', 1)[-1]
            if action not in ('start', 'chunk', 'batch', 'finish', 'abort'):
                return response(404, {'ok': False, 'error': 'unknown transfer action'})
            connection_id = str(payload.get('connection_id') or '')
            try:
                if action == 'start':
                    result = d.file_transfer.start(
                        payload.get('direction'), payload.get('path'), connection_id,
                        actor=actor, size=payload.get('size'),
                        overwrite=payload.get('overwrite') is True)
                    if d.serial:
                        self.manager.store.event(d.serial, actor, 'file-transfer-start',
                                        '%s %s (%s bytes)' % (result['direction'], result['path'], result['size']))
                elif action == 'chunk':
                    result = d.file_transfer.chunk(str(payload.get('id') or ''), connection_id,
                                                   payload.get('offset'), payload.get('data_b64'))
                elif action == 'batch':
                    result = d.file_transfer.batch(str(payload.get('id') or ''), connection_id,
                                                   payload.get('offset'), payload.get('data_b64'))
                elif action == 'finish':
                    result = d.file_transfer.finish(str(payload.get('id') or ''), connection_id)
                    if d.serial:
                        self.manager.store.event(d.serial, actor, 'file-transfer-finish',
                                        '%s %s (%s bytes, sha256 %s)' % (
                                            result['direction'], result['path'], result['size'], result['sha256']))
                else:
                    result = d.file_transfer.abort(str(payload.get('id') or ''), connection_id)
                    if d.serial:
                        self.manager.store.event(d.serial, actor, 'file-transfer-abort', result['id'])
                return response(200, dict(result, ok=True))
            except TransferError as error:
                return response(error.status, {'ok': False, 'error': str(error)})

        if d.serial and action in ('send', 'rescue', 'identify', 'ssh-enable'):
            self.manager.store.event(d.serial, actor, action.rsplit('/', 1)[-1],
                            'request: sid=' + str(payload.get('sid') or 'serial'))

        if action == 'send':
            text = str(payload.get('data') or '')
            if not text:
                return response(400, {'error': 'data required'})
            ok, err = d.send(text)
            return response(200 if ok else 503, {'ok': ok, 'error': err, 'dev': d.dev})

        if action == 'probe':
            ok, err = d.send('\r')
            if ok:
                d.last_probe = time.time()
                d.probed += 1
                d._say('手动探测:已发送回车')
            return response(200, {'ok': ok, 'error': err})

        if action == 'rescue':
            if d.file_transfer.active:
                return response(409, {'ok': False, 'error': '串口文件传输中，请先完成或取消'})
            # 只有用户主动选择才重新绑定 USB，不基于静默时长自动执行。
            mode = str(payload.get('mode') or 'rebind')
            if mode != 'rebind':
                return response(400, {'error': 'mode must be rebind'})
            try:
                with transactions_for(d).begin('usb-recovery', 35, require_open=False):
                    with d.write_lock:
                        if not d.present or not self.manager._matches(d):
                            return response(409, {'ok': False, 'error': '设备已重新枚举,请刷新后重试'})
                        d.last_rescue = time.time()
                        rx_before = d.bytes
                        ok, note = self.recover(d.dev, mode)
                    d.rescue_note = note
                    d.fail_count = 0
                    d._say('手动自愈(%s)%s: %s' % (mode, '步骤完成' if ok else '失败', note))
            except TransactionError as error:
                return response(error.status, {'ok': False, 'error': str(error), 'code': error.code})
            # sysfs 写成功只能证明辅助步骤完成;不能据此宣布采集器已恢复。
            recovered = d.fd is not None and d.bytes > rx_before and self.manager._matches(d)
            requires_setup = not ok and any(x in note.lower() for x in
                                           ('未安装', 'password', 'not allowed', 'sudo 需要密码'))
            hint = ''
            if requires_setup:
                hint = ('特权 recovery helper 未就绪。检查 %s、'
                        '/etc/sudoers.d/seriald-rescue 和 collector 日志；'
                        'Ubuntu 包会自动安装并仅向 seriald 服务账户放行该 helper。' % self.recovery_binary)
            return response(200, {'ok': ok, 'recovered': recovered, 'requires_setup': requires_setup,
                                    'note': note, 'hint': hint, 'mode': mode})

        if action == 'ssh-connect':
            # 一键 SSH:起一条与串口并存的独立会话,输出单独落盘
            ssh_user = payload.get('ssh_user')
            ssh_password = payload.get('ssh_password')
            if ssh_user is not None and (not isinstance(ssh_user, str) or not ssh_user.strip()):
                return response(400, {'ok': False, 'error': 'ssh_user must be a non-empty string'})
            if ssh_password is not None and not isinstance(ssh_password, str):
                return response(400, {'ok': False, 'error': 'ssh_password must be a string'})
            s, err = d.ssh_connect(actor=actor, title=str(payload.get('title') or '')[:2048],
                                    ssh_user=ssh_user.strip() if isinstance(ssh_user, str) else None,
                                    ssh_password=ssh_password)
            if not s:
                return response(502, {'ok': False, 'error': err})
            return response(200, {'ok': True, 'sid': s.sid, 'ip': s.ip,
                                    'persistent': bool(getattr(s, 'persistent', False)),
                                    'auto_configured': bool(getattr(s, 'auto_configured', False)),
                                    'device_key': d.key, 'serial': d.serial})

        if action == 'session-send':
            sid = str(payload.get('sid') or 'serial')
            text = str(payload.get('data') or '')
            if not text:
                return response(400, {'error': 'data required'})
            if sid == 'serial':
                ok, err = d.send(text)
            else:
                s = d.get_session(sid)
                if not s:
                    return response(404, {'error': 'no such session'})
                if actor != s.actor and actor not in s.takeovers:
                    return response(403, {'ok': False, 'error': '先介入该 SSH 会话才能输入'})
                ok, err = (s.write(text), '')
            if ok:
                if d.serial:
                    self.manager.store.event(d.serial, actor, 'session-send', 'request: sid=' + sid)
                d.record_input(sid, actor, text)
            return response(200 if ok else 503, {'ok': ok, 'error': err})

        if action == 'session-takeover':
            sid = str(payload.get('sid') or '')
            s = d.get_session(sid)
            if not d.serial or not s:
                return response(404, {'ok': False, 'error': 'no such active SSH session'})
            s.takeovers.add(actor)
            self.manager.store.event(d.serial, actor, 'session-takeover', sid)
            return response(200, {'ok': True, 'sid': sid})

        if action == 'session-resize':
            sid = str(payload.get('sid') or '')
            try:
                cols = int(payload.get('cols') or 0)
                rows = int(payload.get('rows') or 0)
            except (TypeError, ValueError):
                return response(400, {'error': 'bad cols/rows'})
            s = d.get_session(sid)
            if not s:
                return response(404, {'error': 'no such session'})
            if actor != s.actor and actor not in s.takeovers:
                return response(403, {'ok': False, 'error': '先介入该 SSH 会话才能调整终端大小'})
            return response(200, {'ok': s.resize(cols, rows), 'cols': s.cols, 'rows': s.rows})

        if action == 'session-close':
            sid = str(payload.get('sid') or '')
            s = d.get_session(sid)
            if not s:
                return response(404, {'error': 'no such session'})
            if actor != s.actor and actor not in s.takeovers:
                return response(403, {'ok': False, 'error': '先介入该 SSH 会话才能关闭'})
            if not d.close_session(sid):
                return response(404, {'error': 'no such session'})
            return response(200, {'ok': True})

        if action == 'identify':
            init_marker = payload.get('init_boot_marker', False)
            if type(init_marker) is not bool:
                return response(400, {'ok': False, 'error': 'init_boot_marker must be a boolean'})
            info = d.run_probe(init_boot_marker=True) if init_marker else d.run_probe()
            if init_marker and info.get('ok'):
                self.manager.store.event(d.serial, actor, 'boot-marker-initialized', '/boot/seriald-id')
            return response(200 if info.get('ok') else 502, dict(info, device_key=d.key))

        if action == 'ssh-enable':
            r = d.run_ssh_enable()
            return response(200 if r.get('ok') else 502, r)

        if action == 'label':
            try:
                key = self.manager.rename(d, payload.get('label'))
                return response(200, {'ok': True, 'label': key, 'device_key': key, 'serial': d.serial})
            except IdentityError as error:
                return response(409, {'ok': False, 'error': str(error)})

        return response(404, {'error': 'not found'})
