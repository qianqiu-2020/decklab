"""Coordinated configuration activation, using the existing processes only."""
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

from .web_settings import SettingsError, CONTROL


def preflight_listener(server, environment):
    address = (environment['BIND'], int(environment['PORT']))
    if address == server.server_address:
        return
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((address[0], 0 if address[1] == server.server_address[1] else address[1]))
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


def configure_collector(server, settings):
    server.settings_revision = settings.snapshot()['revision']
    server.configuration_gate = threading.RLock()
    server.configuration_posts = 0
    server.restart_requested = False
    server.activation_reply = threading.Event()

    def activate(revision):
        if revision != settings.snapshot()['revision']:
            raise SettingsError('配置已改变，请重新加载', 409)
        preflight = getattr(server, 'preflight_settings', None)
        if preflight:
            preflight(settings.validate(settings.effective())['environment'])
        manager = server.manager
        tasks = manager.tasks
        # Count in-flight writes without serializing unrelated device commands.
        # Task admission is locked so a queued flasher cannot slip past the check.
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
        def shutdown():
            server.activation_reply.wait(2)
            server.shutdown()
        threading.Thread(target=shutdown, daemon=True).start()
        return {'ok': True, 'state': 'restarting'}

    server.activate_settings = activate


def configure_control(server, settings, collector_socket, connection_factory):
    server.settings_revision = settings.snapshot()['revision']
    active = settings.effective()
    server.settings_activation = {'state': 'idle'}
    server.restart_requested = False
    lock = threading.Lock()

    def exchange(target, method, path, body=None):
        connection = connection_factory(target)
        try:
            connection.request(method, path, body=body, headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read(65537)
            if len(raw) > 65536:
                raise SettingsError('采集器响应过大', 502)
            result = json.loads(raw)
            if response.status >= 400:
                raise SettingsError(result.get('error', '采集器拒绝应用配置'), response.status)
            return result
        finally:
            connection.close()

    def activate(revision):
        if not lock.acquire(blocking=False):
            raise SettingsError('正在应用配置', 409)
        try:
            document = settings.validate(settings.effective())
            if settings.snapshot()['revision'] != revision:
                raise SettingsError('配置已改变，请重新加载', 409)
            env = document['environment']
            changed = {key for key, value in env.items() if active['environment'].get(key) != value}
            document_changes = {key for key, value in document['documents'].items()
                                if active['documents'].get(key) != value}
            needs_collector = bool(changed - CONTROL or document_changes - {'agent_tokens'})
            # Check a new listener before closing a working one. Bind accepts the
            # same IPv4 addresses/hostnames as BoundedThreadingTCPServer.
            if changed & {'PORT', 'BIND'}:
                preflight_listener(server, env)
            if 'STATIC' in changed and not (Path(env['STATIC']) / 'index.html').is_file():
                raise SettingsError('页面资源目录缺少 index.html')
            if needs_collector:
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
                    exchange(collector_socket, 'POST', '/api/settings-activate',
                             json.dumps({'revision': revision}).encode())
                except (OSError, http.client.HTTPException) as error:
                    raise SettingsError('采集器连接中断，可能已开始应用；请先检查状态', 503) from error
            server.settings_activation = {'state': 'applying', 'revision': revision}
            server.activation_reply = threading.Event()

            def run():
                try:
                    if needs_collector:
                        deadline = time.monotonic() + 30
                        while True:
                            try:
                                health = exchange(env['SOCKET'], 'GET', '/api/settings-health')
                                if health.get('revision') == revision and not health.get('restarting'):
                                    break
                            except (OSError, ValueError, http.client.HTTPException):
                                pass
                            if time.monotonic() >= deadline:
                                raise SettingsError('采集器应用未完成，控制页面保留；请检查服务状态')
                            time.sleep(0.25)
                    server.restart_requested = True
                    server.activation_reply.wait(2)
                    server.shutdown()
                except Exception:
                    server.settings_activation = {'state': 'failed', 'revision': revision,
                                                  'error': '配置应用未完成；控制页面保留，请检查采集器状态'}
                    lock.release()

            threading.Thread(target=run, daemon=True).start()
            return {'ok': True, 'state': 'applying', 'revision': revision}
        except BaseException:
            lock.release()
            raise

    server.activate_settings = activate
