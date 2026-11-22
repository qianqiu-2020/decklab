"""Configured local flashing capability and bounded JSONL process execution.

No browser input selects a host executable, USB attachment or loader path.  The
executor owns no UART; the existing collector remains its only reader.
"""
from contextlib import nullcontext, contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import secrets
import stat
import struct
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace

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
              'verify_result': {'not-started', 'verified', 'failed', 'unavailable', 'not-requested', 'skipped'},
              'boot_result': {'not-requested', 'not-observed', 'observed', 'failed', 'unknown', 'reset-command-acknowledged'}}
    for field, allowed in values.items():
        if isinstance(value.get(field), str) and value[field] in allowed:
            result[field] = value[field]
    for field in ('written_bytes', 'confirmed_bytes', 'verified_bytes', 'readback_bytes'):
        count = value.get(field)
        if type(count) is int and 0 <= count <= 2 ** 63 - 1:
            result[field] = count
    if 'written_bytes' not in result and 'confirmed_bytes' in result:
        result['written_bytes'] = result['confirmed_bytes']
    if value.get('verification_scope') == 'payloads-and-outside-selected-partitions':
        result['verification_scope'] = value['verification_scope']
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


class ImagePreparationQuotaError(FlashRunnerError):
    def __init__(self, reclaim_bytes=0):
        super().__init__('镜像准备缓存空间不足。可从「已下载」删除不用的镜像后重试，或使用 URL 极速刷机。',
                         503, code='image-cache-quota')
        self.reclaim_bytes = reclaim_bytes


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
    target_id: str = ''
    target_generation: str = ''

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
                'chip': self.profile_data.get('chip_family', ''),
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
            if not isinstance(device, dict) or set(device) - {'port_path', 'usb_serial', 'vendor_id', 'product_id', 'usb_address'}:
                raise ValueError('invalid flash USB selector')
            if 'usb_address' in device and (type(device['usb_address']) is not int or not 1 <= device['usb_address'] <= 127):
                raise ValueError('invalid USB connection address')
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
              'cache_path', 'source_path', 'source_proofs', 'image_path', 'request_path', 'files',
              'execution_profile', 'execution_device', 'initial_device', 'previous_device', 'final_device',
              'allowed_final_generation', 'source_workspaces', 'steps', 'base_profile'}
    if isinstance(value, dict):
        return {key: public_plan(item) for key, item in value.items()
                if key not in hidden and not key.endswith('_path')}
    if isinstance(value, list):
        return [public_plan(item) for item in value]
    return value


def _with_configuration(result, configuration):
    """Record which measured configuration, if any, a write used."""
    if not isinstance(result, dict):
        return result
    summary = None
    if configuration:
        summary = {key: configuration.get(key) for key in ('measured_at', 'scope', 'port', 'board_serial')}
        summary['tuning'] = configuration.get('tuning')
    return dict(result, flash_configuration=summary)


