"""Coordinated configuration activation, using the existing processes only."""
from contextlib import contextmanager
import http.client
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time

from .httpd import WebTCPServer
from .web_settings import SettingsError, CONTROL, _atomic, _bytes


_RESTART_PHASES = {'restarting-web', 'restarting-backend', 'restarting-windows'}
_ACTIVATION_PHASES = _RESTART_PHASES | {'validating-settings', 'checking-web-listener', 'checking-service-paths',
    'checking-backend-config', 'checking-collector-idle', 'waiting-collector-ready', 'requesting-collector-restart',
    'waiting-collector-revision', 'collector-revision-active', 'revision-active'}
_ACTIVATION_MODES = {'web', 'web-and-collector', 'backend', 'windows'}
_ACTIVATION_STATES = {'applying', 'applied', 'failed', 'unknown', 'interrupted'}
_BLOCKERS = {
    '还有 API 操作未完成，请稍后重试': 'active-api',
    '后台任务仍在运行，请结束后再应用配置': 'active-tasks',
    '存在刷机占用或恢复保留，请先处理': 'board-holds',
    '串口有进行中的操作或输入占用，请先释放': 'serial-occupied',
    '文件传输仍在运行': 'file-transfer',
    'SSH 仍在建立连接': 'session-starting',
}


