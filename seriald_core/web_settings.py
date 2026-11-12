"""Private, versioned Web configuration layered over the launch environment."""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading

from .request_json import load_object


class SettingsError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


DEFAULTS = {
    'BASE': '', 'STATIC': '', 'SOCKET': '', 'HANDOFF': '', 'BY_PATH': '',
    'RESCUE_BIN': '/usr/local/sbin/seriald-rescue.sh', 'PORT': '8898',
    'BIND': '127.0.0.1', 'AUTH_TOKEN': '', 'AGENT_TOKENS_FILE': '',
    'TRUST_LOOPBACK': '0', 'MCP_ENABLED': '1', 'MCP_PYTHON': '',
    'MCP_ALLOWED_HOSTS': '', 'BAUD': '115200', 'DATA_BITS': '8', 'PARITY': 'N',
    'STOP_BITS': '1', 'FLOW_CONTROL': 'none', 'NEWLINE': 'auto',
    'SSH_USER': 'root', 'SSH_PASSWORD': 'bianbu', 'CONSOLE_USER': '',
    'CONSOLE_PASSWORD': '', 'PROBE': 'linux', 'PROBE_COMMAND': '',
    'AUTO_CONFIGURE_SSH': '1', 'PROFILES': '', 'HARDWARE': '', 'FLASH': '',
    'FLASH_EXECUTOR': '', 'LOG_RETENTION_DAYS': '0', 'DISK_MIN_FREE_MB': '1024',
    'DISK_MIN_FREE_PERCENT': '5', 'TRANSFER_MAX_FILES': '128',
    'TRANSFER_MAX_FILE_BYTES': str(8 * 1024 ** 3),
    'TRANSFER_MAX_TOTAL_BYTES': str(32 * 1024 ** 3),
    'TRANSFER_MIN_FREE_BYTES': str(256 * 1024 ** 2),
    'TRANSFER_IDLE_SECONDS': '3600', 'TRANSFER_RETENTION_SECONDS': str(7 * 86400),
    'UI_LANGUAGE': 'auto', 'SCROLLBACK': str(256 * 1024), 'SSH_LIMIT': '8',
    'SSH_IDLE': '1800', 'SHARE_SECONDS': '86400',
}
DOCUMENTS = {'profiles': 'PROFILES', 'hardware': 'HARDWARE', 'flash': 'FLASH',
             'agent_tokens': 'AGENT_TOKENS_FILE'}
DOCUMENT_LIMITS = {'profiles': 65536, 'hardware': 65536,
                   'flash': 1024 ** 2, 'agent_tokens': 65536}
CHOICES = {'PARITY': ['N', 'E', 'O'], 'DATA_BITS': ['5', '6', '7', '8'],
           'STOP_BITS': ['1', '2'], 'FLOW_CONTROL': ['none', 'rtscts', 'xonxoff'],
           'NEWLINE': ['auto', 'cr', 'lf', 'crlf'], 'PROBE': ['linux', 'custom', 'manual'],
           'MCP_ENABLED': ['0', '1'], 'TRUST_LOOPBACK': ['0', '1'],
           'AUTO_CONFIGURE_SSH': ['0', '1', 'false', 'true']}
CHOICES['UI_LANGUAGE'] = ['auto', 'en', 'zh-CN']
SENSITIVE = {'AUTH_TOKEN', 'SSH_PASSWORD', 'CONSOLE_PASSWORD'}
CONTROL = {'PORT', 'BIND', 'AUTH_TOKEN', 'AGENT_TOKENS_FILE', 'TRUST_LOOPBACK',
           'MCP_ENABLED', 'MCP_PYTHON', 'MCP_ALLOWED_HOSTS', 'STATIC', 'UI_LANGUAGE', 'SHARE_SECONDS'}
SHARED = {'BASE', 'SOCKET'}
PATHS = {'BASE', 'STATIC', 'SOCKET', 'HANDOFF', 'BY_PATH', 'RESCUE_BIN',
         'AGENT_TOKENS_FILE', 'MCP_PYTHON', 'PROFILES', 'HARDWARE', 'FLASH', 'FLASH_EXECUTOR'}
REDACTED = {'$redacted': True}
_LOCK = threading.RLock()


