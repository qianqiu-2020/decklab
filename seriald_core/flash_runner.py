"""Configured local flashing capability and bounded JSONL process execution.

No browser input selects a host executable, USB attachment or loader path.  The
executor owns no UART; the existing collector remains its only reader.
"""
from contextlib import nullcontext
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time

from .hardware import PosixProcess
from .profiles import _unique_object
from .tasks import TaskError, _check_result_fields


MAX_LINE = 256 * 1024
MAX_OUTPUT = 64 * 1024 * 1024
MAX_REQUEST = 8 * 1024 * 1024
_IDENTIFIER = re.compile(r'[A-Za-z0-9_.:/ -]{1,160}\Z')


def safe_outcome(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    values = {'write_result': {'not-started', 'completed', 'partial', 'failed', 'unknown'},
              'verify_result': {'not-started', 'verified', 'failed', 'unavailable', 'not-requested'},
              'boot_result': {'not-requested', 'not-observed', 'observed', 'failed', 'unknown'}}
    for field, allowed in values.items():
        if isinstance(value.get(field), str) and value[field] in allowed:
            result[field] = value[field]
    for field in ('written_bytes', 'confirmed_bytes'):
        count = value.get(field)
        if type(count) is int and 0 <= count <= 2 ** 63 - 1:
            result[field] = count
    if 'written_bytes' not in result and 'confirmed_bytes' in result:
        result['written_bytes'] = result['confirmed_bytes']
    digest = value.get('plan_digest')
    if isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest):
        result['plan_digest'] = digest
    return result


class FlashRunnerError(TaskError):
    def __init__(self, message, status=502, cleanup_confirmed=True, outcome=None, code='', stage=''):
        self.code = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', code) else ''
        super().__init__(str(message) + (': ' + self.code if self.code else ''), status)
        self.cleanup_confirmed = cleanup_confirmed
        self.outcome = safe_outcome(outcome)
        self.stage = stage
        self.operation = stage


def _json(raw):
    def invalid(value):
        raise ValueError('non-finite JSON value')
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=invalid)


def _bounded_json(value, limit=65536):
    encoded = json.dumps(value, allow_nan=False).encode('utf-8')
    if len(encoded) > limit:
        raise ValueError('flash configuration value is too large')
    _check_result_fields(value)
    return value


@dataclass(frozen=True)
class BoardControl:
    provider: str = 'manual'
    node: str = 'local'
    endpoint: str = ''

    def public(self):
        return dict(provider=self.provider, node=self.node, endpoint=self.endpoint)


@dataclass(frozen=True)
class SerialEndpoint:
    provider: str = 'decklab-serial'
    node: str = 'local'
    endpoint: str = ''

    def public(self):
        return dict(provider=self.provider, node=self.node, endpoint=self.endpoint)


@dataclass(frozen=True)
class FlashBinding:
    serial: str
    binding_revision: int
    profile: str
    profile_data: dict
    control: BoardControl
    console: SerialEndpoint
    endpoint: str
    argv: tuple
    device: dict
    timeout: int = 3600
    allow_agent: bool = False
    max_expanded_bytes: int = 32 * 1024 ** 3
    max_cache_bytes: int = 64 * 1024 ** 3

    @property
    def resources(self):
        paths = {tuple(self.device['port_path'])}
        steps = [self.profile_data.get('download_device')] + self.profile_data.get('prepare', [])
        for step in steps:
            ports = step.get('port_path') if isinstance(step, dict) else None
            if (isinstance(ports, list) and 2 <= len(ports) <= 16
                    and all(type(port) is int and 1 <= port <= 65535 for port in ports)):
                paths.add(tuple(ports))
        return ['flash:local:' + self.endpoint] + [
            'flash-usb:local:' + '.'.join(map(str, ports)) for ports in sorted(paths)]

    def public(self):
        storages = []
        for storage in self.profile_data.get('storages', []):
            if not isinstance(storage, dict):
                continue
            identifier = storage.get('id')
            if not isinstance(identifier, str):
                continue
            label = storage.get('label')
            item = {'id': identifier, 'name': label if isinstance(label, str) else identifier, 'partitions': []}
            for partition in storage.get('partitions', []):
                if not isinstance(partition, dict) or not isinstance(partition.get('name'), str):
                    continue
                entry = {'id': partition['name'], 'name': partition['name']}
                if type(partition.get('size')) is int and partition['size'] >= 0:
                    entry['size'] = partition['size']
                item['partitions'].append(entry)
            storages.append(item)
        return {'board_serial': self.serial, 'binding_revision': self.binding_revision,
                'profile': self.profile, 'control': self.control.public(),
                'console': self.console.public(),
                'executor': {'provider': 'goflash', 'node': 'local', 'endpoint': self.endpoint},
                'modes': ['full', 'partitions'], 'verification': ['if-supported', 'required', 'none'],
                'storages': storages, 'layout_source': 'profile-unverified'}


