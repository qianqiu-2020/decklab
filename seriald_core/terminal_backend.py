"""Shared terminal parser RPC. Owns no hardware; transport failures stay isolated."""
import atexit
import base64
from collections import deque
import json
from pathlib import Path
import queue
import secrets
import shutil
import subprocess
import sys
import threading

from .config import SCROLLBACK, STATIC

PROCESS_FACTORY = subprocess.Popen


def node_binary():
    root = Path(STATIC)
    for candidate in (root / 'runtime/node.exe', root / 'build/terminal-runtime/node.exe'):
        if sys.platform == 'win32' and candidate.is_file():
            return str(candidate)
    return shutil.which('node') or shutil.which('nodejs')


class TerminalEngine:
    def __init__(self):
        self.lock = threading.RLock()
        self.process = None
        self.responses = queue.Queue()
        self.sequence = 0
        self.error = ''

    def _reader(self, process):
        try:
            for line in process.stdout:
                self.responses.put(json.loads(line))
        except (OSError, ValueError):
            pass
        finally:
            self.responses.put({'error': 'terminal worker exited'})

    def call(self, action, sid, **values):
        with self.lock:
            if self.error:
                raise RuntimeError(self.error)
            if self.process is None:
                node = node_binary()
                if not node:
                    raise RuntimeError('Node.js terminal runtime is unavailable')
                self.process = PROCESS_FACTORY([node, str(Path(STATIC) / 'seriald_core/terminal_worker.js')],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding='utf-8', bufsize=1,
                    creationflags=0x08000000 if sys.platform == 'win32' else 0)
                threading.Thread(target=self._reader, args=(self.process,), daemon=True).start()
            self.sequence += 1
            try:
                self.process.stdin.write(json.dumps(dict(values, action=action, sid=sid,
                                                        id=self.sequence), ensure_ascii=True) + '\n')
                self.process.stdin.flush()
                answer = self.responses.get(timeout=2)
                if answer.get('id') != self.sequence:
                    raise RuntimeError(answer.get('error') or 'terminal worker sequence mismatch')
                if answer.get('error'):
                    return {'error': answer['error']}
                return answer['result']
            except (OSError, ValueError, queue.Empty, RuntimeError) as error:
                self.error = str(error) or 'terminal worker timed out'
                self.close()
                raise RuntimeError(self.error) from error

    def close(self):
        with self.lock:
            process, self.process = self.process, None
            if process:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
                for pipe in (process.stdin, process.stdout):
                    pipe.close()


ENGINE = TerminalEngine()
atexit.register(ENGINE.close)