class LocalFlashExecutor:
    supports_nor_snapshot = True
    supports_reset_after = True
    supports_storage_metadata = True
    supports_skip_verification = True
    supports_image_preparation = True
    supports_stream_flash = True
    supports_calibrate = True

    def __init__(self, work_dir):
        # Canonicalize the administrator-selected root once. On Windows, TEMP
        # may use an 8.3 alias; comparing it with GetFinalPathName output would
        # otherwise reject genuine managed files as escaped loader paths.
        self.root = Path(work_dir).resolve()
        self._cache_lock = threading.Lock()
        self._cache_reserved = 0
        self._active_workspaces = set()
        self._image_locks = {}

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
        if target.parent.resolve() == (self.root / 'images').resolve() and re.fullmatch(r'[0-9a-f]{64}', target.name):
            return False
        if target.parent.resolve() != cache or not re.fullmatch(r'plan-[A-Za-z0-9_-]{8}', target.name):
            raise TaskError('flash workspace is outside managed cache', 409)
        with self._cache_lock:
            if str(target.resolve()) in self._active_workspaces:
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
        return path.resolve()

    def _request(self, binding):
        request = {'version': 1, 'board_serial': binding.serial, 'profile': binding.profile_data,
                'endpoint': binding.endpoint, 'device': binding.device,
                'max_expanded_bytes': binding.max_expanded_bytes, 'max_cache_bytes': binding.max_cache_bytes}
        if binding.target_id:
            request.update(target_id=binding.target_id, target_generation=binding.target_generation)
        return request

    def discover(self, argv, probe=False, device=None, check=lambda: None, link_token=None):
        request = {'version': 1, 'probe': probe}
        if device is not None:
            request['device'] = device
        if link_token:
            request['link_token'] = link_token
        return self._run(SimpleNamespace(argv=tuple(argv), timeout=20 if probe else 10),
                         'discover', request, check=check)

    def stream_flash(self, binding, request, on_event=None, check=lambda: None):
        """Fast mode; the executor downloads, decompresses and writes in memory."""
        configuration = self.tuning(binding)
        if configuration:
            request = dict(request, tuning=configuration['tuning'])
        elif request.get('tuning') is not None:
            from . import flash_tuning
            tuning = flash_tuning.normalize_tuning(request['tuning'])
            if tuning is None:
                raise TaskError('invalid flash configuration')
            request = dict(request, tuning=tuning)
        result = self._run(binding, 'stream-flash', request, on_event, check, timeout=4 * 3600)
        return _with_configuration(result, configuration)

    def calibrate(self, binding, request, on_event=None, check=lambda: None):
        """Measure this board connection and keep the derived configuration."""
        from . import flash_tuning
        result = self._run(binding, 'calibrate', request, on_event, check, timeout=600)
        return dict(result, configuration=flash_tuning.record(self.root, binding, result))

    def tuning(self, binding):
        """The measured configuration later K3 flashes of this connection use."""
        from . import flash_tuning
        if binding.profile_data.get('id') != 'auto-spacemit-gpt-v1' or binding.profile_data.get('chip_family') != 'k3':
            return None
        return flash_tuning.lookup(self.root, binding)

    def forget_tuning(self, binding):
        from . import flash_tuning
        from .flash_loaders import discard_training
        cleared = discard_training(binding, self.root)
        return flash_tuning.forget(self.root, binding) or cleared

    def stream_loader_workspace(self):
        return self.root / 'images' / 'stream'

    def inspect(self, binding, check=lambda: None):
        return self._run(binding, 'inspect', self._request(binding), check=check, timeout=min(binding.timeout, 60))

    def ram_boot(self, binding, serial, workspace=None, files=None, on_event=None, check=lambda: None,
                 *, image_sha256=None):
        profile = dict(binding.profile_data)
        profile.pop('download_device', None)
        request = dict(self._request(binding), profile=profile, ram_boot={'serial': serial})
        if workspace is not None:
            request['workspace'] = str(workspace)
            request['ram_boot']['files'] = {
                name: dict(item, path=str(workspace / name)) for name, item in files.items()
            }
            if binding.profile_data.get('chip_family') == 'k1':
                request['ram_boot']['image_sha256'] = image_sha256
        return self._run(binding, 'ram-boot', request, on_event, check, timeout=1800 if files else 60)

    def prepare(self, binding, on_event=None, check=lambda: None, task_id='', attempt_id=''):
        request = dict(self._request(binding), task_id=task_id, attempt_id=attempt_id)
        return self._run(binding, 'prepare', request, on_event, check)

    def execution_binding(self, binding, profile, workspace, check=lambda: None):
        """Accept loader paths produced by this host's bounded package import.

        Device-returned profiles cannot use this entry point: only the fixed
        automatic SpacemiT template may gain the four loader preparation steps.
        The coordinator keeps the original binding digest for attachment checks.
        """
        if (not binding.target_id or not isinstance(profile, dict)
                or binding.profile_data.get('id') != 'auto-spacemit-gpt-v1'
                or binding.profile_data.get('chip_family') not in ('k1', 'k3')):
            raise FlashRunnerError('loader preparation requires a detected SpacemiT target')
        try:
            _bounded_json(profile, 512 * 1024)
        except (TypeError, ValueError) as error:
            raise FlashRunnerError('invalid prepared execution profile') from error
        baseline = json.loads(json.dumps(binding.profile_data))
        candidate = json.loads(json.dumps(profile))
        system_storage = candidate.pop('system_storage', '')
        original_system = baseline.pop('system_storage', '')
        if system_storage and (system_storage not in ('auto', 'active') and not re.fullmatch(r'(nvme|scsi|mmc|usb):[0-3]', system_storage)):
            raise FlashRunnerError('invalid prepared system storage')
        if original_system and original_system != system_storage:
            raise FlashRunnerError('prepared system storage changed')
        baseline_protocol = baseline.pop('ram_loader_protocol', None)
        protocol = candidate.pop('ram_loader_protocol', None)
        from .flash_loaders import RAM_BOOT_PROTOCOLS
        protocols = ('stock', 'spacemit-ram-v1')
        if baseline.get('bootrom_identity'):
            protocols += (RAM_BOOT_PROTOCOLS[baseline['chip_family']],)
        if protocol is not None and protocol not in protocols:
            raise FlashRunnerError('unsupported RAM loader protocol')
        if baseline_protocol and baseline_protocol != protocol:
            raise FlashRunnerError('prepared execution profile changed RAM loader protocol')
        # Go serializes an absent identity map as null. Both mean no extra
        # checks; the detected chip and BootROM checks must still match below.
        baseline.setdefault('identity', None)
        candidate.setdefault('identity', None)
        steps = candidate.pop('prepare', None)
        original_steps = baseline.pop('prepare', None)
        live_catalogue = (not baseline.get('bootrom_identity') and not original_steps and not steps
                          and (baseline_protocol == protocol == 'spacemit-ram-v1'
                               or baseline.get('chip_family') == 'k1' and baseline_protocol is None and protocol is None))
        k1_active_system = (system_storage == 'active' and candidate.get('chip_family') == 'k1'
                            and protocol in (None, 'stock'))
        if not live_catalogue and (not isinstance(steps, list) or len(steps) != 4
                or [step.get('kind') if isinstance(step, dict) else None for step in steps]
                    != ['stage', 'continue', 'stage', 'continue']):
            raise FlashRunnerError('prepared loader steps differ from the fixed execution sequence')
        # The importer's one package table is fixed, never a user-supplied path.
        storages, original_storages = candidate.get('storages'), baseline.get('storages')
        nor_catalogue = {'id': 'active', 'label': 'NOR (package payload catalogue)',
            'layout_source': 'spacemit-gpt', 'size_getvar': '__decklab_gpt_capacity',
            'sector_size': 512, 'partitions': [], 'package_tables': ['partition_nor.json'], 'sparse': False}
        if system_storage:
            if ((not k1_active_system and (protocol != 'spacemit-ram-v1' or system_storage == 'active'))
                    or not isinstance(storages, list) or len(storages) != 1
                    or storages[0].get('package_tables') != ['partition_universal.json']):
                raise FlashRunnerError('system storage requires the fixed RAM OS catalogue')
            if not storages[0].get('sparse'):
                raise FlashRunnerError('system payload transfer capability missing')
            storages[0]['sparse'] = original_storages[0].get('sparse', False)
            if (storages[0].get('layout_target') == 'gpt'
                    and (k1_active_system and storages[0].get('sector_size') == 512
                         or protocol == 'spacemit-ram-v1' and candidate.get('chip_family') == 'k3')):
                storages[0].pop('layout_target')
                if original_storages[0].get('layout_target') == 'gpt':
                    original_storages[0].pop('layout_target')
                if k1_active_system:
                    storages[0]['sector_size'] = original_storages[0]['sector_size']
        if protocol == 'spacemit-ram-v1' and storages == [nor_catalogue]:
            candidate['storages'] = baseline['storages']
        if (isinstance(storages, list) and len(storages) == 1 and isinstance(storages[0], dict)
                and isinstance(original_storages, list) and len(original_storages) == 1
                and isinstance(original_storages[0], dict)
                and storages[0].get('package_tables') == ['partition_universal.json']
                and not original_storages[0].get('package_tables')):
            storages[0].pop('package_tables')
            original_storages[0].pop('package_tables', None)
        if (isinstance(storages, list) and len(storages) == 1
                and storages[0] == {'id': 'active', 'label': 'MTD (NOR firmware package)',
                    'layout_source': 'spacemit-nor', 'size_getvar': 'mtd-size', 'sector_size': 512,
                    'partitions': None, 'package_tables': ['partition_nor.json'],
                    'layout_target': 'mtd', 'sparse': False}):
            if candidate.get('nor_fill_tail') is True:
                candidate.pop('nor_fill_tail')
                baseline.pop('nor_fill_tail', None)
            # This template is produced by host image import, never by USB.
            # Keep original chip/BootROM checks and validate cached loaders below.
            candidate['storages'] = baseline['storages']
        if candidate != baseline:
            raise FlashRunnerError('prepared execution profile changed device capabilities or identity')
        if not isinstance(workspace, (str, os.PathLike)):
            raise FlashRunnerError('prepared loaders require a managed image workspace')
        target = Path(workspace).absolute()
        cache = (self.root / 'cache').absolute()
        if (not (target.parent == cache and re.fullmatch(r'plan-[A-Za-z0-9_-]{8}', target.name)
                or target.parent == (self.root / 'images').absolute() and re.fullmatch(r'[0-9a-f]{64}', target.name))
                or target.is_symlink() or not target.is_dir() or cache.is_symlink()
                or target.resolve() != target):
            raise FlashRunnerError('prepared loader workspace is outside the managed image cache')
        for index, step in enumerate(steps or []):
            check()
            if index in (1, 3):
                if set(step) != {'kind'}:
                    raise FlashRunnerError('prepared continue step contains unsupported fields')
                continue
            if (set(step) != {'kind', 'path', 'sha256'} or not isinstance(step['path'], str)
                    or not isinstance(step['sha256'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', step['sha256'])):
                raise FlashRunnerError('invalid prepared loader reference')
            path = Path(step['path'])
            inherited = (protocol == 'spacemit-ram-v1' and isinstance(original_steps, list)
                         and len(original_steps) == 4 and step == original_steps[index])
            if not path.is_absolute():
                raise FlashRunnerError('prepared loader is outside the managed image workspace')
            if not path.is_relative_to(target):
                if not inherited:
                    raise FlashRunnerError('prepared loader is outside the managed image workspace')
                # A package imported into an already running RAM workspace may
                # reuse its exact previously checked loader files. They remain
                # owned by that task and must still be in our managed cache.
                inherited_workspace = next((parent for parent in path.parents if parent.parent in (cache, (self.root / 'images').absolute())), None)
                if inherited_workspace is None:
                    raise FlashRunnerError('inherited loader left the managed cache')
                self._owned_workspace(inherited_workspace)
            expected = 'FSBL.bin' if index == 0 else 'u-boot.itb'
            if path.name != expected or index == 0 and path.parent.name != 'factory':
                raise FlashRunnerError('prepared loader does not match the fixed image member')
            for component in (path, *path.parents):
                if component == target:
                    break
                if component.is_symlink():
                    raise FlashRunnerError('prepared loader path contains a symbolic link')
            if not path.is_file() or path.resolve() != path:
                raise FlashRunnerError('prepared loader is not a regular managed file')
            digest = hashlib.sha256()
            with path.open('rb') as source:
                while True:
                    check()
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            if digest.hexdigest() != step['sha256']:
                raise FlashRunnerError('prepared loader checksum changed')
        return replace(binding, profile_data=json.loads(json.dumps(profile)))

    def _payload_metadata(self, result, workspace):
        """Read bounded NOR payloads, retaining paths only in the fixed Go plan."""
        from .archive_io import open_archive
        from .nor_contents import MAX_PARTITION_BYTES, partition_content
        root = self._owned_workspace(workspace)
        summary = root / 'image-metadata.json'
        cached = None
        roots = [root] + [self._owned_workspace(value) for value in result.get('source_workspaces', [])]
        if root.parent == (self.root / 'images').resolve() and summary.is_file():
            if summary.is_symlink() or summary.stat().st_size > MAX_REQUEST:
                raise FlashRunnerError('invalid image metadata cache')
            cached = _json(summary.read_bytes()).get('payload_metadata', [])
        for other in roots[1:]:
            summary = other / 'image-metadata.json'
            if summary.is_file() and not summary.is_symlink() and summary.stat().st_size <= MAX_REQUEST:
                cached = (cached or []) + _json(summary.read_bytes()).get('payload_metadata', [])
        metadata, seen = [], set()
        for value in list((result.get('plan') or {}).get('writes') or []) + list(result.get('payload_candidates') or []):
            path = Path(value.get('path', ''))
            payload_root = next((candidate for candidate in roots if path.is_absolute() and path.is_relative_to(candidate)), root)
            part = value.get('partition')
            hit = next((item for item in cached or [] if item.get('sha256') == value.get('sha256')
                        and (item.get('partition') == part or item.get('partition') == ''
                             and item.get('content', {}).get('filesystem'))), None)
            if hit is not None:
                key = (value.get('storage'), part, value.get('sha256'))
                if key not in seen:
                    metadata.append(dict(hit, storage=value.get('storage'), partition=part))
                    seen.add(key)
                continue
            if part not in ('bootinfo', 'fsbl', 'env', 'esos', 'opensbi', 'uboot'):
                if value.get('kind') in ('partition', 'disk') and value.get('path') and len(metadata) < 128:
                    item = self._os_payload_metadata(value, payload_root, seen)
                    if item:
                        metadata.append(item)
                continue
            key = (value.get('storage'), part, value.get('sha256'))
            if key in seen or len(metadata) >= 128:
                continue
            seen.add(key)
            path = Path(value.get('path', ''))
            if (not path.is_absolute() or not path.is_relative_to(payload_root)
                    or path.resolve(strict=True) != path):
                raise FlashRunnerError('NOR payload metadata escaped its managed workspace')
            size = value.get('size')
            if type(size) is not int or not 0 < size <= MAX_PARTITION_BYTES:
                continue
            with open_archive(path) as source:
                data = source.read(MAX_PARTITION_BYTES + 1)
            if len(data) != size:
                raise FlashRunnerError('NOR payload size changed during metadata read')
            content = partition_content(data, part)
            if content['sha256'] != value.get('sha256'):
                raise FlashRunnerError('NOR payload checksum changed during metadata read')
            content['sha256_scope'] = 'payload'
            metadata.append({'storage': value.get('storage'), 'partition': part,
                             'sha256': content['sha256'], 'content': content})
        return metadata

    @staticmethod
    def _os_payload_metadata(value, root, seen):
        """Filesystem/OS metadata for large partition or disk payloads without hashing them again."""
        from .archive_io import open_archive
        from .os_contents import ImageError, image_content
        key = (value.get('storage'), value.get('partition') or value.get('target') or '', value.get('sha256'))
        if key in seen:
            return None
        seen.add(key)
        path = Path(value.get('path', ''))
        # Descriptive only: payloads the executor streams from elsewhere are
        # simply not inspected, so full-image plans never fail on metadata.
        try:
            if (not path.is_absolute() or not path.is_relative_to(root)
                    or path.resolve(strict=True) != path):
                return None
        except OSError:
            return None
        size = value.get('size')
        if type(size) is not int or size <= 0:
            return None
        with open_archive(path) as source:
            before = os.fstat(source.fileno())
            if before.st_size != size:
                raise FlashRunnerError('OS payload size changed during metadata read')
            try:
                content = image_content(source, size, value.get('partition') or '', value.get('kind'))
            except (ImageError, OSError, ValueError, IndexError, struct.error):
                content = {'size': size, 'format': 'raw', 'version': None, 'version_state': 'unknown', 'versions': []}
            after = os.fstat(source.fileno())
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise FlashRunnerError('OS payload changed during metadata read')
        # The executor already hashed this exact file for the plan; reuse it.
        content['sha256'] = value.get('sha256')
        return {'storage': value.get('storage'), 'partition': value.get('partition') or '',
                'kind': value.get('kind'), 'sha256': value.get('sha256'), 'content': content}

    def image_workspace(self, artifact):
        sha = artifact.get('sha256') if isinstance(artifact, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
            raise TaskError('a sealed image artifact is required')
        return self.root.resolve() / 'images' / sha

    def image_lock(self, artifact):
        workspace = self.image_workspace(artifact)
        with self._cache_lock:
            return self._image_locks.setdefault(str(workspace), threading.RLock())

    @contextmanager
    def image_guard(self, artifact, check=lambda: None):
        lock = self.image_lock(artifact)
        while not lock.acquire(timeout=.1):
            check()
        try:
            check()
            yield
        finally:
            lock.release()

    def prepared_artifact_unchanged(self, artifact):
        """Cheap source seal check; Go independently validates prepared members."""
        try:
            workspace = self.image_workspace(artifact)
            receipt = workspace / ('prepared-' + artifact['sha256'] + '.json')
            if receipt.is_symlink() or receipt.stat().st_size > 16 * 1024 ** 2:
                return False
            document = _json(receipt.read_bytes())
            source = Path(artifact['path'])
            info = source.stat()
            return (not source.is_symlink() and document.get('version') == 1
                and document.get('artifact_sha256') == artifact['sha256']
                and document.get('artifact_size') == artifact['size'] == info.st_size
                and ((document.get('source_path') == str(source)
                      and document.get('source_modified_ns') == info.st_mtime_ns)
                     or document.get('source_proofs', {}).get(str(source)) == info.st_mtime_ns)
                and bool(document.get('files'))
                and not (workspace / 'image-metadata.json').is_symlink()
                and all(self._prepared_member_unchanged(document, item) for item in document['files'])
                and (workspace / 'image-metadata.json').is_file())
        except (TaskError, ValueError, KeyError, OSError):
            return False

    @staticmethod
    def _prepared_member_unchanged(document, item):
        try:
            root = Path(document['root_path'])
            path = root / item['name']
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                return False
            info = path.stat()
            return info.st_size == item['size'] and info.st_mtime_ns == item['modified_ns']
        except (KeyError, OSError, ValueError):
            return False

    def cleanup_image(self, workspace):
        target = Path(workspace).absolute()
        if target.parent != self.root / 'images' or not re.fullmatch(r'[0-9a-f]{64}', target.name):
            raise TaskError('image directory is outside managed cache', 409)
        lock = self.image_lock({'sha256': target.name})
        if not lock.acquire(blocking=False):
            return False
        try:
            with self._cache_lock:
                if str(target) in self._active_workspaces:
                    return False
                if target.is_symlink():
                    raise TaskError('image directory changed', 409)
                if target.exists():
                    shutil.rmtree(target)
                    return True
                return False
        finally:
            lock.release()

    def prepare_image(self, argv, artifact, on_event=None, check=lambda: None,
                      max_expanded_bytes=32 * 1024 ** 3, max_cache_bytes=64 * 1024 ** 3):
        workspace = self.image_workspace(artifact)
        with self.image_guard(artifact, check):
            images = self._directory(self.root / 'images')
            if workspace.is_symlink() or workspace.exists() and not workspace.is_dir():
                raise TaskError('image preparation directory changed', 409)
            workspace = self._directory(workspace)
            # A cancelled, confirmed-stopped import has no completion seal.
            # Discard that partial extraction before retrying the same image.
            if not (workspace / ('prepared-' + artifact['sha256'] + '.json')).exists():
                shutil.rmtree(workspace)
                workspace = self._directory(workspace)
            with self._cache_lock:
                existing = self._cache_size(workspace)
                available = min(max(0, shutil.disk_usage(images).free - 256 * 1024 ** 2 - self._cache_reserved),
                    max(0, max_cache_bytes - self._cache_size(images) - self._cache_size(self.root / 'cache') - self._cache_size(self.root / 'workdirs') - self._cache_reserved))
                budget = min(max_cache_bytes, existing + available)
                required = min(max_cache_bytes, artifact['size'] + (existing or max_expanded_bytes))
                if budget < artifact['size']:
                    raise ImagePreparationQuotaError(max(0, required - budget))
                reserved = min(available, max_expanded_bytes + artifact['size'])
                self._cache_reserved += reserved
                self._active_workspaces.add(str(workspace))
            request = dict(version=1, artifact=dict(artifact), workspace=str(workspace),
                           max_expanded_bytes=max_expanded_bytes, max_cache_bytes=budget)
            try:
                operation = 'prepare-image'
                from .image_workspaces import origin_path
                origin = origin_path(artifact['path'])
                if artifact.get('name', '').endswith('.directory') and not origin.exists():
                    raise TaskError('目录来源记录缺失，请重新选择控制机目录', 409)
                if origin.exists():
                    if origin.is_symlink() or origin.stat().st_size > MAX_REQUEST:
                        raise TaskError('directory source record changed', 409)
                    document = _json(origin.read_bytes())
                    from .image_workspaces import members
                    _, current_files = members(document['directory'])
                    if current_files != document.get('files'):
                        raise TaskError('目录内容已修改，请重新读取目录生成新版本', 409)
                    request['directory_source'] = document['directory']
                    operation = 'prepare-directory'
                result = self._run(SimpleNamespace(argv=tuple(argv), timeout=3600), operation, request, on_event=on_event, check=check)
                if result.get('receipt_path'):
                    receipt = workspace / ('prepared-' + artifact['sha256'] + '.json')
                    if (result['receipt_path'] != str(receipt) or receipt.is_symlink()
                            or receipt.resolve(strict=True) != receipt or receipt.stat().st_size > 16 * 1024 ** 2):
                        raise FlashRunnerError('image preparation receipt escaped its image directory')
                    reused = result.get('reused') is True
                    result = _json(receipt.read_bytes())
                    result['reused'] = reused
                if result.get('artifact_sha256') != artifact['sha256'] or not result.get('files'):
                    raise FlashRunnerError('invalid image preparation receipt')
                summary = workspace / 'image-metadata.json'
                if summary.is_file():
                    if summary.is_symlink() or summary.stat().st_size > MAX_REQUEST:
                        raise FlashRunnerError('invalid image metadata cache')
                    metadata = _json(summary.read_bytes()).get('payload_metadata', [])
                else:
                    if on_event:
                        on_event({'event': 'phase', 'phase': 'reading-payload-metadata'})
                    files = {value['name']: value for value in result['files']}
                    writes = []
                    for table in result.get('catalogue') or []:
                        for part in table.get('partitions') or []:
                            name = part.get('payload')
                            item = files.get(result.get('expanded_files', {}).get(name, name))
                            if item:
                                writes.append(dict(item, path=str(Path(result['root_path']) / item['name']),
                                    storage='image', partition=part['name'], kind='partition'))
                    if not result.get('catalogue'):
                        item = files.get(result.get('expanded_files', {}).get('image.raw', 'image.raw'))
                        if item:
                            writes.append(dict(item, path=str(Path(result['root_path']) / item['name']),
                                storage='image', partition='', kind='partition'))
                    metadata = self._payload_metadata({'plan': {'writes': writes}}, workspace)
                    descriptor, temporary = tempfile.mkstemp(prefix='.metadata-', dir=workspace)
                    try:
                        with os.fdopen(descriptor, 'w') as target:
                            json.dump({'payload_metadata': metadata}, target, allow_nan=False)
                        os.replace(temporary, summary)
                    finally:
                        Path(temporary).unlink(missing_ok=True)
                from .image_tables import describe
                describe(result)
                if operation == 'prepare-directory' and document.get('table_choice'):
                    result['table_choice'] = document['table_choice']
                result['payload_metadata'] = metadata
                os.utime(workspace, None)
                return result
            except BaseException as error:
                if isinstance(error, ImagePreparationQuotaError):
                    error.reclaim_bytes = max(0, required - budget)
                if not getattr(error, 'stage', ''):
                    error.stage = 'prepare-image'
                    error.operation = 'prepare-image'
                if not getattr(error, 'cleanup_confirmed', True):
                    error.workspace = str(workspace)
                elif not (workspace / ('prepared-' + artifact['sha256'] + '.json')).exists():
                    shutil.rmtree(workspace, ignore_errors=True)
                raise
            finally:
                with self._cache_lock:
                    self._cache_reserved -= reserved
                    self._active_workspaces.discard(str(workspace))

    def plan(self, binding, artifact, input_mode, selections=None, verify='if-supported', check=lambda: None,
             target_storage=None, inspection_scope=None, on_event=None, loader_source=None, package_scope=None):
        with self.image_guard(artifact, check):
            return self._plan(binding, artifact, input_mode, selections, verify, check,
                              target_storage, inspection_scope, on_event, loader_source, package_scope)

    def _plan(self, binding, artifact, input_mode, selections=None, verify='if-supported', check=lambda: None,
              target_storage=None, inspection_scope=None, on_event=None, loader_source=None, package_scope=None):
        if input_mode not in ('full', 'partitions') or verify not in ('none', 'if-supported', 'optional', 'required'):
            raise TaskError('invalid flash mode or verification policy')
        if not isinstance(artifact, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(artifact.get('sha256', ''))):
            raise TaskError('a sealed image artifact is required')
        if target_storage is not None and (not isinstance(target_storage, str)
                or not re.fullmatch(r'(?:[A-Za-z0-9_.-]{1,128}|(?:nvme|scsi|mmc|usb):[0-3])', target_storage)):
            raise TaskError('invalid flash target storage')
        bootrom = (binding.profile_data.get('id') == 'auto-spacemit-gpt-v1'
                   and binding.profile_data.get('bootrom_identity') and not binding.profile_data.get('prepare'))
        if loader_source not in (None, 'builtin', 'image'):
            raise TaskError('invalid flash loader source')
        if loader_source and not bootrom:
            raise TaskError('设备已运行刷机环境；如需更换加载器，请重新进入 BootROM 下载模式', 409)
        preparation = self.prepare_image(binding.argv, artifact, on_event, check,
            max_expanded_bytes=binding.max_expanded_bytes, max_cache_bytes=binding.max_cache_bytes)
        source_workspaces = []
        for selection in selections or []:
            source = selection.get('artifact')
            if source and source.get('sha256') != artifact['sha256']:
                self.prepare_image(binding.argv, source, on_event, check,
                    max_expanded_bytes=binding.max_expanded_bytes, max_cache_bytes=binding.max_cache_bytes)
                source_workspaces.append(str(self.image_workspace(source)))
        workspace = self.image_workspace(artifact)
        budget = binding.max_cache_bytes
        with self._cache_lock:
            self._active_workspaces.add(str(workspace))
        request = dict(self._request(binding), artifact=dict(artifact), mode=input_mode,
                       selections=selections or [], verify='if-supported' if verify == 'optional' else verify,
                       workspace=str(workspace), max_cache_bytes=budget,
                       max_expanded_bytes=min(binding.max_expanded_bytes, budget))
        if not package_scope and input_mode == 'full' and binding.profile_data.get('id') == 'auto-spacemit-gpt-v1':
            tables = preparation.get('catalogue') or []
            if (any(t.get('name') == 'partition_universal.json' and t.get('format') == 'gpt' and t.get('selected') for t in tables)
                    and any(t.get('format') == 'mtd' and any(p.get('payload') == 'factory/bootinfo_spinor.bin'
                            for p in t.get('partitions', [])) for t in tables)):
                # NOR is opt-in. A mixed OS archive without a companion targets only its GPT.
                package_scope = 'system'
        if package_scope:
            request['package_scope'] = package_scope
        if target_storage is not None:
            request['target_storage'] = target_storage
        stage = 'image-plan'
        try:
            if bootrom:
                from .flash_loaders import installed_for, prepare_materials
                package = bool(preparation.get('catalogue')) or artifact.get('name', '').lower().endswith(('.tar.gz', '.tgz', '.tar', '.zip', '.directory'))
                if loader_source == 'image' and not package:
                    raise TaskError('此镜像不含刷机加载器，请选择内置加载器', 409)
                if loader_source == 'builtin' or loader_source is None and (
                        installed_for(binding.profile_data.get('chip_family')) or input_mode == 'partitions' and not package):
                    request['boot_materials'] = prepare_materials(binding, workspace, check, self.root)
            image = self._run(binding, 'image-plan', request, on_event=on_event, check=check)
            if (not isinstance(image.get('plan'), dict)
                    or not isinstance(image.get('payload_candidates', []), list)):
                raise FlashRunnerError('flashing executor returned an invalid image plan')
            execution = (self.execution_binding(binding, image['execution_profile'], workspace, check)
                         if image.get('execution_profile') is not None else binding)
            inspection_request = dict(request, plan=image['plan'], payload_candidates=image.get('payload_candidates', []))
            inspection_request['profile'] = execution.profile_data
            stage = 'check-plan'
            scope = inspection_scope() if inspection_scope is not None else nullcontext()
            with scope:
                if on_event:
                    on_event({'event': 'phase', 'phase': 'checking-layout'})
                result = self._run(execution, 'check-plan', inspection_request, on_event=on_event, check=check)
            result['workspace'] = str(workspace)
            result['source_workspaces'] = sorted(set(source_workspaces))
            if on_event:
                on_event({'event': 'phase', 'phase': 'reading-payload-metadata'})
            result['payload_metadata'] = self._payload_metadata(result, workspace)
            if image.get('execution_profile') is not None:
                result['execution_profile'] = execution.profile_data
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
            raise
        finally:
            with self._cache_lock:
                self._active_workspaces.discard(str(workspace))

    def nor_workspace(self, binding, check=lambda: None):
        """Prepare a RAM-only read profile without importing a dummy image."""
        if (not binding.target_id or binding.profile_data.get('id') != 'auto-spacemit-gpt-v1'
                or binding.profile_data.get('chip_family') != 'k3'):
            raise TaskError('NOR reading requires a detected K3 USB target', 409)
        return self._loader_workspace(binding, 'nor-read-profile', check)

    def ram_boot_workspace(self, binding, check=lambda: None):
        from .flash_loaders import ram_boot_installed
        family = binding.profile_data.get('chip_family')
        if family == 'k3' and not ram_boot_installed('k3'):
            return self.nor_workspace(binding, check)
        if (not binding.target_id or binding.profile_data.get('id') != 'auto-spacemit-gpt-v1'
                or family not in ('k1', 'k3') or not binding.profile_data.get('bootrom_identity')):
            raise TaskError('RAM boot requires a detected BootROM target', 409)
        return self._loader_workspace(binding, 'ram-boot-profile', check)

    def _loader_workspace(self, binding, operation, check):
        ram_boot = operation == 'ram-boot-profile'
        cache = self._directory(self.root / 'cache')
        with self._cache_lock:
            free = max(0, shutil.disk_usage(cache).free - 256 * 1024 ** 2 - self._cache_reserved)
            budget = min(free, max(0, binding.max_cache_bytes - self._cache_size(cache) - self._cache_reserved),
                         800 * 1024 ** 2)
            if budget < 32 * 1024 ** 2:
                raise TaskError('insufficient free space or cache quota for ' +
                                ('the RAM boot loader' if ram_boot else 'NOR backup'), 503)
            workspace = Path(tempfile.mkdtemp(prefix='plan-', dir=cache))
            self._cache_reserved += budget
            self._active_workspaces.add(str(workspace))
        try:
            request = dict(self._request(binding), mode='partitions', workspace=str(workspace),
                           max_cache_bytes=budget)
            if binding.profile_data.get('bootrom_identity'):
                from .flash_loaders import RAM_BOOT_PROTOCOLS, prepare_materials
                options = {'ram_boot': True} if ram_boot else {}
                request['boot_materials'] = prepare_materials(binding, workspace, check, self.root, **options)
                protocol = RAM_BOOT_PROTOCOLS[binding.profile_data['chip_family']] if ram_boot else 'spacemit-ram-v1'
                if request['boot_materials'].get('protocol') != protocol:
                    raise TaskError('RAM boot requires its managed loader' if ram_boot else
                                    'NOR backup requires the RAM-only NOR loader', 409)
            profile = self._run(binding, operation, request, check=check)
            execution = self.execution_binding(binding, profile, workspace, check)
            return {'workspace': str(workspace), 'execution_profile': execution.profile_data}
        except BaseException as error:
            if getattr(error, 'cleanup_confirmed', True):
                shutil.rmtree(workspace)
            else:
                error.workspace = str(workspace)
            raise
        finally:
            with self._cache_lock:
                self._cache_reserved -= budget
                self._active_workspaces.discard(str(workspace))

    def _owned_workspace(self, workspace):
        if not isinstance(workspace, (str, os.PathLike)):
            raise TaskError('NOR operation requires its managed workspace', 409)
        path = Path(workspace)
        resolved = path.resolve(strict=True)
        cache = (self.root / 'cache').resolve()
        if (not path.is_absolute() or path != resolved or not (resolved.parent == cache and resolved.name.startswith('plan-')
                or resolved.parent == (self.root / 'images').resolve() and re.fullmatch(r'[0-9a-f]{64}', resolved.name))
                or not stat.S_ISDIR(os.lstat(path).st_mode)):
            raise TaskError('NOR operation workspace changed', 409)
        return resolved

    def snapshot(self, binding, workspace, on_event=None, check=lambda: None, task_id='', attempt_id=''):
        workspace = self._owned_workspace(workspace)
        request = dict(self._request(binding), workspace=str(workspace), task_id=task_id,
                       attempt_id=attempt_id, mode='partitions')
        return self._run(binding, 'snapshot-nor', request, on_event, check)

    def set_boot_priority(self, binding, expected, value, check=lambda: None):
        request = dict(self._request(binding), boot_priority={'expected': expected, 'value': value})
        return self._run(binding, 'boot-priority', request, check=check)

    def storage_inventory(self, binding, check=lambda: None, on_event=None):
        from .storage_metadata import read_inventory
        request = dict(self._request(binding), mode='partitions')
        if on_event:
            on_event({'event': 'progress', 'phase': 'reading-system-layout', 'done': 0, 'total': 0})
        document = self._run(binding, 'storage-inventory', request, check=check)
        def fetch(extent):
            return self._run(binding, 'storage-metadata', dict(request, storage_read=extent), check=check)
        def progress(done, total):
            if on_event:
                on_event({'event': 'progress', 'phase': 'reading-system-metadata', 'done': done, 'total': 0})
        return read_inventory(document, fetch, check, progress)

    def execute(self, binding, plan, on_event=None, check=lambda: None, task_id='', attempt_id='',
                workspace=None, nor_baseline=None, reset_after=False, skip_verification=None):
        request = dict(self._request(binding), plan=plan, task_id=task_id, attempt_id=attempt_id)
        configuration = self.tuning(binding)
        if configuration:
            request['tuning'] = configuration['tuning']
        if reset_after:
            request['reset_after'] = True
        if workspace is not None:
            request['workspace'] = str(self._owned_workspace(workspace))
        if nor_baseline is not None:
            if not isinstance(nor_baseline, dict) or not isinstance(nor_baseline.get('path'), str):
                raise TaskError('invalid NOR baseline')
            self._owned_workspace(Path(nor_baseline['path']).parent)
            request['nor_baseline'] = {key: nor_baseline[key] for key in ('id', 'path', 'size', 'sha256')}
        result = self._run(binding, 'execute', request, on_event, check, skip_verification=skip_verification)
        return _with_configuration(result, configuration)

    def check_plan(self, binding, plan, payload_candidates=None, check=lambda: None, selections=None,
                   previous_device=None):
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
        if previous_device is not None:
            if (not isinstance(previous_device, dict) or plan.get('ready') is not False
                    or not binding.profile_data.get('prepare')
                    or previous_device.get('port_path') != binding.device.get('port_path')):
                raise TaskError('USB connection transition requires this plan\'s loader preparation', 409)
            request['previous_device'] = previous_device
        result = self._run(binding, 'check-plan', request, check=check)
        paths = [value.get('path') for value in list((result.get('plan') or {}).get('writes', []))
                 + list(result.get('payload_candidates') or []) if value.get('path')]
        if paths:
            source = Path(paths[0])
            workspace = next((parent for parent in source.parents
                if parent.parent in (self.root / 'cache', self.root / 'images')), None)
            if not source.is_absolute() or workspace is None:
                raise FlashRunnerError('payload is outside the managed cache')
            self._owned_workspace(workspace)
            result['payload_metadata'] = self._payload_metadata(result, workspace)
        return result

    def _run(self, binding, operation, request, on_event=None, check=lambda: None, timeout=None, skip_verification=None):
        encoded = json.dumps(request, allow_nan=False, separators=(',', ':')).encode('utf-8')
        if len(encoded) > MAX_REQUEST:
            raise TaskError('flash request is too large', 413)
        check()
        directory = self._directory(self.root / 'requests')
        descriptor, path = tempfile.mkstemp(prefix='flash-', suffix='.json', dir=directory)
        skip_path, skip_sent = Path(path + '.skip-verification'), False
        process, reader, stopping = None, None, threading.Event()
        cleanup_attempted = False
        outcome, nor_snapshots = {}, {}
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
                if operation == 'execute' and not skip_sent and skip_verification and skip_verification():
                    # One monotonic control alongside the private immutable
                    # request; the child checks it only between readback blocks.
                    os.close(os.open(skip_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
                    skip_sent = True
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
                if not isinstance(event, dict) or event.get('event') not in ('progress', 'phase', 'result', 'error', 'ddr-training'):
                    raise FlashRunnerError('flashing executor returned an invalid event')
                if event['event'] == 'ddr-training':
                    # Kept per board; never forwarded to the task event log.
                    from .flash_loaders import record_training
                    try:
                        record_training(binding, self.root, event.get('result'))
                    except ValueError as error:
                        logging.getLogger(__name__).warning('DDR training result ignored: %s', error)
                    continue
                if isinstance(event.get('result'), dict):
                    # Keep private, untrusted file receipts only on this process
                    # result/error. Publication independently validates the fixed
                    # task filename, workspace, size and every partition digest.
                    nor_snapshots.update({key: event['result'][key] for key in ('nor_before', 'nor_after')
                                          if isinstance(event['result'].get(key), dict)})
                if event['event'] == 'error':
                    outcome.update(safe_outcome(event.get('result')))
                    message = 'flashing executor reported failure'
                    reasons = {'host-storage-full': '控制机磁盘空间不足，镜像未能解压完成',
                        'image-file-missing': '镜像载荷或临时缓存文件不存在，请重新预览',
                        'image-permission-denied': '控制机没有读取镜像或写入缓存的权限',
                        'system-package-loader-unsupported': '所选加载器不支持安全整包写入系统盘。可选用支持系统盘核对的 DeckLab 内置加载器',
                        'system-disk-unavailable': 'K1 下载环境未检测到可使用 partition_universal.json 的系统盘，请检查存储介质和启动模式',
                        'system-disk-selection-required': '请在「读取设备存储」工作区中明确选择目标系统盘',
                        'target-permission-denied': '控制机无权打开重新连接的 USB 下载设备。请检查安装包的 USB 权限规则后重新检测',
                        'image-archive-corrupt': '镜像压缩包不完整或校验失败，请重新下载',
                        'image-invalid': '镜像格式或包内布局无效'}
                    if event.get('code') in reasons:
                        message = reasons[event['code']]
                    if operation == 'calibrate' and event.get('code') == 'system-disk-selection-required':
                        message = '写盘测速需要选定一块磁盘，请在「刷写配置」中填写测速目标盘'
                    # Reuse the service log for the complete diagnostic; the
                    # browser gets a correlation ID, never an arbitrary host path.
                    diagnostic = event.get('message')
                    diagnostic_id = secrets.token_hex(6) if isinstance(diagnostic, str) else ''
                    if event.get('code') == 'capacity-mismatch':
                        message = '目标容量与写入计划不符'
                    if operation == 'execute' and isinstance(diagnostic, str):
                        partition = re.fullmatch(r'new K1 GPT partition ([A-Za-z0-9_.-]{1,35}) (?:capacity|extent) did not match the plan', diagnostic)
                        if partition:
                            message = f'GPT 已写入，但新分区 {partition[1]} 的范围与计划不符，系统分区载荷尚未发送'
                        elif diagnostic.startswith(('cannot read new K1 GPT after layout write:', 'cannot query new K1 GPT partition ')):
                            message = 'GPT 已写入，未能核对新系统分区，系统分区载荷尚未发送'
                    if operation == 'image-plan' and diagnostic == 'unsupported NOR descriptor partitions':
                        message = 'NOR 分区表与目标芯片要求的分区列表不符，请核对 Firmware 镜像和板卡型号'
                    if diagnostic_id:
                        logging.getLogger(__name__).error('flash diagnostic %s operation=%s code=%s reason=%r',
                            diagnostic_id, operation, event.get('code'), diagnostic[:8192])
                    if operation in ('prepare-image', 'prepare-directory') and isinstance(diagnostic, str) and diagnostic.endswith((
                            'prepared image exceeds cache quota', 'image cache budget is exhausted',
                            'image expansion/cache budget exceeded', 'sparse cache exceeds budget',
                            'Android sparse image exceeds total expansion/cache budget')):
                        raise ImagePreparationQuotaError()
                    if event.get('code') in ('loader-fsbl-timeout', 'device-ram-unstable', 'loader-training-mismatch'):
                        from .flash_loaders import discard_training
                        embedded = discard_training(binding, self.root)
                        stopped = {'loader-fsbl-timeout': '控制机等待下一阶段的 USB 下载设备上线超时',
                                   'loader-training-mismatch': '该 USB 口上换了板子，保存的 DDR 训练结果不属于这块板',
                                   }.get(event['code'], '板上对下载样本计算的 CRC 与控制机不一致')
                        if embedded:
                            message = (stopped + '，尚未写入。这次复用了这块板之前保存的 DDR 训练结果，已作废；'
                                       '请让板子重新进入下载模式后重试，下次会重新训练 DDR（约多 6 秒）')
                        elif event['code'] == 'device-ram-unstable':
                            message = stopped + '，尚未写入。请分别检查 USB 传输和板上内存读写'
                        else:
                            message = (stopped + '，尚未写入。请查看串口输出，断电后重新进入下载模式再试')
                    if event.get('code') == 'device-data-corrupt':
                        message = ('板上解压命令失败，已停止刷写。此前的分段可能已写入。'
                                   '请按诊断编号查看执行器日志，并核对串口输出')
                    if event.get('code') == 'nor-baseline-changed':
                        message = '板上 NOR 内容在备份后发生变化，尚未写入；请重新读取备份并核对。'
                    if event.get('code') == 'nor-read-failed':
                        message = 'NOR 读取未完成，请查看原任务；不会自动重试写入。'
                    if event.get('code') == 'verify-failed':
                        message = '回读校验失败；请核对写入结果和回读文件，不会自动重刷。'
                    if event.get('code') == 'nor-package-requires-full':
                        message = '当前执行器仍限制 NOR 固件包选区，请升级刷机执行器；不要为刷单分区改选“完整镜像”。'
                    if event.get('code') == 'nor-partition-loader-required':
                        message = 'NOR 固件包选区需要下载专用 RAM 刷机环境，请升级后端或配置预备 FSBL/U-Boot；无需改选完整镜像。'
                    if event.get('code') == 'nor-package-unsupported':
                        message = '这是 NOR 固件刷机包；当前自动刷机仅支持 GPT 镜像包，尚未支持 NOR 固件刷写。'
                    if diagnostic_id:
                        message += '（诊断 ' + diagnostic_id + '）'
                    raise FlashRunnerError(message, outcome=outcome, code=event.get('code'))
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
                skipped = result.get('verify_result') == 'skipped'
                if skipped and not skip_sent:
                    outcome['verify_result'] = 'unavailable'
                    raise FlashRunnerError('flashing executor skipped verification without an operator request', outcome=outcome)
                if request.get('plan', {}).get('verify') == 'required' and result.get('verify_result') != 'verified' and not skipped:
                    raise FlashRunnerError('required flash verification was not confirmed', outcome=outcome)
            return result
        except BaseException as error:
            # Cancellation or a transport exception can race a confirmed final
            # write result; preserve that evidence without inferring success.
            if outcome and not getattr(error, 'outcome', None):
                error.outcome = dict(outcome)
            if nor_snapshots:
                error.nor_snapshots = dict(nor_snapshots)
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
                skip_path.unlink(missing_ok=True)
                if reader is not None and reader.is_alive():
                    raise FlashRunnerError('flashing event reader cleanup could not be confirmed', 503, False, outcome,
                                           stage=operation)
