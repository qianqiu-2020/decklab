"""Continuous native desktop events with the existing board/input authority.

The browser receives decoded display updates, never RDP/VNC credentials or a
target socket. Native libraries own remote protocols and held-key cleanup.
"""
from contextlib import nullcontext
from collections import deque
import json
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from .agent_grants import GrantRevoked
from .board_ownership import BoardInputOwnership, ownership_for
from .desktop import check_reservation, checked_image, reservation_activity
from .desktop_providers import validate_live_input
from .request_json import load_object
from .tasks import TaskError
from .transactions import TransactionError, transactions_for
from .websocket_io import WSConnection, WSError, WSTimeout


SUBPROTOCOL = 'decklab-desktop-v1'


def serve(handler, device, sid, actor, client_id):
    """Serve one already authenticated /api/desktop/stream request.

    The control route overwrites X-Seriald-Desktop-Origin after validating the
    public Host/Origin. Only its private Unix socket or authenticated TLS backend
    may reach this function. No proof or credential is accepted from URL fields.
    """
    manager = handler.manager
    sessions = manager.desktop
    session = sessions.get(device, sid, actor, client_id)
    if session['actor'] != actor or actor == 'web' and session['client_id'] != client_id:
        raise TaskError('desktop stream belongs to another controller', 403)
    provider = session['provider']
    if any(not callable(getattr(provider, name, None)) for name in (
            'start_live', 'next_event', 'live_input', 'release_all', 'stop_live')):
        raise TaskError('this desktop provider does not support continuous sessions', 404)
    origin = handler.headers.get('X-Seriald-Desktop-Origin', '')
    if not origin or not urlsplit(origin).netloc:
        raise TaskError('validated desktop stream origin is required', 403)
    channel = LiveChannel(manager, sessions, session, device, actor, client_id)
    with session['lock']:
        if session['closed'] or session.get('live_channel') is not None:
            raise TaskError('desktop already has a live controller', 423)
        session['live_channel'] = channel
    try:
        channel.ws = WSConnection.accept(handler, origins={origin}, hosts={urlsplit(origin).netloc},
            subprotocol=SUBPROTOCOL, max_message=65536, max_send=12 * 1024 ** 2,
            idle_timeout=30, lifetime=24 * 3600, io_timeout=4)
        try:
            channel.run()
        except Exception:
            channel.unknown = True
            channel._error('desktop-stream-stopped', '桌面流未确认启动或已中断，请重新连接后观察')
    finally:
        channel.finish()