class SharedTerminal:
    """A single canonical screen and protocol responder for one live stream."""
    def __init__(self, kind, cols=80, rows=24):
        self.sid = secrets.token_hex(12)
        self.kind, self.cols, self.rows = kind, cols, rows
        self.lock = threading.RLock()
        self.controller = None
        self.control_revision = 0
        self.state = None
        self.deltas = deque()
        self.delta_bytes = 0
        self.error = ''
        self.closed = False

    def _call(self, action, **values):
        if self.error or self.closed:
            return None
        try:
            if self.state is None:
                self.state = ENGINE.call('create', self.sid, kind=self.kind, cols=self.cols, rows=self.rows)
            result = ENGINE.call(action, self.sid, **values)
            if result.get('error'):
                raise RuntimeError(result['error'])
            self.state = result
            self.cols, self.rows = result['geometry']['cols'], result['geometry']['rows']
            return result
        except (OSError, RuntimeError) as error:
            self.error = str(error)
            return None

    def feed(self, data, off, live=True):
        with self.lock:
            state = self._call('feed', data=base64.b64encode(data).decode('ascii'), off=off, live=live)
            if state:
                rendered = base64.b64decode(state.pop('render_data', ''))
                self.deltas.append((off - len(data), off, rendered))
                self.delta_bytes += len(rendered)
                while self.deltas and (self.delta_bytes > SCROLLBACK or len(self.deltas) > 4096):
                    self.delta_bytes -= len(self.deltas.popleft()[2])
            return state.get('replies', []) if state else []

    def resize(self, cols, rows):
        with self.lock:
            return self._call('resize', cols=cols, rows=rows) is not None

    def metadata(self):
        with self.lock:
            result = dict(self.state or {})
            for key in ('data', 'pending', 'replies', 'render_data', 'adapter_state'):
                result.pop(key, None)
            result.update(managed=bool(self.state and not self.error), controller=self.controller,
                          control_revision=self.control_revision, error=self.error)
            return result

    def snapshot(self):
        with self.lock:
            if self.state is None:
                return None
            if self.closed:
                return dict(self.state, controller=self.controller, control_revision=self.control_revision) if 'data' in self.state else None
            result = self._call('snapshot')
            return dict(result, controller=self.controller, control_revision=self.control_revision) if result else None

    def control(self, actor, client_id='', takeover=False):
        with self.lock:
            target = {'actor': actor, 'client_id': client_id}
            if self.controller is None or self.controller == target or takeover:
                if self.controller != target:
                    self.control_revision += 1
                self.controller = target
                return True
            return False

    def presentation(self, off, revision):
        """Atomically return a screen or complete canonical chunks since its cursor.

        Raw byte offsets remain the public cursor. A chunk boundary is required
        because an APT correction can have a different length than UART bytes.
        Unknown/evicted cursors recover from a snapshot, never partial replay.
        """
        with self.lock:
            if not self.state or self.error:
                return None
            current = self.state['off']
            chunks = [chunk for chunk in self.deltas if chunk[1] > off]
            complete = off == current or chunks and chunks[0][0] == off
            if off <= 0 or revision != self.state['revision'] or not complete:
                snapshot = self.snapshot()
                return {'off': snapshot['off'], 'data_b64': '', 'data': '',
                        'terminal_snapshot': snapshot} if snapshot else None
            return {'off': current, 'data_b64': base64.b64encode(b''.join(c[2] for c in chunks)).decode('ascii'), 'data': ''}

    def restore(self, snapshot):
        """Clean same-connection handoff only; replay never sends protocol input."""
        if not isinstance(snapshot, dict):
            return False
        geometry = snapshot.get('geometry')
        if (not isinstance(geometry, dict) or type(geometry.get('cols')) is not int
                or type(geometry.get('rows')) is not int
                or not 20 <= geometry['cols'] <= 500 or not 2 <= geometry['rows'] <= 200
                or type(snapshot.get('off')) is not int or not 0 <= snapshot['off'] < 2 ** 63):
            return False
        try:
            for field in ('data', 'pending'):
                value = snapshot.get(field, '')
                if not isinstance(value, str) or len(value) > 32 * 1024 ** 2:
                    return False
                base64.b64decode(value, validate=True)
        except ValueError:
            return False
        with self.lock:
            self.cols, self.rows = snapshot['geometry']['cols'], snapshot['geometry']['rows']
            return self._call('restore', snapshot=snapshot) is not None

    def close(self):
        with self.lock:
            if self.state:
                if not self.error:
                    self._call('snapshot')
                try:
                    ENGINE.call('dispose', self.sid)
                except (OSError, RuntimeError):
                    pass
            self.closed = True


def screen_for(stream):
    terminal = getattr(stream, 'terminal', None)
    return getattr(terminal, 'screen', terminal)


def control_session(stream, actor, client_id='', takeover=False):
    terminal = screen_for(stream)
    return not isinstance(terminal, SharedTerminal) or terminal.control(actor, client_id, takeover)


def terminal_payload(stream, payload, requested_off, screen=False, revision=0,
                     actor='web', client_id=''):
    """Attach canonical state; raw CLI cursors retain their existing contract."""
    terminal = screen_for(stream)
    if not isinstance(terminal, SharedTerminal):
        return payload
    with terminal.lock:
        if screen:
            presentation = terminal.presentation(requested_off, revision)
            if presentation:
                payload.update(presentation)
        state = terminal.metadata()
    if hasattr(getattr(stream, 'terminal', None), 'prompt'):
        state['serial_ready'] = stream.terminal.prompt() and not stream.terminal.input_pending
        state['serial_configured'] = (stream.terminal.geometry is not None
                                      and stream.terminal.attempted
                                      and not stream.terminal.environment_pending)
    payload['terminal_state'] = state
    if getattr(stream, 'sid', None):
        allowed = actor == stream.actor or actor in stream.takeovers
        controller = state.get('controller')
        payload['terminal_writable'] = allowed and (controller is None or controller == {
            'actor': actor, 'client_id': client_id if actor == 'web' else ''})
    return payload