@contextmanager
def _disk_lock(path):
    """Serialize CAS commits even when two control processes share storage."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(str(path) + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b'\0')
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _bytes(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')


def _redact(value, key=''):
    if any(word in key.lower() for word in ('password', 'token', 'secret', 'private_key')):
        return deepcopy(REDACTED) if value else value
    if isinstance(value, dict):
        return {name: _redact(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _restore(value, old):
    if value == REDACTED:
        if old is None:
            raise SettingsError('隐藏字段没有现有值，请填写后再导入')
        return deepcopy(old)
    if isinstance(value, dict):
        previous = old if isinstance(old, dict) else {}
        return {key: _restore(item, previous.get(key)) for key, item in value.items()}
    if isinstance(value, list):
        previous = old if isinstance(old, list) else []
        return [_restore(item, previous[index] if index < len(previous) else None)
                for index, item in enumerate(value)]
    return value


def _atomic(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.settings-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _shape(record):
    if (not isinstance(record, dict) or type(record.get('version')) is not int or record.get('version') != 1
            or set(record) - {'version', 'environment', 'documents'}):
        raise SettingsError('配置格式必须为 version 1')
    env, docs = record.get('environment', {}), record.get('documents', {})
    if (not isinstance(env, dict) or set(env) - set(DEFAULTS)
            or not isinstance(docs, dict) or set(docs) - set(DOCUMENTS)):
        raise SettingsError('存在未知配置项')
    for name, value in env.items():
        if not isinstance(value, str) or len(value.encode('utf-8')) > 16384 or '\x00' in value:
            raise SettingsError('配置值无效：' + name)
        if name != 'PROBE_COMMAND' and ('\r' in value or '\n' in value):
            raise SettingsError('配置必须为单行：' + name)
        if name in CHOICES and value not in CHOICES[name]:
            raise SettingsError('选项无效：' + name)
    for name, document in docs.items():
        if not isinstance(document, dict) or len(_bytes(document)) > DOCUMENT_LIMITS[name]:
            raise SettingsError('配置文档类型或大小无效：' + name)
    return env, docs


def default_environment(environment):
    env = {name: environment.get('SERIALD_' + name, value) for name, value in DEFAULTS.items()}
    root = str(Path(__file__).resolve().parents[1])
    env['BASE'] = environment.get('SERIALD_BASE', str(Path(sys.executable).parent / 'data')
                                  if sys.platform == 'win32' else root)
    env['STATIC'] = environment.get('SERIALD_STATIC') or str(getattr(sys, '_MEIPASS', root))
    env['SOCKET'] = environment.get('SERIALD_SOCKET', str(Path(env['BASE']) / 'seriald.sock'))
    env['HANDOFF'] = environment.get('SERIALD_HANDOFF', str(Path(env['BASE']) / 'collector-handoff.json'))
    env['BY_PATH'] = environment.get('SERIALD_BY_PATH', 'Windows COM' if sys.platform == 'win32'
                                     else '/dev/serial/by-path')
    env['RESCUE_BIN'] = environment.get('SERIALD_RESCUE_BIN', '' if sys.platform == 'win32'
                                       else DEFAULTS['RESCUE_BIN'])
    env['CONSOLE_USER'] = environment.get('SERIALD_CONSOLE_USER', env['SSH_USER'])
    env['CONSOLE_PASSWORD'] = environment.get('SERIALD_CONSOLE_PASSWORD', env['SSH_PASSWORD'])
    env['AUTO_CONFIGURE_SSH'] = environment.get('SERIALD_AUTO_CONFIGURE_SSH', '1' if env['PROBE'] == 'linux' else '0')
    return env


class WebSettings:
    guard = _LOCK

    def __init__(self, path, environment=None):
        self.path = Path(path)
        self.environment = dict(os.environ if environment is None else environment)

    def read(self):
        try:
            with self.path.open('rb') as source:
                raw = source.read(2 * 1024 ** 2 + 1)
            if len(raw) > 2 * 1024 ** 2:
                raise SettingsError('配置超过 2 MiB')
            record = load_object(raw)
        except FileNotFoundError:
            record = {'version': 1, 'environment': {}, 'documents': {}}
        _shape(record)
        return record

    def effective(self, record=None):
        saved = self.read() if record is None else record
        # Calculate dependent defaults from the proposed environment, without
        # changing process-global os.environ while another request is running.
        source = dict(self.environment)
        source.update({'SERIALD_' + key: value for key, value in saved.get('environment', {}).items()})
        env = default_environment(source)
        documents = deepcopy(saved.get('documents', {}))
        for name, variable in DOCUMENTS.items():
            if name not in documents and env[variable]:
                try:
                    with open(env[variable], 'rb') as source:
                        raw = source.read(DOCUMENT_LIMITS[name] + 1)
                    if len(raw) > DOCUMENT_LIMITS[name]:
                        raise SettingsError('配置文档过大：' + name)
                    documents[name] = load_object(raw[3:] if raw.startswith(b'\xef\xbb\xbf') else raw)
                except (OSError, ValueError) as error:
                    raise SettingsError('无法读取配置文档：' + name) from error
        return {'version': 1, 'environment': env, 'documents': documents}

    def snapshot(self, secrets=False):
        saved = self.read()
        document = self.effective(saved)
        source_revision = {'saved': saved, 'external_documents': {key: value
                           for key, value in document['documents'].items()
                           if key not in saved.get('documents', {})}}
        revision = hashlib.sha256(_bytes(source_revision)).hexdigest()
        if not secrets:
            docs = document['documents']
            document['environment'] = {key: deepcopy(REDACTED) if key in SENSITIVE and value else value
                                       for key, value in document['environment'].items()}
            document['documents'] = {key: ({actor: deepcopy(REDACTED) for actor in value}
                if key == 'agent_tokens' else _redact(value)) for key, value in docs.items()}
        return {'revision': revision, 'configuration': document,
                'fields': [{'name': name, 'secret': name in SENSITIVE, 'path': name in PATHS,
                            'choices': CHOICES.get(name), 'restart': 'both' if name in SHARED else
                            'control' if name in CONTROL else 'collector'} for name in DEFAULTS],
                'apply': 'restart-required'}

    def validate(self, incoming):
        current = self.effective()
        document = _restore(incoming, current)
        env, docs = _shape(document)
        # Imports are patches; omission preserves existing settings, while an
        # explicit empty document clears its bindings. Never drop hidden secrets.
        combined = {'version': 1, 'environment': dict(current['environment'], **env),
                    'documents': dict(current['documents'], **docs)}
        merged, docs = combined['environment'], combined['documents']
        integer_limits = {'PORT': (1, 65535), 'LOG_RETENTION_DAYS': (0, 36500),
                          'DISK_MIN_FREE_MB': (0, 1048576), 'DISK_MIN_FREE_PERCENT': (0, 100),
                          'SCROLLBACK': (4096, 16 * 1024 ** 2), 'SSH_LIMIT': (1, 128),
                          'SSH_IDLE': (60, 86400), 'SHARE_SECONDS': (60, 30 * 86400)}
        for name, (minimum, maximum) in integer_limits.items():
            value = merged[name]
            if not re.fullmatch(r'[0-9]+', value) or not minimum <= int(value) <= maximum:
                raise SettingsError('数值范围无效：' + name)
        for name in PATHS - {'BY_PATH'}:
            if merged[name] and not os.path.isabs(merged[name]):
                raise SettingsError('路径必须为服务端绝对路径：' + name)
        if any(not merged[name] for name in ('BASE', 'STATIC', 'SOCKET', 'HANDOFF', 'BIND', 'BY_PATH')):
            raise SettingsError('服务路径和监听地址不能为空')
        from .profiles import BoardProfile, ProfileBook
        from .hardware import HardwareBook
        from .flash_runner import FlashBook
        from .transfer_artifacts import ArtifactPolicy
        full_environment = {'SERIALD_' + key: value for key, value in merged.items()}
        base = BoardProfile.from_env(full_environment)
        ProfileBook(docs.get('profiles', {}), base=base)
        HardwareBook(docs.get('hardware', {}))
        FlashBook(docs.get('flash', {}))
        ArtifactPolicy.from_env(full_environment)
        if 'agent_tokens' in docs:
            tokens = docs['agent_tokens']
            if (tokens and not merged['AUTH_TOKEN'] or any(
                    not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,79}', key) or key == 'web'
                    or not isinstance(value, str) or len(value) < 32 or value == merged['AUTH_TOKEN']
                    for key, value in tokens.items()) or len(set(tokens.values())) != len(tokens)):
                raise SettingsError('Agent 令牌配置无效')
        if len(_bytes(combined)) > 2 * 1024 ** 2:
            raise SettingsError('配置超过 2 MiB')
        return combined

    def save(self, incoming, revision, preview=False):
        with _LOCK, _disk_lock(self.path):
            before = self.snapshot(secrets=True)
            if revision != before['revision']:
                raise SettingsError('配置已被修改，请重新加载后再保存', 409)
            document = self.validate(incoming)
            changes = [name for name, value in document['environment'].items()
                       if before['configuration']['environment'].get(name) != value]
            changes += ['document:' + name for name, value in document['documents'].items()
                        if before['configuration']['documents'].get(name) != value]
            if not preview:
                _atomic(self.path, _bytes(document))
            return {'ok': True, 'changes': changes, 'apply': 'restart-required',
                    'preview': preview, 'revision': self.snapshot()['revision']}


def load_environment(path):
    """Run before configuration imports; corrupt saved configuration fails closed."""
    record = WebSettings(path).read()
    for key, value in record.get('environment', {}).items():
        os.environ['SERIALD_' + key] = value
    documents = record.get('documents', {})
    if documents:
        revision = hashlib.sha256(_bytes(record)).hexdigest()
        directory = Path(path).parent / 'web-settings' / revision
        for name, document in documents.items():
            target = directory / (name + '.json')
            data = _bytes(document)
            if not target.exists() or target.read_bytes() != data:
                _atomic(target, data)
            os.environ['SERIALD_' + DOCUMENTS[name]] = str(target)
    return record
