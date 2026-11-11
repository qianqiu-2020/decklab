"""Application actions shared by protocol adapters; no HTTP or global manager."""
import time
import re
from .results import response

from .config import RESCUE_BIN
from .transport import try_rescue
from .storage import IdentityError, validate_conflict_reason
from .file_transfer import TransferError
from .transactions import transactions_for, TransactionError
from . import __version__
from .hardware import ACTIONS, host_adapter
from .terminal_backend import SharedTerminal, control_session, screen_for
from .session_base import SessionBase, SSHAuthenticationError, valid_ssh_user, valid_ssh_password


def capabilities():
    """Protocol features, not a promise that a particular board supports a shell."""
    return response(200, {
        'api_version': 1, 'version': __version__,
        'features': {'serial': True, 'ssh': True, 'sse_offsets': True, 'shared_terminal': True,
                     'boot_archives': True, 'boot_compare': True,
                     'custom_probe_commands': True, 'unidentified_terminal_access': True,
                     'operation_status': True, 'operation_cancel': True, 'durable_tasks': True},
        'tasks': {'kinds': ['identify', 'ssh-enable', 'ssh-connect', 'file-copy', 'file-upload', 'file-download']
                  + ['hardware-' + action for action in ACTIONS], 'restart': 'interrupt',
                  'file_transfer': ['copy', 'upload', 'download']},
        'artifacts': {'chunk_size': 1024 ** 2, 'append_body_limit': 2 * 1024 ** 2, 'download': 'stream'},
        'file_transfer': {'transport': 'serial', 'requires_linux_shell': True,
                          'integrity': 'sha256'},
        'ssh_provisioning': 'profile-controlled',
        'input_ownership': 'optional-client',
        'hardware': {'actions': list(ACTIONS), 'configuration': 'explicit-serial',
                     'host_adapter': host_adapter(), 'default': 'disabled'},
    })




def validate_identify_request(payload, actor):
    """Association is a human confirmation, never an agent or implicit action."""
    init_marker = payload.get('init_boot_marker', False)
    if type(init_marker) is not bool:
        return response(400, {'ok': False, 'error': 'init_boot_marker must be a boolean'})
    if 'association' not in payload:
        return None
    if actor != 'web':
        return response(403, {'ok': False, 'error': '只有网页操作者可以确认关联历史档案'})
    association = payload['association']
    if (not isinstance(association, dict) or set(association) != {'nonce', 'serial', 'reason'}
            or any(not isinstance(association.get(key), str) or not association[key].strip()
                   for key in ('nonce', 'serial', 'reason'))
            or len(association['nonce']) > 128 or len(association['serial'].encode('utf-8')) > 100):
        return response(400, {'ok': False, 'error': 'invalid identity association confirmation'})
    if init_marker:
        return response(400, {'ok': False, 'error': '关联历史档案时不能同时创建身份标记'})
    try:
        validate_conflict_reason(association['reason'])
    except IdentityError as error:
        return response(400, {'ok': False, 'error': str(error)})
    return None


