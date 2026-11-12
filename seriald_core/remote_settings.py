"""Node-local configuration for a split Web/backend deployment."""
import os
from pathlib import Path
import threading

from .web_settings import CONTROL, SettingsError
from .settings_restart import preflight_listener

WEB_FIELDS = CONTROL | {'BASE', 'BACKEND_URL', 'BACKEND_TOKEN', 'BACKEND_CA'}
BACKEND_NETWORK_FIELDS = {'BACKEND_BIND', 'BACKEND_PORT', 'BACKEND_TOKEN', 'BACKEND_CERT', 'BACKEND_KEY'}


def allowed_fields(role, fields):
    return WEB_FIELDS if role == 'web' else (set(fields) - CONTROL - {'STATIC', 'BACKEND_URL', 'BACKEND_CA'}) | BACKEND_NETWORK_FIELDS


def node_snapshot(settings, role, secrets=False):
    result = settings.snapshot(secrets)
    allowed = allowed_fields(role, result['configuration']['environment'])
    result['fields'] = [field for field in result['fields'] if field['name'] in allowed]
    result['configuration']['environment'] = {key: value for key, value in
        result['configuration']['environment'].items() if key in allowed}
    documents = {'agent_tokens'} if role == 'web' else {'profiles', 'hardware', 'flash'}
    result['configuration']['documents'] = {key: value for key, value in
        result['configuration']['documents'].items() if key in documents}
    result['document_names'] = sorted(documents)
    result['node_role'] = role
    return result


def validate_scope(settings, role, incoming):
    if not isinstance(incoming, dict):
        raise SettingsError('配置格式无效')
    allowed = allowed_fields(role, settings.effective()['environment'])
    documents = {'agent_tokens'} if role == 'web' else {'profiles', 'hardware', 'flash'}
    if (not isinstance(incoming.get('environment', {}), dict)
            or not isinstance(incoming.get('documents', {}), dict)):
        raise SettingsError('配置字段必须为对象')
    if (set(incoming.get('environment', {})) - allowed
            or set(incoming.get('documents', {})) - documents):
        raise SettingsError('配置包含不属于当前节点的字段')


def configure_remote_control(server, settings):
    """Restart the Web process only; never activate or restart its backend."""
    server.settings_revision = settings.snapshot()['revision']
    server.settings_activation = {'state': 'idle'}
    server.restart_requested = False
    lock = threading.Lock()

    def activate(revision):
        if not lock.acquire(blocking=False):
            raise SettingsError('正在应用配置', 409)
        try:
            if settings.snapshot()['revision'] != revision:
                raise SettingsError('配置已改变，请重新加载', 409)
            environment = settings.validate(settings.effective())['environment']
            if not environment['BACKEND_URL']:
                raise SettingsError('远程 Web 节点必须配置后端 HTTPS 地址')
            from .backend_link import BackendLink
            BackendLink(environment['BACKEND_URL'], environment['BACKEND_TOKEN'], environment['BACKEND_CA'])
            preflight_listener(server, environment)
            if not (Path(environment['STATIC']) / 'index.html').is_file():
                raise SettingsError('页面资源目录缺少 index.html')
            base = Path(environment['BASE'])
            base.mkdir(parents=True, exist_ok=True)
            if not os.access(base, os.W_OK | os.X_OK):
                raise SettingsError('Web 数据目录不可写')
            server.restart_requested = True
            server.activation_reply = threading.Event()
            server.settings_activation = {'state': 'applying', 'revision': revision}
            def shutdown():
                server.activation_reply.wait(2)
                server.shutdown()
            threading.Thread(target=shutdown, daemon=True).start()
            return {'ok': True, 'state': 'applying', 'revision': revision}
        except BaseException:
            lock.release()
            raise
    server.activate_settings = activate
