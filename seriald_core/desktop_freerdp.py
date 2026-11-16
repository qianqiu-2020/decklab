"""FreeRDP's native client inside the existing isolated desktop worker."""
import base64
import ctypes
import io
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import unquote, urlsplit

from .desktop_providers import LiveFrames, MAX_CLIPBOARD_BYTES, RDPDesktop, validate_live_input
from .desktop_errors import connection_error, redact


ABI_VERSION = 1
COMMAND_TIMEOUT_MS = 2500
MAX_PIXELS = 32 * 1024 ** 2
MAX_TEXT_UNITS = 2048
MAX_CLIPBOARD_UNITS = 65536
_P16 = ctypes.POINTER(ctypes.c_uint16)
_P32 = ctypes.POINTER(ctypes.c_uint32)
_FrameCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8),
    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint64)
_ClosedCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p)


class _Options(ctypes.Structure):
    _fields_ = [('size', ctypes.c_uint32), ('abi_version', ctypes.c_uint32),
        ('hostname', ctypes.c_char_p), ('username', ctypes.c_char_p),
        ('password', ctypes.c_char_p), ('domain', ctypes.c_char_p),
        ('certificate_sha256', ctypes.c_char_p), ('port', ctypes.c_uint32),
        ('width', ctypes.c_uint32), ('height', ctypes.c_uint32)]


def _library_path():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    name = 'decklab-freerdp.dll' if sys.platform == 'win32' else 'libdecklab-freerdp.so'
    return root / 'desktop-runtime' / 'freerdp' / name


class NativeLibrary:
    """The reviewed private SDK, never an arbitrary library found on PATH."""
    def __init__(self, path=None):
        path = Path(path) if path is not None else _library_path()
        if not path.is_absolute() or not path.is_file():
            raise FileNotFoundError('bundled FreeRDP runtime is missing: ' + str(path))
        self.directory = (os.add_dll_directory(str(path.parent))
            if sys.platform == 'win32' and hasattr(os, 'add_dll_directory') else None)
        try:
            self.dll = ctypes.CDLL(str(path))
            signatures = {
                'dl_rdp_abi_version': (ctypes.c_uint32, []),
                'dl_rdp_new': (ctypes.c_void_p, [ctypes.POINTER(_Options), _FrameCallback,
                    _ClosedCallback, ctypes.c_void_p]),
                'dl_rdp_start': (ctypes.c_int, [ctypes.c_void_p, ctypes.c_uint32]),
                'dl_rdp_error': (ctypes.c_uint32, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]),
                'dl_rdp_pointer': (ctypes.c_int, [ctypes.c_void_p, ctypes.c_uint32,
                    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]),
                'dl_rdp_key': (ctypes.c_int, [ctypes.c_void_p, ctypes.c_uint32,
                    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]),
                'dl_rdp_keycode': (ctypes.c_uint32, [ctypes.c_char_p]),
                'dl_rdp_text': (ctypes.c_int, [ctypes.c_void_p, _P16,
                    ctypes.c_uint32, ctypes.c_uint32]),
                'dl_rdp_clipboard_write': (ctypes.c_int, [ctypes.c_void_p, _P16,
                    ctypes.c_uint32, ctypes.c_uint32]),
                'dl_rdp_clipboard_read': (ctypes.c_int, [ctypes.c_void_p, _P16,
                    ctypes.c_uint32, _P32, _P32, ctypes.c_uint32]),
                'dl_rdp_release_all': (ctypes.c_int, [ctypes.c_void_p, ctypes.c_uint32]),
                'dl_rdp_free': (None, [ctypes.c_void_p]),
            }
            for name, (result, arguments) in signatures.items():
                function = getattr(self.dll, name)
                function.restype, function.argtypes = result, arguments
        except Exception as error:
            self.close()
            raise RuntimeError('Cannot load bundled FreeRDP runtime: ' + str(error)) from error

    def __getattr__(self, name):
        return getattr(self.dll, name)

    def close(self):
        if self.directory is not None:
            self.directory.close()
            self.directory = None


def _utf16(value, max_chars, max_units, *, clipboard=False):
    if not isinstance(value, str) or len(value) > max_chars or '\0' in value:
        raise ValueError('invalid native RDP text')
    try:
        encoded = value.encode('utf-16-le')
        if clipboard and len(value.encode('utf-8')) > MAX_CLIPBOARD_BYTES:
            raise ValueError()
    except (UnicodeError, ValueError):
        raise ValueError('invalid native RDP text') from None
    units = len(encoded) // 2
    if units > max_units:
        raise ValueError('native RDP text exceeds limit')
    array = (ctypes.c_uint16 * max(1, units))()
    if encoded:
        ctypes.memmove(array, encoded, len(encoded))
    return array, units