class DeviceActions:
    def __init__(self, manager, recover=None, recovery_binary=RESCUE_BIN):
        self.manager = manager
        self.recover = recover if recover is not None else try_rescue
        self.recovery_binary = recovery_binary

    def operation(self, device):
        transactions = transactions_for(device)
        return response(200, {'operation': transactions.snapshot(), 'input_owner': transactions.ownership()})

    def execute(self, action, payload, device, actor='web'):
        if not isinstance(payload, dict):
            return response(400, {'error': 'JSON object required'})
        if not isinstance(actor, str) or not actor or len(actor) > 80:
            return response(400, {'error': 'actor must be a non-empty string of at most 80 characters'})
        client_id = payload.get('client_id', '') if actor == 'web' else ''
        if not isinstance(client_id, str) or client_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', client_id):
            return response(400, {'error': 'invalid client_id'})
        with transactions_for(device).actor_scope(actor, client_id):
            if action in ('send', 'probe', 'identify', 'ssh-enable', 'rescue') or (
                    action.startswith('file-transfer/') and action != 'file-transfer/abort'):
                try:
                    transactions_for(device).check_owner(actor)
                except TransactionError as error:
                    return response(error.status, {'ok': False, 'error': str(error), 'code': error.code})
            return self._dispatch(action, payload, device, actor)

    def _dispatch(self, action, payload, d, actor):
        if action == 'probe-settings':
            # Persisting a command changes future identity probes. This is a
            # human configuration operation, never delegated by an AI grant.
            if actor != 'web':
                return response(403, {'ok': False, 'error': '只有网页操作者可以配置探测命令'})
            if payload.get('read') is True:
                return response(200, dict(d.probe_settings(include_default=True),
                                         connection_id=d.connection_id))
            connection = payload.get('connection_id')
            if not isinstance(connection, str) or connection != d.connection_id:
                return response(409, {'ok': False, 'error': '连接已变化，请刷新后重试'})
            try:
                from .profiles import validate_probe_settings
                settings = validate_probe_settings({'mode': payload.get('mode'),
                                                     'command': payload.get('command', '')})
                with transactions_for(d).begin('probe-settings', 5, require_open=False,
                                               expected_connection=connection):
                    if not d.present or not d.by_path:
                        return response(409, {'ok': False, 'error': '请先选择当前串口连接'})
                    self.manager.store.set_probe_settings(d.by_path, settings)
                    # Saving preferences must not inject a command into a console.
                    d.auto_identify_started = True
                    if d.serial:
                        self.manager.store.event(d.serial, actor, 'probe-settings', settings['mode'])
                return response(200, dict(d.probe_settings(include_default=True), ok=True,
                                         connection_id=d.connection_id))
            except (ValueError, TransactionError) as error:
                return response(getattr(error, 'status', 400), {'ok': False, 'error': str(error)})

        if action in ('input-claim', 'input-release'):
            try:
                transactions = transactions_for(d)
                if action == 'input-claim':
                    takeover = payload.get('takeover', False)
                    if type(takeover) is not bool:
                        return response(400, {'error': 'takeover must be a boolean'})
                    owner = transactions.claim(actor, takeover)
                else:
                    transactions.release(str(payload.get('id') or ''), actor)
                    owner = None
                if d.serial:
                    self.manager.store.event(d.serial, actor, action, owner['id'] if owner else str(payload.get('id') or ''))
                return response(200, {'ok': True, 'input_owner': owner})
            except TransactionError as error:
                return response(error.status, {'ok': False, 'error': str(error), 'code': error.code})
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
            if ssh_user is not None and not valid_ssh_user(ssh_user):
                return response(400, {'ok': False, 'error': 'ssh_user must be an unambiguous login name of at most 128 characters'})
            if ssh_password is not None and not valid_ssh_password(ssh_password):
                return response(400, {'ok': False, 'error': 'ssh_password must be a single-line string of at most 4096 characters'})
            try:
                s, err = d.ssh_connect(actor=actor, title=str(payload.get('title') or '')[:2048],
                                      ssh_user=ssh_user, ssh_password=ssh_password)
            except SSHAuthenticationError as error:
                return response(502, {'ok': False, 'code': 'ssh-authentication-failed',
                                      'ssh_user': error.user, 'error': str(error)})
            if not s:
                return response(502, {'ok': False, 'error': err})
            credential_note = getattr(s, 'ssh_credentials_note', '')
            return response(200, {'ok': True, 'sid': s.sid, 'ip': s.ip,
                                    'persistent': bool(getattr(s, 'persistent', False)),
                                    'auto_configured': bool(getattr(s, 'auto_configured', False)),
                                    'ssh_credentials_saved': getattr(s, 'ssh_credentials_saved', False) is True,
                                    'ssh_credentials_note': credential_note if isinstance(credential_note, str) else '',
                                    'device_key': d.key, 'serial': d.serial})

        if action == 'session-send':
            sid = str(payload.get('sid') or 'serial')
            text = str(payload.get('data') or '')
            if not text:
                return response(400, {'error': 'data required'})
            if sid == 'serial':
                try:
                    transactions_for(d).check_owner(actor)
                except TransactionError as error:
                    return response(error.status, {'ok': False, 'error': str(error), 'code': error.code})
                ok, err = d.send(text)
            else:
                s = d.get_session(sid)
                if not s:
                    return response(404, {'error': 'no such session'})
                if actor != s.actor and actor not in s.takeovers:
                    return response(403, {'ok': False, 'error': '先介入该 SSH 会话才能输入'})
                if not control_session(s, actor, transactions_for(d).client_id):
                    return response(409, {'ok': False, 'error': '另一客户端正在控制 SSH；请先介入此会话', 'code': 'session-controlled'})
                ok = (s.write(text, actor=actor, client_id=transactions_for(d).client_id)
                      if isinstance(s, SessionBase) else s.write(text))
                err = ''
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
            if isinstance(s, SessionBase):
                s.takeover(actor, transactions_for(d).client_id)
            else:
                s.takeovers.add(actor)
                control_session(s, actor, transactions_for(d).client_id, takeover=True)
            self.manager.store.event(d.serial, actor, 'session-takeover', sid)
            terminal = screen_for(s)
            result = {'ok': True, 'sid': sid}
            if isinstance(terminal, SharedTerminal):
                state = terminal.metadata()
                result.update(terminal_state=state, terminal_writable=state['controller'] == {
                    'actor': actor, 'client_id': transactions_for(d).client_id})
            return response(200, result)

        if action == 'session-resize':
            sid = str(payload.get('sid') or '')
            try:
                cols = int(payload.get('cols') or 0)
                rows = int(payload.get('rows') or 0)
            except (TypeError, ValueError, OverflowError):
                return response(400, {'error': 'bad cols/rows'})
            if sid == 'serial':
                if (not 20 <= cols <= 500 or not 2 <= rows <= 200
                        or type(payload.get('observed_off')) is not int
                        or type(payload.get('automatic', False)) is not bool
                        or type(payload.get('presentation', False)) is not bool):
                    return response(400, {'error': 'bad serial terminal dimensions or offset'})
                try:
                    if payload.get('presentation') is True:
                        return response(200, d.terminal.presentation(cols, rows))
                    result = d.terminal.configure(cols, rows, payload['observed_off'], payload.get('automatic', False))
                    return response(200 if result['ok'] else 503, result)
                except TransactionError as error:
                    return response(error.status, {'ok': False, 'code': error.code, 'error': str(error)})
            s = d.get_session(sid)
            if not s:
                return response(404, {'error': 'no such session'})
            if actor != s.actor and actor not in s.takeovers:
                return response(403, {'ok': False, 'error': '先介入该 SSH 会话才能调整终端大小'})
            if not control_session(s, actor, transactions_for(d).client_id):
                return response(200, {'ok': True, 'skipped': True, 'cols': s.cols, 'rows': s.rows,
                                      'terminal_state': screen_for(s).metadata()})
            ok = (s.resize(cols, rows, actor=actor, client_id=transactions_for(d).client_id)
                  if isinstance(s, SessionBase) else s.resize(cols, rows))
            return response(200, {'ok': ok, 'cols': s.cols, 'rows': s.rows})

        if action == 'session-close':
            sid = str(payload.get('sid') or '')
            s = d.get_session(sid)
            if not s:
                return response(404, {'error': 'no such session'})
            if actor != s.actor and actor not in s.takeovers:
                return response(403, {'ok': False, 'error': '先介入该 SSH 会话才能关闭'})
            if not control_session(s, actor, transactions_for(d).client_id):
                return response(409, {'ok': False, 'error': '另一客户端正在控制 SSH；请先介入此会话', 'code': 'session-controlled'})
            if not d.close_session(sid):
                return response(404, {'error': 'no such session'})
            return response(200, {'ok': True})

        if action == 'identify':
            invalid = validate_identify_request(payload, actor)
            if invalid is not None:
                return invalid
            init_marker = payload.get('init_boot_marker', False)
            if 'association' in payload:
                info = d.run_probe(association=payload['association'], actor=actor)
            else:
                info = d.run_probe(init_boot_marker=True) if init_marker else d.run_probe()
            if actor != 'web':
                info = dict(info)
                info.pop('identity_confirmation', None)
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
