"""Explicit host-command hardware adapters; never discover or guess a target."""
from contextlib import nullcontext
from dataclasses import dataclass
import json
import os
import signal
import string
import subprocess
import time

from .profiles import _unique_object
from .tasks import TaskError, TaskCancelled
from .transactions import transactions_for, TransactionError

ACTIONS = ('power-on', 'power-off', 'reset', 'flash')


def host_adapter():
    return 'windows-job-object' if os.name == 'nt' else 'linux-process-group'


class PosixProcess:
    def __init__(self, argv):
        self.process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)

    def poll(self):
        return self.process.poll()

    @property
    def returncode(self):
        return self.process.returncode

    def close(self):
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait()


def helper_process(argv):
    if os.name == 'nt':
        from .process_windows import WindowsProcess
        return WindowsProcess(argv)
    if os.name == 'posix':
        return PosixProcess(argv)
    raise TaskError('unsupported helper process platform', 503)


@dataclass(frozen=True)
class HardwareCommand:
    argv: tuple
    timeout: int = 60
    allow_agent: bool = False
    exclusive_serial: bool = False

    def __post_init__(self):
        if (not isinstance(self.argv, tuple) or not 1 <= len(self.argv) <= 64
                or any(not isinstance(arg, str) or len(arg.encode()) > 4096 or '\x00' in arg for arg in self.argv)
                or not os.path.isabs(self.argv[0])):
            raise ValueError('hardware argv requires an absolute executable and bounded string arguments')
        if type(self.timeout) is not int or not 1 <= self.timeout <= 3600:
            raise ValueError('hardware timeout must be an integer between 1 and 3600 seconds')
        if type(self.exclusive_serial) is not bool:
            raise ValueError('hardware exclusive_serial must be boolean')
        if type(self.allow_agent) is not bool:
            raise ValueError('hardware allow_agent must be boolean')
        for index, arg in enumerate(self.argv):
            for _, field, spec, conversion in string.Formatter().parse(arg):
                if field is not None and (index == 0 or field not in ('serial', 'device', 'port') or spec or conversion):
                    raise ValueError('hardware placeholders may only be serial, device or port arguments')

    def expand(self, device):
        for arg in self.argv:
            for _, field, _, _ in string.Formatter().parse(arg):
                if field in ('device', 'port') and not getattr(device, 'dev' if field == 'device' else 'by_path'):
                    raise TaskError('configured helper requires a current serial attachment', 409)
        return [arg.format(serial=device.serial, device=device.dev, port=device.by_path) for arg in self.argv]


class HardwareBook:
    def __init__(self, document=None):
        document = {} if document is None else document
        if not isinstance(document, dict) or set(document) - {'boards'} or not isinstance(document.get('boards', {}), dict):
            raise ValueError('hardware configuration requires a boards object')
        self.boards = {}
        for serial, actions in document.get('boards', {}).items():
            if not isinstance(serial, str) or not serial or len(serial.encode()) > 100:
                raise ValueError('invalid hardware board serial')
            if not isinstance(actions, dict) or set(actions) - set(ACTIONS):
                raise ValueError('invalid hardware action')
            self.boards[serial] = {}
            for action, values in actions.items():
                if (not isinstance(values, dict) or set(values) - {'argv', 'timeout', 'allow_agent', 'exclusive_serial'}
                        or not isinstance(values.get('argv'), list)):
                    raise ValueError('invalid hardware command configuration')
                self.boards[serial][action] = HardwareCommand(tuple(values['argv']), values.get('timeout', 60),
                                                            values.get('allow_agent', False),
                                                            values.get('exclusive_serial', False))

    @classmethod
    def from_env(cls):
        path = os.environ.get('SERIALD_HARDWARE')
        if not path:
            return cls()
        with open(path, 'rb') as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError('hardware configuration exceeds 64 KiB')
        return cls(json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_object))

    def command(self, serial, action, actor):
        command = self.boards.get(serial, {}).get(action)
        if command is None:
            raise TaskError('hardware action is not configured for this board', 404)
        if actor != 'web' and not command.allow_agent:
            raise TaskError('this hardware action is not enabled for agents', 403)
        if os.name not in ('posix', 'nt'):
            raise TaskError('unsupported helper process platform', 503)
        return command

    def available(self, serial, actor):
        return {'actions': [action for action, command in self.boards.get(serial, {}).items()
                            if actor == 'web' or command.allow_agent],
                'supported': os.name in ('posix', 'nt'), 'adapter': host_adapter()}


def run_command(argv, timeout, check, cleanup_failed=None):
    """Bound execution and terminate the whole owned group, including descendants."""
    check()
    started = time.monotonic()
    process = helper_process(argv)
    try:
        while process.poll() is None:
            check()
            if time.monotonic() - started >= timeout:
                raise TaskError('hardware helper timed out; operation may be partial', 504)
            time.sleep(0.05)
        check()
        if process.returncode:
            raise TaskError('hardware helper exited with status %s; operation may be partial' % process.returncode, 502)
        return {'exit_code': process.returncode, 'duration_ms': round((time.monotonic() - started) * 1000)}
    finally:
        try:
            process.close()
        except BaseException:
            if cleanup_failed is not None:
                cleanup_failed()
            raise


class HardwareOperation:
    def __init__(self, manager, device, action, actor, client_id):
        self.manager, self.device, self.action = manager, device, action
        self.actor, self.client_id = actor, client_id
        self.serial, self.connection, self.stop = device.serial or '', device.connection_id, device.stop
        self.was_stopped = self.stop.is_set()
        self.command = manager.hardware.command(self.serial, action, actor)
        self.argv = self.command.expand(device)

    def run(self, context):
        device = self.device
        transactions = transactions_for(device)
        lease = None
        context.on_cancel(transactions._wake)
        def check():
            context.check()
            if lease is not None:
                lease.check()
            if (device.connection_id != self.connection or device.stop is not self.stop
                    or (device.serial or '') != self.serial or self.stop.is_set() != self.was_stopped):
                raise TaskError('hardware target connection or identity changed', 409)
            current = self.manager.boards[self.serial].connection
            if current is not None and current is not device and current.present:
                raise TaskError('hardware target has a new connection', 409)
            transactions.check_owner(self.actor, self.client_id)
        with transactions.actor_scope(self.actor, self.client_id), \
                transactions.task_scope(context.cancelled, self.connection, self.stop), \
                transactions.begin('hardware-' + self.action, self.command.timeout + 1, require_open=False) as lease:
            check()
            self.manager.store.event(self.serial, self.actor, 'hardware-start', self.action)
            context.progress(0, 1, self.action)
            try:
                handoff = (self.manager.exclusive_serial(device, check)
                           if self.command.exclusive_serial else nullcontext())
                with handoff as permit:
                    result = run_command(self.argv, self.command.timeout, check,
                                         (lambda: permit.update(resume=False)) if permit is not None else None)
                check()
                self.manager.store.event(self.serial, self.actor, 'hardware-complete', self.action)
                context.progress(1, 1, self.action)
                return dict(result, ok=True, action=self.action)
            except Exception as error:
                self.manager.store.event(self.serial, self.actor, 'hardware-stopped', self.action)
                if isinstance(error, TransactionError) and error.code == 'cancelled':
                    raise TaskCancelled('hardware operation cancelled; outcome may be partial') from error
                raise