class SettingsActivation:
    """Last attempt, alongside the launch configuration so it survives re-exec."""

    def __init__(self, settings, node, active_revision):
        self.path = settings.path.with_name(settings.path.name + '.activation-' + node + '.json')
        self.record = {'state': 'idle'}
        try:
            with self.path.open('rb') as source:
                saved = json.loads(source.read(8193))
            if self._valid(saved):
                self.record = saved
        except (OSError, ValueError, TypeError):
            pass
        if self.record['state'] == 'applying':
            if self.record['phase'] in _RESTART_PHASES and self.record['revision'] == active_revision:
                self.phase('revision-active', state='applied')
            else:
                self._save(dict(self.record, state='interrupted'))

    @staticmethod
    def _valid(value):
        if (not isinstance(value, dict) or set(value) - {'state', 'revision', 'mode', 'phase', 'steps', 'http_status', 'blocker'}
                or value.get('state') not in _ACTIVATION_STATES or value.get('mode') not in _ACTIVATION_MODES
                or not isinstance(value.get('revision'), str) or not re.fullmatch('[a-f0-9]{64}', value['revision'])
                or value.get('phase') not in _ACTIVATION_PHASES
                or ('http_status' in value and (type(value['http_status']) is not int or not 400 <= value['http_status'] <= 599))
                or ('blocker' in value and value['blocker'] not in _BLOCKERS.values())):
            return False
        steps = value.get('steps')
        return isinstance(steps, list) and 1 <= len(steps) <= 16 and all(
            isinstance(step, dict) and set(step) == {'phase', 'at_ms'} and step['phase'] in _ACTIVATION_PHASES
            and type(step['at_ms']) is int and step['at_ms'] > 0 for step in steps) and steps[-1]['phase'] == value['phase']

    def _save(self, record):
        self.record = record
        try:
            _atomic(self.path, _bytes(record))
        except OSError:
            logging.getLogger(__name__).warning('Could not persist the configuration activation receipt')

    @contextmanager
    def attempt(self, revision, mode):
        if not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{64}', revision):
            raise SettingsError('配置已改变，请重新加载', 409)
        self.record = {'state': 'applying', 'revision': revision, 'mode': mode, 'steps': []}
        self.phase('validating-settings')
        try:
            yield
        except Exception as error:
            if self.record['state'] == 'applying':
                self.fail(error)
            raise

    def phase(self, phase, *, mode=None, state=None):
        step = {'phase': phase, 'at_ms': time.time_ns() // 1_000_000}
        record = dict(self.record, phase=phase, steps=[*self.record['steps'], step][-16:])
        if mode is not None:
            record['mode'] = mode
        if state is not None:
            record['state'] = state
        self._save(record)

    def fail(self, error, *, uncertain=False):
        record = dict(self.record, state='unknown' if uncertain else 'failed')
        status = getattr(error, 'status', None)
        if type(status) is int and 400 <= status <= 599:
            record['http_status'] = status
        if isinstance(error, SettingsError) and str(error) in _BLOCKERS:
            record['blocker'] = _BLOCKERS[str(error)]
        self._save(record)


def preflight_listener(server, environment):
    address = (environment['BIND'], int(environment['PORT']))
    active = (getattr(server, 'bind_host', server.server_address[0]), server.server_address[1])
    if address == active:
        return
    try:
        with WebTCPServer((address[0], 0 if address[1] == active[1] else address[1]), None):
            pass
    except OSError as error:
        raise SettingsError('新监听地址不可用，当前服务保留', 409) from error


def preflight_storage(environment):
    if not (Path(environment['STATIC']) / 'index.html').is_file():
        raise SettingsError('页面资源目录缺少 index.html')
    for path in (Path(environment['BASE']), Path(environment['SOCKET']).parent,
                 Path(environment['HANDOFF']).parent):
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
            raise SettingsError('新数据或通信目录不可写')


def reexecute(environment, role=None):
    """Called only after servers, workers, registry and serial descriptors close."""
    arguments = ([sys.executable] + sys.argv[1:] if getattr(sys, 'frozen', False)
                 else list(sys.orig_argv))
    if role and getattr(sys, 'frozen', False):
        arguments.insert(1, role)
    os.execve(sys.executable, arguments, environment)


def activation_status(server):
    receipt = getattr(server, 'settings_activation_log', None)
    return receipt.record if receipt is not None else getattr(server, 'settings_activation', {'state': 'idle'})


def configure_collector(server, settings, *, public_backend=False):
    snapshot = settings.snapshot()
    server.settings_revision = snapshot['revision']
    server.settings_restart_revision = snapshot['restart_revision']
    if public_backend:
        server.settings_activation_log = SettingsActivation(settings, 'backend', server.settings_revision)
    slots = getattr(server.manager, 'control_slots', None)
    if slots is not None:
        slots.settings = settings
        slots.active_revision = server.settings_revision
    desktop = getattr(server.manager, 'desktop', None)
    if desktop is not None:
        desktop.setup.settings = settings
    server.configuration_gate = threading.RLock()
    server.configuration_posts = 0
    server.restart_requested = False
    server.activation_reply = threading.Event()

    def apply(revision, receipt=None):
        stage = receipt.phase if receipt is not None else (lambda phase: None)
        if revision != settings.snapshot()['revision']:
            raise SettingsError('配置已改变，请重新加载', 409)
        preflight = getattr(server, 'preflight_settings', None)
        if preflight:
            stage('checking-backend-config')
            preflight(settings.validate(settings.effective())['environment'])
        manager = server.manager
        tasks = manager.tasks
        # Count in-flight writes without serializing unrelated device commands.
        # Task admission is locked so a queued flasher cannot slip past the check.
        stage('checking-collector-idle')
        with server.configuration_gate, tasks._lock:
            if server.configuration_posts > 1:
                raise SettingsError('还有 API 操作未完成，请稍后重试', 423)
            if tasks._active or tasks._closing:
                raise SettingsError('后台任务仍在运行，请结束后再应用配置', 423)
            from .board_maintenance import maintenance_for
            maintenance = maintenance_for(manager)
            if maintenance is not None and maintenance.holds():
                raise SettingsError('存在刷机占用或恢复保留，请先处理', 423)
            for device in manager.all_devices():
                transactions = getattr(device, 'transactions', None)
                if transactions is not None and (transactions.snapshot() or transactions.ownership()):
                    raise SettingsError('串口有进行中的操作或输入占用，请先释放', 423)
                if getattr(getattr(device, 'file_transfer', None), 'active', False):
                    raise SettingsError('文件传输仍在运行', 423)
                if getattr(getattr(device, 'sessions', None), 'pending', 0):
                    raise SettingsError('SSH 仍在建立连接', 423)
            tasks._closing = True
            server.restart_requested = True
            manager.stopping.set()
        if public_backend:
            stage('restarting-backend')
        def shutdown():
            server.activation_reply.wait(2)
            server.shutdown()
        threading.Thread(target=shutdown, daemon=True).start()
        return {'ok': True, 'state': 'restarting'}

    def activate_backend(revision):
        if server.restart_requested:
            raise SettingsError('正在应用配置', 409)
        receipt = server.settings_activation_log
        with receipt.attempt(revision, 'backend'):
            return apply(revision, receipt)

    server.activate_settings = activate_backend if public_backend else apply


def configure_control(server, settings, collector_socket, connection_factory):
    snapshot = settings.snapshot()
    server.settings_revision = snapshot['revision']
    server.settings_restart_revision = snapshot['restart_revision']
    active = settings.effective()
    receipt = server.settings_activation_log = SettingsActivation(settings, 'web', server.settings_revision)
    server.restart_requested = False
    lock = threading.Lock()

    def exchange(target, method, path, body=None):
        connection = connection_factory(target)
        try:
            connection.request(method, path, body=body, headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError('collector settings reply exceeds limit')
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError('invalid collector settings reply')
            if response.status >= 400:
                raise SettingsError(result.get('error', '采集器拒绝应用配置'), response.status)
            return result
        finally:
            connection.close()

    def wiring_is_active(revision):
        try:
            health = exchange(collector_socket, 'GET', '/api/settings-health')
        except (OSError, http.client.HTTPException, ValueError):
            return False
        return (not health.get('restarting') and health.get('revision') == server.settings_revision
                and health.get('wiring_revision') == revision)

    server.wiring_is_active = wiring_is_active

    def apply(revision):
        document = settings.validate(settings.effective())
        if settings.snapshot()['revision'] != revision:
            raise SettingsError('配置已改变，请重新加载', 409)
        env = document['environment']
        changed = {key for key, value in env.items() if active['environment'].get(key) != value}
        document_changes = {key for key, value in document['documents'].items()
                            if active['documents'].get(key) != value}
        needs_collector = bool(changed - CONTROL or document_changes - {'agent_tokens'})
        # Check the new listener before closing the existing one.
        if changed & {'PORT', 'BIND'}:
            receipt.phase('checking-web-listener', mode='web-and-collector' if needs_collector else 'web')
            preflight_listener(server, env)
        if 'STATIC' in changed and not (Path(env['STATIC']) / 'index.html').is_file():
            receipt.phase('checking-service-paths')
            raise SettingsError('页面资源目录缺少 index.html')
        if needs_collector:
            receipt.phase('checking-service-paths', mode='web-and-collector')
            preflight_storage(env)
            if env['SOCKET'] != collector_socket and os.path.exists(env['SOCKET']):
                try:
                    exchange(env['SOCKET'], 'GET', '/api/settings-health')
                except (OSError, http.client.HTTPException):
                    pass
                else:
                    raise SettingsError('新通信路径已有采集器，当前服务保留', 409)
            # Control can start slightly earlier than its collector. Only
            # retry the read-only readiness check, never a restart request.
            receipt.phase('waiting-collector-ready')
            deadline = time.monotonic() + 5
            while True:
                try:
                    health = exchange(collector_socket, 'GET', '/api/settings-health')
                    if not health.get('restarting'):
                        break
                except (OSError, http.client.HTTPException):
                    pass
                if time.monotonic() >= deadline:
                    raise SettingsError('采集器尚未就绪，请检查服务状态后重试', 503)
                time.sleep(0.05)
            try:
                receipt.phase('requesting-collector-restart')
                answer = exchange(collector_socket, 'POST', '/api/settings-activate',
                                  json.dumps({'revision': revision}).encode())
                if answer.get('ok') is not True or answer.get('state') != 'restarting':
                    raise ValueError('collector did not confirm restart')
            except SettingsError:
                raise
            except (OSError, ValueError, http.client.HTTPException) as error:
                failure = SettingsError('采集器连接中断，可能已开始应用；请先检查状态', 503)
                receipt.fail(failure, uncertain=True)
                raise failure from error
        server.activation_reply = threading.Event()

        def run():
            try:
                if needs_collector:
                    receipt.phase('waiting-collector-revision')
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            health = exchange(env['SOCKET'], 'GET', '/api/settings-health')
                            if health.get('revision') == revision and not health.get('restarting'):
                                break
                        except (OSError, ValueError, http.client.HTTPException):
                            pass
                        if time.monotonic() >= deadline:
                            receipt.fail(SettingsError('尚未核实采集器加载目标配置', 504), uncertain=True)
                            lock.release()
                            return
                        time.sleep(0.25)
                    receipt.phase('collector-revision-active')
                receipt.phase('restarting-web')
                server.restart_requested = True
                server.activation_reply.wait(2)
                server.shutdown()
            except Exception as error:
                receipt.fail(error)
                lock.release()

        threading.Thread(target=run, daemon=True).start()
        return {'ok': True, 'state': 'applying', 'revision': revision}

    def activate(revision):
        if not lock.acquire(blocking=False):
            raise SettingsError('正在应用配置', 409)
        try:
            with receipt.attempt(revision, 'web'):
                return apply(revision)
        except BaseException:
            lock.release()
            raise

    server.activate_settings = activate