def _endpoint(value, kind):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {'provider', 'node', 'endpoint'}:
        raise ValueError('invalid ' + kind + ' endpoint')
    default = 'manual' if kind == 'control' else 'decklab-serial'
    provider, node, endpoint = value.get('provider', default), value.get('node', 'local'), value.get('endpoint', '')
    allowed = ('manual',) if kind == 'control' else ('decklab-serial', 'none')
    if (provider not in allowed or node != 'local' or not isinstance(endpoint, str)
            or len(endpoint.encode()) > 100 or '\x00' in endpoint):
        raise ValueError('this release requires a local/manual ' + kind + ' endpoint')
    return (BoardControl if kind == 'control' else SerialEndpoint)(provider, node, endpoint)


class FlashBook:
    def __init__(self, document=None):
        document = {} if document is None else document
        if not isinstance(document, dict) or set(document) - {'boards'} or not isinstance(document.get('boards', {}), dict):
            raise ValueError('flash configuration requires a boards object')
        self.boards = {}
        for serial, value in document.get('boards', {}).items():
            if (not isinstance(serial, str) or not serial or len(serial.encode()) > 100
                    or not isinstance(value, dict) or set(value) - {
                        'binding_revision', 'profile', 'profile_data', 'control', 'console', 'flash'}):
                raise ValueError('invalid flash board configuration')
            revision, profile = value.get('binding_revision'), value.get('profile')
            data = value.get('profile_data', {})
            if (type(revision) is not int or not 1 <= revision <= 2 ** 53 - 1
                    or not isinstance(profile, str) or not profile or len(profile.encode()) > 100
                    or not isinstance(data, dict)):
                raise ValueError('flash binding requires a revision and profile')
            _bounded_json(data, 512 * 1024)
            flash = value.get('flash')
            if not isinstance(flash, dict) or set(flash) - {
                    'provider', 'node', 'endpoint', 'argv', 'device', 'timeout', 'allow_agent',
                    'max_expanded_bytes', 'max_cache_bytes'}:
                raise ValueError('invalid flash executor configuration')
            argv, device, endpoint = flash.get('argv'), flash.get('device'), flash.get('endpoint')
            if (flash.get('provider', 'goflash') != 'goflash' or flash.get('node', 'local') != 'local'
                    or not isinstance(endpoint, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', endpoint)
                    or not isinstance(argv, list) or not 1 <= len(argv) <= 64
                    or any(not isinstance(arg, str) or '\x00' in arg or len(arg.encode()) > 4096 for arg in argv)
                    or not os.path.isabs(argv[0])):
                raise ValueError('flash executor requires a local endpoint and absolute executable')
            if not isinstance(device, dict) or set(device) - {'port_path', 'usb_serial', 'vendor_id', 'product_id'}:
                raise ValueError('invalid flash USB selector')
            ports = device.get('port_path')
            if (not isinstance(ports, list) or not 2 <= len(ports) <= 16
                    or any(type(port) is not int or not 1 <= port <= (65535 if index == 0 else 255)
                           for index, port in enumerate(ports))):
                raise ValueError('flash USB selector requires an exact bus and port path')
            for field in ('vendor_id', 'product_id'):
                if field in device and (type(device[field]) is not int or not 0 <= device[field] <= 65535):
                    raise ValueError('invalid USB identifier')
            if 'usb_serial' in device and (not isinstance(device['usb_serial'], str)
                    or not device['usb_serial'] or len(device['usb_serial'].encode()) > 128):
                raise ValueError('invalid USB serial selector')
            timeout, agent = flash.get('timeout', 3600), flash.get('allow_agent', False)
            expanded, cache = flash.get('max_expanded_bytes', 32 * 1024 ** 3), flash.get('max_cache_bytes', 64 * 1024 ** 3)
            if (type(timeout) is not int or not 1 <= timeout <= 86400 or type(agent) is not bool
                    or any(type(limit) is not int or not 1 <= limit <= 2 ** 63 - 1 for limit in (expanded, cache))
                    or cache < expanded):
                raise ValueError('invalid flash executor limits')
            self.boards[serial] = FlashBinding(serial, revision, profile, data,
                _endpoint(value.get('control'), 'control'), _endpoint(value.get('console'), 'console'),
                endpoint, tuple(argv), dict(device), timeout, agent, expanded, cache)

    @classmethod
    def from_env(cls):
        path = os.environ.get('SERIALD_FLASH')
        if not path:
            return cls()
        with open(path, 'rb') as source:
            raw = source.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('flash configuration exceeds 1 MiB')
        return cls(_json(raw.decode('utf-8-sig')))

    def binding(self, serial, actor='web'):
        binding = self.boards.get(serial)
        if binding is None:
            raise TaskError('flashing is not configured for this board', 404)
        if actor != 'web' and not binding.allow_agent:
            raise TaskError('flashing is not enabled for agents', 403)
        return binding

    def available(self, serial, actor='web'):
        try:
            binding = self.binding(serial, actor)
        except TaskError as error:
            return {'supported': False, 'reason': str(error), 'board_serial': serial}
        try:
            mode = os.stat(binding.argv[0]).st_mode
        except FileNotFoundError:
            return dict(binding.public(), supported=False, reason='configured flashing executor is missing')
        except OSError:
            return dict(binding.public(), supported=False, reason='configured flashing executor is inaccessible')
        if not stat.S_ISREG(mode) or not os.access(binding.argv[0], os.X_OK):
            return dict(binding.public(), supported=False, reason='configured flashing executor is not executable')
        return dict(binding.public(), supported=True, reset_required=False)


class _PosixStreamProcess(PosixProcess):
    def __init__(self, argv):
        self.process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        self.stdout = self.process.stdout


def stream_process(argv):
    if os.name == 'nt':
        from .process_windows import WindowsProcess
        return WindowsProcess(argv, capture_stdout=True)
    if os.name == 'posix':
        return _PosixStreamProcess(argv)
    raise FlashRunnerError('unsupported flashing process platform', 503)


def safe_event(event):
    """Keep only typed domain fields; never persist arbitrary helper diagnostics."""
    if not isinstance(event, dict):
        raise FlashRunnerError('invalid flashing event')
    output = {}
    for field in ('event', 'phase', 'storage', 'partition', 'status', 'code'):
        value = event.get(field)
        if value is not None:
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise FlashRunnerError('invalid flashing event field')
            output[field] = value
    for field in ('done', 'total'):
        if field in event:
            value = event[field]
            if type(value) is not int or not 0 <= value <= 2 ** 63 - 1:
                raise FlashRunnerError('invalid flashing progress')
            output[field] = value
    if 'done' in output and 'total' in output and output['done'] > output['total']:
        raise FlashRunnerError('invalid flashing progress range')
    return output


def public_plan(value):
    """Strip implementation paths and device selectors from the domain preview."""
    hidden = {'path', 'workspace', 'argv', 'device', 'profile', 'profile_data', 'prepare',
              'cache_path', 'source_path', 'image_path', 'request_path', 'files'}
    if isinstance(value, dict):
        return {key: public_plan(item) for key, item in value.items()
                if key not in hidden and not key.endswith('_path')}
    if isinstance(value, list):
        return [public_plan(item) for item in value]
    return value


class LocalFlashExecutor:
    def __init__(self, work_dir):
        self.root = Path(work_dir).absolute()
        self._cache_lock = threading.Lock()
        self._cache_reserved = 0
        self._active_workspaces = set()

    @staticmethod
    def _cache_size(path):
        size = 0
        for current, directories, files in os.walk(path, followlinks=False):
            for name in directories + files:
                try:
                    info = os.lstat(os.path.join(current, name))
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    raise FlashRunnerError('flash cache contains an unexpected link', 503)
                if stat.S_ISREG(info.st_mode):
                    size += info.st_size
        return size

    def cleanup_plan(self, plan_or_workspace):
        """Remove an expired, unreferenced workspace selected by the task store.

        The caller must first exclude all active tasks and recovery holds. This
        method independently rejects foreign paths, links and in-flight import.
        """
        workspace = (plan_or_workspace.get('workspace') if isinstance(plan_or_workspace, dict)
                     else plan_or_workspace)
        if not isinstance(workspace, (str, os.PathLike)):
            return False
        target = Path(workspace).absolute()
        cache = (self.root / 'cache').resolve()
        if target.parent.resolve() != cache or not re.fullmatch(r'plan-[A-Za-z0-9_-]{8}', target.name):
            raise TaskError('flash workspace is outside managed cache', 409)
        with self._cache_lock:
            if str(target) in self._active_workspaces:
                raise TaskError('flash workspace is still being prepared', 409)
            try:
                mode = os.lstat(target).st_mode
            except FileNotFoundError:
                return False
            if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                raise TaskError('flash workspace is not a regular managed directory', 409)
            shutil.rmtree(target)
            return True

    def _directory(self, path):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise FlashRunnerError('flashing workspace is not a regular directory', 503)
        if os.name == 'posix':
            os.chmod(path, 0o700)
        return path

    def _request(self, binding):
        return {'version': 1, 'board_serial': binding.serial, 'profile': binding.profile_data,
                'endpoint': binding.endpoint, 'device': binding.device,
                'max_expanded_bytes': binding.max_expanded_bytes, 'max_cache_bytes': binding.max_cache_bytes}

    def inspect(self, binding, check=lambda: None):
        return self._run(binding, 'inspect', self._request(binding), check=check, timeout=min(binding.timeout, 60))

    def prepare(self, binding, on_event=None, check=lambda: None, task_id='', attempt_id=''):
        request = dict(self._request(binding), task_id=task_id, attempt_id=attempt_id)
        return self._run(binding, 'prepare', request, on_event, check)

    def plan(self, binding, artifact, input_mode, selections=None, verify='if-supported', check=lambda: None,
             target_storage=None, inspection_scope=None):
        if input_mode not in ('full', 'partitions') or verify not in ('none', 'if-supported', 'optional', 'required'):
            raise TaskError('invalid flash mode or verification policy')
        if not isinstance(artifact, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(artifact.get('sha256', ''))):
            raise TaskError('a sealed image artifact is required')
        if target_storage is not None and (not isinstance(target_storage, str)
                or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', target_storage)):
            raise TaskError('invalid flash target storage')
        cache = self._directory(self.root / 'cache')
        with self._cache_lock:
            free = max(0, shutil.disk_usage(cache).free - 256 * 1024 ** 2 - self._cache_reserved)
            budget = min(free, max(0, binding.max_cache_bytes - self._cache_size(cache) - self._cache_reserved),
                         binding.max_expanded_bytes + artifact.get('size', 0))
            if budget < max(1, artifact.get('size', 0)):
                raise TaskError('insufficient free space or cache quota for flash image preparation', 503)
            workspace = Path(tempfile.mkdtemp(prefix='plan-', dir=cache))
            self._cache_reserved += budget
            self._active_workspaces.add(str(workspace))
        request = dict(self._request(binding), artifact=dict(artifact), mode=input_mode,
                       selections=selections or [], verify='if-supported' if verify == 'optional' else verify,
                       workspace=str(workspace), max_cache_bytes=budget,
                       max_expanded_bytes=min(binding.max_expanded_bytes, budget))
        if target_storage is not None:
            request['target_storage'] = target_storage
        stage = 'image-plan'
        try:
            image = self._run(binding, 'image-plan', request, check=check)
            if (not isinstance(image.get('plan'), dict)
                    or not isinstance(image.get('payload_candidates', []), list)):
                raise FlashRunnerError('flashing executor returned an invalid image plan')
            inspection_request = dict(request, plan=image['plan'], payload_candidates=image.get('payload_candidates', []))
            stage = 'check-plan'
            scope = inspection_scope() if inspection_scope is not None else nullcontext()
            with scope:
                result = self._run(binding, 'check-plan', inspection_request, check=check)
            result['workspace'] = str(workspace)
            return result
        except BaseException as error:
            if not getattr(error, 'stage', ''):
                error.stage = stage
            if not getattr(error, 'operation', ''):
                error.operation = stage
            if not getattr(error, 'cleanup_confirmed', True):
                # An unconfirmed process can still be using imported payloads.
                # Preserve both its resource hold and the only cache reference.
                error.workspace = str(workspace)
            else:
                shutil.rmtree(workspace)
            raise
        finally:
            with self._cache_lock:
                self._cache_reserved -= budget
                self._active_workspaces.discard(str(workspace))

    def execute(self, binding, plan, on_event=None, check=lambda: None, task_id='', attempt_id=''):
        request = dict(self._request(binding), plan=plan, task_id=task_id, attempt_id=attempt_id)
        return self._run(binding, 'execute', request, on_event, check)

    def check_plan(self, binding, plan, payload_candidates=None, check=lambda: None, selections=None):
        """Bind cached payloads to the live layout without importing them again.

        The coordinator already owns the board maintenance lease at this point.
        A new selection may only reference the sealed image's cached candidates;
        the engine verifies that restriction and the original intent digest.
        """
        if not isinstance(plan, dict) or not isinstance(plan.get('artifact'), dict):
            raise TaskError('a prepared image plan is required')
        candidates = [] if payload_candidates is None else payload_candidates
        chosen = plan.get('selections', []) if selections is None else selections
        if not isinstance(candidates, list) or not isinstance(chosen, list):
            raise TaskError('invalid cached flash plan selections')
        request = dict(self._request(binding), plan=plan, artifact=plan['artifact'],
                       mode=plan.get('mode'), verify=plan.get('verify', 'if-supported'),
                       selections=chosen, payload_candidates=candidates)
        if plan.get('target_storage'):
            request['target_storage'] = plan['target_storage']
        if selections is not None and selections != plan.get('selections', []):
            request['selection_update'] = True
        return self._run(binding, 'check-plan', request, check=check)

    def _run(self, binding, operation, request, on_event=None, check=lambda: None, timeout=None):
        encoded = json.dumps(request, allow_nan=False, separators=(',', ':')).encode('utf-8')
        if len(encoded) > MAX_REQUEST:
            raise TaskError('flash request is too large', 413)
        check()
        directory = self._directory(self.root / 'requests')
        descriptor, path = tempfile.mkstemp(prefix='flash-', suffix='.json', dir=directory)
        process, reader, stopping = None, None, threading.Event()
        cleanup_attempted = False
        outcome = {}
        events = queue.Queue(maxsize=32)
        try:
            with os.fdopen(descriptor, 'wb') as target:
                target.write(encoded)
            process = stream_process(list(binding.argv) + [operation, '--request', path])
            def put(value):
                while not stopping.is_set():
                    try:
                        events.put(value, timeout=0.05)
                        return
                    except queue.Full:
                        pass
            def read():
                total = 0
                try:
                    while not stopping.is_set():
                        line = process.stdout.readline(MAX_LINE + 1)
                        if not line:
                            break
                        total += len(line)
                        if len(line) > MAX_LINE or total > MAX_OUTPUT:
                            raise FlashRunnerError('flashing event stream exceeded its limit')
                        put(line)
                except BaseException as error:
                    put(error)
                finally:
                    put(None)
            reader = threading.Thread(target=read, name='flash-events', daemon=True)
            reader.start()
            deadline, result, ended = time.monotonic() + (timeout or binding.timeout), None, False
            root_exited = None
            while True:
                check()
                if time.monotonic() >= deadline:
                    raise FlashRunnerError('flashing executor timed out; write outcome may be partial', 504)
                if process.poll() is not None:
                    if root_exited is None:
                        root_exited = time.monotonic()
                    # Drain the root's final receipt before cleaning remaining
                    # children. A descendant holding stdout cannot postpone
                    # cleanup indefinitely, and queued receipts remain bounded.
                    if (not cleanup_attempted and (ended or
                            time.monotonic() - root_exited >= 0.1 and events.empty())):
                        cleanup_attempted = True
                        try:
                            process.close()
                        except BaseException as error:
                            raise FlashRunnerError('flashing process cleanup could not be confirmed', 503, False, outcome,
                                                   stage=operation) from error
                    if ended and cleanup_attempted:
                        break
                try:
                    line = events.get(timeout=0.05)
                except queue.Empty:
                    # A successful root can leave a child holding stdout. Owned
                    # tree cleanup below is still required before success.
                    if process.poll() is not None and events.empty() and not reader.is_alive():
                        break
                    continue
                if line is None:
                    ended = True
                    continue
                if isinstance(line, BaseException):
                    raise line
                try:
                    event = _json(line.decode('utf-8'))
                except (ValueError, UnicodeError):
                    raise FlashRunnerError('flashing executor returned invalid JSON') from None
                if not isinstance(event, dict) or event.get('event') not in ('progress', 'phase', 'result', 'error'):
                    raise FlashRunnerError('flashing executor returned an invalid event')
                if event['event'] == 'error':
                    outcome.update(safe_outcome(event.get('result')))
                    raise FlashRunnerError('flashing executor reported failure', outcome=outcome, code=event.get('code'))
                if result is not None:
                    raise FlashRunnerError('flashing executor emitted events after its result')
                if event['event'] == 'result':
                    result = event.get('result')
                    if not isinstance(result, dict) or len(json.dumps(result).encode()) > MAX_REQUEST:
                        raise FlashRunnerError('flashing executor returned an invalid result')
                    outcome.update(safe_outcome(result))
                    try:
                        _check_result_fields(result)
                    except TaskError:
                        raise FlashRunnerError('flashing executor returned disallowed result fields', outcome=outcome) from None
                    if result.get('ok') is False or result.get('status') in ('failed', 'error', 'FAIL'):
                        raise FlashRunnerError('flashing executor reported an unsuccessful result', outcome=outcome)
                else:
                    safe = safe_event(event)
                    if on_event:
                        on_event(safe)
            check()
            if process.returncode:
                raise FlashRunnerError('flashing executor exited with status %s; write outcome may be partial' % process.returncode,
                                       outcome=outcome)
            if result is None:
                raise FlashRunnerError('flashing executor exited without a result')
            if operation == 'execute':
                if result.get('write_result') != 'completed':
                    raise FlashRunnerError('flashing executor did not confirm completed writes', outcome=outcome)
                if request.get('plan', {}).get('verify') == 'required' and result.get('verify_result') != 'verified':
                    raise FlashRunnerError('required flash verification was not confirmed', outcome=outcome)
            return result
        except BaseException as error:
            # Cancellation or a transport exception can race a confirmed final
            # write result; preserve that evidence without inferring success.
            if outcome and not getattr(error, 'outcome', None):
                error.outcome = dict(outcome)
            if not getattr(error, 'stage', ''):
                error.stage = operation
            if not getattr(error, 'operation', ''):
                error.operation = operation
            raise
        finally:
            stopping.set()
            try:
                if process is not None and not cleanup_attempted:
                    cleanup_attempted = True
                    try:
                        process.close()
                    except BaseException as error:
                        raise FlashRunnerError('flashing process cleanup could not be confirmed', 503, False, outcome,
                                               stage=operation) from error
            finally:
                if reader is not None:
                    reader.join(3)
                if process is not None and process.stdout is not None and not (reader and reader.is_alive()):
                    process.stdout.close()
                Path(path).unlink(missing_ok=True)
                if reader is not None and reader.is_alive():
                    raise FlashRunnerError('flashing event reader cleanup could not be confirmed', 503, False, outcome,
                                           stage=operation)