class LiveChannel:
    def __init__(self, manager, sessions, session, device, actor, client_id):
        self.manager, self.sessions, self.session = manager, sessions, session
        self.device, self.actor, self.client_id = device, actor, client_id
        self.provider, self.config = session['provider'], session['config']
        self.ws, self.sender, self.proof = None, None, None
        self.lock, self.operation_lock = threading.RLock(), threading.RLock()
        self.stop, self.done = threading.Event(), threading.Event()
        self.finish_lock = threading.Lock()
        self.thread = None
        self.started, self.unknown, self.input_claimed = False, False, False
        self.frame_id, self.mode, self.sent_sequence = '', None, -1
        self.sequence_floor, self.waiting_reset = -1, True
        self.observed_sequence, self.observed_at, self.last_sequence = -1, 0, 0
        self.sent_sequences = deque(maxlen=128)
        self.retired_frame_ids = deque(maxlen=32)
        self.attachment = (id(device), device.connection_id)
        self.reservation_running, self.reservation_unknown = False, False
        self.inputs_issued, self.cleanup_confirmed = 0, False
        self.last_watch = 0

    def _send(self, packet):
        if self.ws is not None:
            self.ws.send(json.dumps(packet, ensure_ascii=False, allow_nan=False, separators=(',', ':')))

    def _error(self, code, message, needs_observation=True):
        if needs_observation:
            with self.lock:
                self.observed_sequence, self.observed_at = -1, 0
                self.session['needs_observation'] = True
        try:
            self._send({'type': 'error', 'code': code, 'error': message,
                        'needs_observation': needs_observation})
        except WSError:
            self.stop.set()

    def _actor_check(self):
        grants = getattr(self.manager, 'agent_grants', None)
        if grants is not None:
            grants.check_actor(self.actor)
        if self.stop.is_set() or self.session['closed']:
            raise TaskError('desktop live session stopped', 409)

    def _current_device(self):
        board = getattr(self.manager, 'boards', {}).get(self.session['serial'])
        if board is not None:
            current = getattr(board, 'connection', None)
            if current is None:
                current = getattr(board, 'history', None)
            if current is None and callable(getattr(self.manager, 'resolve', None)):
                current = self.manager.resolve(getattr(board, 'label', self.session['serial']))
            if current is not None:
                return current
        return self.device

    def _release(self, *, resync=False):
        try:
            result = self.provider.release_all(timeout=4)
            if not isinstance(result, dict) or result.get('ok') is not True or result.get('release_confirmed') is not True:
                raise TaskError('desktop key release was not confirmed', 502)
        except Exception:
            self.unknown = True
            self.cleanup_confirmed = False
            raise
        self.sessions.release_input(self.session)
        self.input_claimed = False
        with self.lock:
            self.observed_sequence, self.observed_at = -1, 0
            self.session['needs_observation'] = True
            if resync:
                self._retire_frame()
                self.mode, self.frame_id, self.waiting_reset = None, '', True
        if resync:
            self._start_live()

    def _start_live(self):
        result = self.provider.start_live(timeout=4)
        floor = result.get('sequence_floor') if isinstance(result, dict) else None
        if (not isinstance(result, dict) or result.get('ok') is not True
                or type(floor) is not int or floor < max(0, self.sent_sequence, self.sequence_floor)):
            raise TaskError('native desktop stream reset was not confirmed', 502)
        with self.lock:
            # The native encoder captures this waterline while invalidating its
            # old epoch. Frames already in IPC or this reader's hands cannot
            # qualify as the reset requested by the release acknowledgement.
            self.sequence_floor, self.waiting_reset = floor, True
        return result

    def _refresh_attachment(self):
        current = self._current_device()
        attachment = (id(current), current.connection_id)
        if (current.serial or '') != self.session['serial']:
            raise TaskError('desktop board identity changed', 409)
        if attachment != self.attachment:
            with self.operation_lock:
                if attachment == self.attachment:
                    return
                self._release(resync=True)
                self.device, self.attachment = current, attachment
                with self.lock:
                    self._retire_frame()
                    self.mode, self.frame_id = None, ''
                self._send({'type': 'reset', 'needs_observation': True, 'reason': 'attachment-changed'})

    def _check_attachment(self):
        current = self._current_device()
        if ((id(current), current.connection_id) != self.attachment
                or (current.serial or '') != self.session['serial']):
            raise TaskError('desktop attachment changed during input', 409)

    def _retire_frame(self):
        # Called with self.lock held. Browser observation acknowledgements may
        # cross a server reset in flight; only this channel's old modes qualify.
        if self.frame_id:
            self.retired_frame_ids.append(self.frame_id)

    def _authority(self):
        ownership = ownership_for(self.manager)
        return ownership.scope(self.actor, self.client_id, self.proof) if ownership else nullcontext()

    def _watch(self):
        self._actor_check()
        self._refresh_attachment()
        now = time.monotonic()
        if now - self.last_watch < .75:
            return
        # The original platform remains authoritative. A stale/unreachable read
        # fences input; GET polling never repeats a mutation receipt.
        check_reservation(self.config,
            expected_activity='running' if self.reservation_running else ('idle', 'running'), timeout=.75)
        if self.input_claimed:
            with self._authority(), transactions_for(self.device).actor_scope(self.actor, self.client_id):
                transactions_for(self.device).check_owner(self.actor)
        self.last_watch = time.monotonic()

    def _frame(self, event, attachment=None):
        fields = ('sequence', 'width', 'height', 'x', 'y', 'rect_width', 'rect_height')
        if any(type(event.get(key)) is not int for key in fields):
            raise TaskError('invalid native desktop frame', 502)
        width, height = event['width'], event['height']
        if (not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > 32 * 1024 ** 2
                or not 0 <= event['x'] < width or not 0 <= event['y'] < height
                or not 1 <= event['rect_width'] <= width - event['x']
                or not 1 <= event['rect_height'] <= height - event['y']):
            raise TaskError('invalid native desktop frame bounds', 502)
        _, image_width, image_height = checked_image({'png_base64': event.get('png_base64')})
        if (image_width, image_height) != (event['rect_width'], event['rect_height']):
            raise TaskError('native desktop rectangle dimensions changed', 502)
        mode = (self.attachment, event.get('generation'), width, height)
        with self.operation_lock:
            if attachment is not None and attachment != self.attachment:
                return  # Resync has queued a complete frame for the new attachment.
            with self.lock:
                if event['sequence'] <= self.sequence_floor:
                    return
                if self.waiting_reset and event.get('reset') is not True:
                    return
                changed = self.mode is not None and (mode != self.mode or event.get('reset') is True)
                gap = (self.mode is not None and event.get('reset') is not True
                       and event['sequence'] != self.sent_sequence + 1)
            if gap:
                self._release(resync=True)
                with self.lock:
                    self._retire_frame()
                    self.mode, self.frame_id = None, ''
                self._send({'type': 'reset', 'needs_observation': True, 'reason': 'frame-gap'})
                return
            if changed:
                self._release()
            with self.lock:
                if self.mode is None or changed:
                    if event.get('reset') is not True or (event['x'], event['y'], image_width, image_height) != (0, 0, width, height):
                        raise TaskError('desktop mode requires a complete reset frame', 502)
                    self._retire_frame()
                    self.frame_id, self.mode = secrets.token_hex(16), mode
                    self.sent_sequence, self.observed_sequence, self.observed_at = -1, -1, 0
                    self.waiting_reset = False
                    self.sent_sequences.clear()
                    self.session['needs_observation'] = True
                if event['sequence'] <= self.sent_sequence:
                    raise TaskError('native desktop frame sequence did not advance', 502)
                self.sent_sequence = event['sequence']
                self.sent_sequences.append(event['sequence'])
                packet = {key: event[key] for key in fields}
                packet.update(type='frame', frame_id=self.frame_id, reset=bool(event.get('reset')),
                    generation=event.get('generation'), png_base64=event['png_base64'], mime='image/png',
                    source_updated_at=event.get('source_updated_at'))
            self._send(packet)

    def _pump(self):
        try:
            while not self.stop.is_set():
                self._watch()
                attachment = self.attachment
                event = self.provider.next_event(timeout=.25)
                if event is None:
                    continue
                if event.get('type') == 'frame':
                    self._frame(event, attachment)
                elif event.get('type') == 'closed':
                    raise TaskError('native desktop connection ended', 502)
                # Native clipboard and unknown library events never reach the
                # browser through the display stream.
        except Exception:
            if not self.stop.is_set():
                self.unknown = True
                self._error('desktop-stream-stopped', '桌面流已停止；输入或释放结果不明时请重新核对')
            self.stop.set()
            if self.ws is not None:
                self.ws.close(1011)

    def _sequence(self, packet):
        value = packet.get('seq')
        if type(value) is not int or not 1 <= value <= 2 ** 53 - 1 or value <= self.last_sequence:
            raise TaskError('desktop input sequence was already used or invalid', 409)
        self.last_sequence = value
        return value

    def _event(self, event):
        if not isinstance(event, dict):
            raise TaskError('desktop input event must be an object')
        kind = event.get('type')
        allowed = {'key': {'type', 'keysym', 'code', 'down'},
            'pointer': {'type', 'x', 'y', 'buttons'}, 'wheel': {'type', 'x', 'y', 'steps'},
            'clipboard': {'type', 'text'}, 'clipboard-read': {'type'}, 'text': {'type', 'text'}}
        if not isinstance(kind, str) or kind not in allowed or set(event) != allowed[kind]:
            raise TaskError('unsupported desktop input event')
        if kind == 'key' and (type(event['down']) is not bool or type(event['keysym']) is not int
                or not 0 < event['keysym'] <= 0xffffffff or not isinstance(event['code'], str)
                or not re.fullmatch(r'[A-Za-z0-9_]{1,40}', event['code'])):
            raise TaskError('invalid desktop key event')
        if kind in ('pointer', 'wheel'):
            with self.lock:
                width, height = self.mode[-2:] if self.mode is not None else (0, 0)
            if (type(event['x']) is not int or type(event['y']) is not int
                    or not 0 <= event['x'] < width or not 0 <= event['y'] < height):
                raise TaskError('desktop pointer is outside the observed frame', 409)
            if kind == 'pointer' and (type(event['buttons']) is not int or not 0 <= event['buttons'] <= 7):
                raise TaskError('invalid desktop pointer buttons')
            if kind == 'wheel' and (type(event['steps']) is not int or not 1 <= abs(event['steps']) <= 20):
                raise TaskError('invalid desktop wheel steps')
        if kind == 'clipboard' and (not isinstance(event['text'], str) or len(event['text'].encode()) > 32768):
            raise TaskError('desktop clipboard text exceeds limit')
        if kind == 'text':
            try:
                validate_live_input(event)
            except ValueError:
                raise TaskError('invalid desktop Unicode text') from None
        return event

    def _input(self, packet):
        seq = self._sequence(packet)
        if packet.get('execution_lease') is not None:
            self.proof = BoardInputOwnership._proof(packet['execution_lease'])
        with self.operation_lock:
            self._actor_check()
            self._refresh_attachment()
            with self.lock:
                if (packet.get('frame_id') != self.frame_id or self.observed_sequence < 0
                        or time.monotonic() - self.observed_at > 30):
                    raise TaskError('observe the current desktop frame before input', 409)
            event = self._event(packet.get('event'))
            transactions = transactions_for(self.device)
            with self._authority(), transactions.actor_scope(self.actor, self.client_id):
                transactions.check_owner(self.actor)
                if not self.input_claimed:
                    check_reservation(self.config, for_input=not self.reservation_running,
                        expected_activity='running' if self.reservation_running else None, timeout=.75)
                    self.session['ownership'] = (self.device, transactions.claim(self.actor))
                    self.input_claimed = True
                with transactions.begin('desktop-live-input', 5, self.actor, require_open=False) as lease:
                    lease.check()
                    try:
                        if self.config.get('reservation') and not self.reservation_running:
                            self.reservation_running = True  # Lost receipts are not retried.
                            reservation_activity(self.config, 'running', timeout=1)
                        self._check_attachment()
                        if event['type'] == 'clipboard-read':
                            result = self.provider.clipboard_read(timeout=4)
                        else:
                            self.inputs_issued += 1
                            result = self.provider.live_input(event, timeout=4)
                        self._actor_check()
                        self._check_attachment()
                        lease.check()
                        if not isinstance(result, dict) or result.get('ok') is not True:
                            raise TaskError('native desktop input was not confirmed', 502)
                    except Exception:
                        self.unknown = True
                        raise TaskError('desktop input outcome is unknown; do not replay it', 502) from None
            packet = {'type': 'ack', 'seq': seq, 'ok': True}
            if event['type'] == 'clipboard-read':
                clipboard = {key: result[key] for key in ('available', 'text', 'encoding') if key in result}
                if ('text' in clipboard and (not isinstance(clipboard['text'], str)
                        or len(clipboard['text'].encode()) > 32768)):
                    raise TaskError('desktop clipboard response exceeds limit', 502)
                self._send(dict(clipboard, type='clipboard'))
            self._send(packet)

    def _packet(self, packet):
        kind = packet.get('type')
        if kind == 'heartbeat':
            self._send({'type': 'heartbeat', 'ok': True})
        elif kind == 'observed':
            with self.lock:
                sequence = packet.get('sequence')
                frame_id = packet.get('frame_id')
                if type(sequence) is int and isinstance(frame_id, str) and frame_id in self.retired_frame_ids:
                    return  # Never renew observation or authority for an old mode.
                if (frame_id != self.frame_id or type(sequence) is not int
                        or sequence not in self.sent_sequences):
                    raise TaskError('desktop observation belongs to an old frame', 409)
                self.observed_sequence, self.observed_at = sequence, time.monotonic()
                self.session['needs_observation'] = False
            self._send({'type': 'observed', 'ok': True, 'frame_id': frame_id, 'sequence': sequence})
        elif kind == 'bind':
            with self.operation_lock:
                proof = packet.get('execution_lease')
                if proof is not None:
                    proof = BoardInputOwnership._proof(proof)
                ownership = ownership_for(self.manager)
                if ownership is not None:
                    with ownership.scope(self.actor, self.client_id, proof):
                        ownership.check(self.device.serial, self.actor, self.client_id)
                self.proof = proof
            self._send({'type': 'bound', 'ok': True})
        elif kind == 'release_all':
            seq = self._sequence(packet)
            with self.operation_lock:
                self._release(resync=True)
                # Keep the acknowledgement ahead of the full reset queued by
                # resync. The browser can then fence unsent toolbar intentions
                # until that fresh frame has actually been painted.
                self._send({'type': 'ack', 'seq': seq, 'ok': True, 'release_confirmed': True})
        elif kind == 'input':
            self._input(packet)
        else:
            raise TaskError('unsupported desktop stream message')

    def run(self):
        self.thread = threading.current_thread()
        self._actor_check()
        result = self._start_live()
        self.started = True
        self._send({'type': 'ready', 'session': self.sessions.public(self.session),
                    'capabilities': result.get('capabilities', {}), 'heartbeat_seconds': 10,
                    'needs_observation': True})
        self.sender = threading.Thread(target=self._pump, name='desktop-live-events', daemon=True)
        self.sender.start()
        while not self.stop.is_set():
            try:
                raw = self.ws.recv(timeout=.5)
                if raw is None:
                    break
                self._actor_check()
                if not isinstance(raw, str):
                    raise TaskError('desktop client messages must be JSON text')
                self._packet(load_object(raw.encode()))
            except WSTimeout:
                continue
            except WSError:
                break
            except Exception as error:
                # A rejected packet has not issued another native action. End
                # this controller so its held keys and input owner are cleaned
                # up, without turning a known rejection into unknown execution.
                if not isinstance(error, (TaskError, TransactionError, ValueError, GrantRevoked)):
                    self.unknown = True
                self._error('desktop-input-unknown' if self.unknown else 'desktop-input-rejected',
                    '输入结果未知，请先重新观察，不要重放' if self.unknown else '桌面输入被拒绝，请核对当前画面与输入占用')
                break

    def request_stop(self):
        self.stop.set()
        if self.ws is not None:
            self.ws.close(1001)
        if self.thread is not None and self.thread is not threading.current_thread():
            self.done.wait(10)
        return self.done.is_set()

    def finish(self):
        if self.done.is_set():
            return
        if not self.finish_lock.acquire(blocking=False):
            return
        try:
            if self.done.is_set():
                return
            self.stop.set()
            if self.ws is not None:
                self.ws.close(1000 if not self.unknown else 1011)
            if self.sender is not None and self.sender is not threading.current_thread():
                self.sender.join(timeout=2)
                if self.sender.is_alive():
                    self.unknown = True
            if self.started:
                with self.operation_lock:
                    try:
                        self._release()
                        result = self.provider.stop_live(timeout=4)
                        if (not isinstance(result, dict) or result.get('ok') is not True
                                or result.get('release_confirmed') is not True):
                            raise TaskError('native desktop stream stop was not confirmed', 502)
                        self.cleanup_confirmed = True
                    except Exception:
                        self.unknown = True
                        self.cleanup_confirmed = False
            if self.reservation_running and not self.reservation_unknown:
                self.reservation_unknown = True
                try:
                    reservation_activity(self.config, 'unknown' if self.unknown else 'idle', timeout=1)
                except Exception:
                    self.unknown = True
            event = getattr(getattr(self.manager, 'store', None), 'event', None)
            if callable(event) and self.started:
                try:
                    event(self.session['serial'], self.actor, 'desktop-live-ended', json.dumps({
                        'session_id': self.session['id'], 'inputs_issued': self.inputs_issued,
                        'outcome': 'unknown' if self.unknown else 'acknowledged' if self.inputs_issued else 'not-issued',
                        'cleanup_confirmed': self.cleanup_confirmed}, separators=(',', ':')))
                except Exception:
                    self.unknown = True
            with self.session['lock']:
                if self.session.get('live_channel') is self:
                    self.session.pop('live_channel', None)
            if self.unknown:
                self.sessions.close_session(self.session)
        finally:
            self.done.set()
            self.finish_lock.release()