class FreeRDPDesktop:
    """Translate native callbacks and input calls into the existing provider API."""
    def __init__(self, config, library=None):
        self.frames = LiveFrames()
        self.lock, self.closed = threading.RLock(), threading.Event()
        self.image, self.native_generation, self.updated_at = None, None, 0
        self.live, self.buttons, self.pointer = False, 0, (0, 0)
        self.text_input = config.get('text_input', 'unicode')
        self.library, self.handle = None, None
        # C borrows these function pointers until dl_rdp_free has joined IO.
        self.frame_callback = _FrameCallback(self._frame)
        self.closed_callback = _ClosedCallback(self._closed)
        stage, native = 'configuration', None
        try:
            self.options = self._options(config)
            stage = 'runtime'
            self.library = library if library is not None else NativeLibrary()
            if self.library.dl_rdp_abi_version() != ABI_VERSION:
                raise RuntimeError('unsupported native RDP ABI')
            self.handle = self.library.dl_rdp_new(ctypes.byref(self.options),
                self.frame_callback, self.closed_callback, None)
            if not self.handle:
                raise RuntimeError('native RDP initialization failed')
            deadline = time.monotonic() + 25
            stage = 'connect'
            if self.library.dl_rdp_start(self.handle, 20000) != 1:
                reason = ctypes.create_string_buffer(2048)
                code = self.library.dl_rdp_error(self.handle, reason, len(reason))
                native = {'code': code, 'reason': reason.value.decode('utf-8', 'replace')}
                raise RuntimeError('dl_rdp_start returned failure')
            stage = 'framebuffer'
            with self.frames.condition:
                self.frames.condition.wait_for(lambda: self.image is not None or self.closed.is_set(),
                    timeout=max(0, deadline - time.monotonic()))
                if self.image is None or self.closed.is_set():
                    raise RuntimeError('native RDP framebuffer unavailable')
        except Exception as error:
            if self.handle is not None and native is None and stage in ('connect', 'framebuffer'):
                try:
                    reason = ctypes.create_string_buffer(2048)
                    code = self.library.dl_rdp_error(self.handle, reason, len(reason))
                    native = {'code': code, 'reason': reason.value.decode('utf-8', 'replace')}
                except Exception:
                    pass
            failure = connection_error(config, stage, error, native)
            try:
                self.close()
            except Exception as cleanup:
                failure.details['cleanup_error'] = redact(cleanup, config)
            raise failure from None

    @staticmethod
    def _options(config):
        try:
            value = config['secret_url']
            if not isinstance(value, str) or len(value) > 16384 or '\0' in value:
                raise ValueError()
            url = urlsplit(value)
            if url.scheme != 'rdp+ntlm-password' or not url.hostname or url.path not in ('', '/') or url.query or url.fragment:
                raise ValueError()
            username, password = unquote(url.username or ''), unquote(url.password or '')
            domain = ''
            if '\\' in username:
                domain, username = username.rsplit('\\', 1)
            strings = (url.hostname, username, password, domain)
            if not username or any('\0' in item or len(item) > 4096 for item in strings):
                raise ValueError()
            fingerprint = config.get('certificate_sha256', '')
            if not isinstance(fingerprint, str) or fingerprint and not re.fullmatch(r'[0-9a-fA-F]{64}', fingerprint):
                raise ValueError()
            width, height = config.get('width', 1280), config.get('height', 800)
            if any(type(v) is not int or not 64 <= v <= 4096 for v in (width, height)):
                raise ValueError()
            port = url.port or 3389
            if not 1 <= port <= 65535 or config.get('text_input', 'unicode') not in ('unicode', 'clipboard'):
                raise ValueError()
            return _Options(ctypes.sizeof(_Options), ABI_VERSION,
                *(v.encode('utf-8') for v in strings), fingerprint.lower().encode('ascii'), port, width, height)
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise ValueError('invalid server-side native RDP configuration') from None

    def _closed(self, unused):
        self.closed.set()
        self.frames.close('native RDP connection ended')

    def _frame(self, unused, pixels, width, height, stride, x, y, dirty_width, dirty_height, generation):
        if self.closed.is_set():
            return
        try:
            if (not pixels or not 0 < width <= 8192 or not 0 < height <= 8192
                    or width * height > MAX_PIXELS or not width * 4 <= stride <= width * 4 + 256
                    or not 0 <= x < width or not 0 <= y < height
                    or not 0 < dirty_width <= width - x or not 0 < dirty_height <= height - y):
                raise ValueError()
            from PIL import Image
            raw = ctypes.string_at(pixels, stride * height)
            image = Image.frombytes('RGB', (width, height), raw, 'raw', 'BGRX', stride)
            with self.frames.condition:
                if self.closed.is_set():
                    return
                if self.native_generation is not None and generation != self.native_generation:
                    # A handover can replace a display without changing its size.
                    self.frames.epoch += 1
                    self.frames._clear()
                    self.frames.image, self.frames.dirty = None, None
                self.native_generation = generation
                self.image, self.updated_at = image, time.time()
                self.frames.update(image, [(x, y, dirty_width, dirty_height)])
                self.frames.condition.notify_all()
        except Exception:
            # Never unwind Python exceptions through a native paint callback.
            self._closed(None)

    def _check(self):
        if self.closed.is_set() or self.handle is None:
            raise RuntimeError('native RDP connection ended; reconnect and observe')

    def _call(self, name, *arguments, timeout=COMMAND_TIMEOUT_MS):
        with self.lock:
            self._check()
            try:
                result = getattr(self.library, name)(self.handle, *arguments, timeout)
                if result != 1:
                    raise RuntimeError()
            except Exception:
                self.close()
                raise RuntimeError('native RDP input outcome unknown; do not replay') from None

    def screenshot(self):
        with self.frames.condition:
            self._check()
            if self.image is None:
                raise RuntimeError('native RDP framebuffer unavailable')
            image = self.image.copy()
            generation, updated_at = self.native_generation, self.updated_at
        output = io.BytesIO()
        image.save(output, format='PNG')
        return {'png_base64': base64.b64encode(output.getvalue()).decode('ascii'),
            'generation': generation, 'source_updated_at': updated_at}

    def capabilities(self):
        return {'live': True, 'pointer': True, 'key': True, 'text': True,
            'clipboard': {'read': True, 'write': True, 'encoding': 'utf-8'}}

    def start_live(self):
        with self.lock:
            self._check()
            sequence_floor = self.frames.start()
            self.live = True
            return {'ok': True, 'capabilities': self.capabilities(), 'sequence_floor': sequence_floor}

    def next_event(self, timeout=1):
        return self.frames.next_event(timeout)

    def resync_live(self):
        self._check()
        self.frames.resync()
        return {'ok': True}

    def _coordinates(self, x, y):
        if (type(x) is not int or type(y) is not int or self.image is None
                or not 0 <= x < self.image.width or not 0 <= y < self.image.height):
            raise ValueError('native RDP coordinates outside display')

    @staticmethod
    def _native_scancode(value):
        # aardwolf's layout represents E0 as 0xe000; FreeRDP uses KBDEXT=0x100.
        return (value & 0xff) | (0x100 if value > 0xff else 0)

    def _named_key(self, key):
        aliases = {'Return': 'VK_RETURN', 'Escape': 'VK_ESCAPE', 'Tab': 'VK_TAB',
            'Control_L': 'VK_LCONTROL', 'Alt_L': 'VK_LMENU', 'Shift_L': 'VK_LSHIFT',
            'Super_L': 'VK_LWIN', 'BackSpace': 'VK_BACK', 'Delete': 'VK_DELETE',
            'Left': 'VK_LEFT', 'Right': 'VK_RIGHT', 'Up': 'VK_UP', 'Down': 'VK_DOWN'}
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_+.-]{1,40}', key):
            raise ValueError('invalid native RDP key')
        value = self.library.dl_rdp_keycode(aliases.get(key, 'VK_' + key.upper()).encode('ascii'))
        if not 0 < value <= 0x1ff:
            raise ValueError('unsupported native RDP key')
        return value

    def _clipboard_write(self, text):
        data, count = _utf16(text, MAX_CLIPBOARD_UNITS, MAX_CLIPBOARD_UNITS, clipboard=True)
        self._call('dl_rdp_clipboard_write', data, count)

    def _text(self, text):
        data, count = _utf16(text, 1024, MAX_TEXT_UNITS)
        if self.text_input == 'clipboard':
            self._clipboard_write(text)
            self._keys(['Control_L', 'v'])
        else:
            self._call('dl_rdp_text', data, count)

    def _keys(self, names):
        if not isinstance(names, list) or not 1 <= len(names) <= 4:
            raise ValueError('invalid native RDP keys')
        # Validate the whole combination before the first DOWN.
        values = [self._named_key(key) for key in names]
        for value in values:
            self._call('dl_rdp_key', value, 0, 1)
        for value in reversed(values):
            self._call('dl_rdp_key', value, 0, 0)

    def input(self, request):
        with self.lock:
            self._check()
            action = request.get('action')
            if action in ('click', 'scroll'):
                x, y = request.get('x'), request.get('y')
                self._coordinates(x, y)
                if action == 'click':
                    button = {'left': 1, 'middle': 2, 'right': 4}.get(request.get('button'))
                    if button is None:
                        raise ValueError('invalid native RDP button')
                    self.release_all()
                    self._call('dl_rdp_pointer', x, y, button, 0)
                    self._call('dl_rdp_pointer', x, y, 0, 0)
                else:
                    steps = request.get('steps')
                    if type(steps) is not int or not -20 <= steps <= 20:
                        raise ValueError('invalid native RDP wheel')
                    if steps:
                        self._call('dl_rdp_pointer', x, y, self.buttons, steps)
                self.pointer = (x, y)
            elif action == 'key':
                self._keys(request.get('keys'))
            elif action == 'text':
                self._text(request.get('text'))
            else:
                raise ValueError('unsupported native RDP action')
            return {'ok': True}

    def live_input(self, request):
        validate_live_input(request)
        with self.lock:
            self._check()
            if not self.live:
                raise RuntimeError('native RDP live attachment ended')
            kind = request['type']
            if kind in ('pointer', 'wheel'):
                x, y = request['x'], request['y']
                self._coordinates(x, y)
                buttons = request['buttons'] if kind == 'pointer' else self.buttons
                self._call('dl_rdp_pointer', x, y, buttons, request['steps'] if kind == 'wheel' else 0)
                self.buttons, self.pointer = buttons, (x, y)
            elif kind == 'key':
                method, value = RDPDesktop._live_key(request)
                value = self._native_scancode(value) if method == 'scancode' else ord(value)
                if method == 'char' and 0xd800 <= value <= 0xdfff:
                    raise ValueError('invalid native RDP Unicode key')
                self._call('dl_rdp_key', value, int(method == 'char'), int(request['down']))
            elif kind == 'text':
                self.release_all()
                self._text(request['text'])
            else:
                self._clipboard_write(request['text'])
            return {'ok': True}

    def clipboard_read(self):
        with self.lock:
            text = (ctypes.c_uint16 * MAX_CLIPBOARD_UNITS)()
            written, available = ctypes.c_uint32(), ctypes.c_uint32()
            self._call('dl_rdp_clipboard_read', text, MAX_CLIPBOARD_UNITS,
                ctypes.byref(written), ctypes.byref(available))
            if written.value > MAX_CLIPBOARD_UNITS or available.value not in (0, 1):
                self.close()
                raise RuntimeError('invalid native RDP clipboard response')
            result = {'ok': True, 'available': bool(available.value), 'encoding': 'utf-8'}
            if available.value:
                try:
                    value = ctypes.string_at(text, written.value * 2).decode('utf-16-le')
                    _utf16(value, MAX_CLIPBOARD_UNITS, MAX_CLIPBOARD_UNITS, clipboard=True)
                except (UnicodeError, ValueError):
                    self.close()
                    raise RuntimeError('invalid native RDP clipboard response') from None
                result['text'] = value
            return result

    def release_all(self):
        with self.lock:
            self._call('dl_rdp_release_all')
            self.buttons = 0
            return {'ok': True, 'release_confirmed': True}

    def stop_live(self):
        with self.lock:
            self.live = False
            self.frames.stop()
            return self.release_all()

    def close(self):
        self.closed.set()
        self.frames.close('native RDP connection ended')
        with self.lock:
            handle, self.handle = self.handle, None
            try:
                if handle is not None:
                    self.library.dl_rdp_free(handle)
            except Exception:
                raise RuntimeError('native RDP close not confirmed') from None
            finally:
                if isinstance(self.library, NativeLibrary):
                    self.library.close()
