"""FreeRDP ABI boundary tests with generated pixels; no network or board IO."""
import base64
import ctypes
import io
from pathlib import Path
import time
import unittest
from unittest import mock

try:
    from PIL import Image
except ImportError:
    Image = None

from seriald_core.desktop_freerdp import (ABI_VERSION, MAX_CLIPBOARD_UNITS,
    FreeRDPDesktop, _Options, _P32, _library_path)
from seriald_core.desktop_errors import DesktopConnectionError


CONFIG = {'provider': 'rdp', 'engine': 'freerdp',
    'secret_url': 'rdp+ntlm-password://DOMAIN%5Ctester:private-password@configured-host:3389',
    'certificate_sha256': 'ab' * 32, 'display': 'test-monitor', 'display_kind': 'virtual'}


class FakeLibrary:
    def __init__(self):
        self.calls, self.free_count = [], 0
        self.failure, self.clipboard = None, '远端\U0001f600'
        self.clipboard_available, self.bad_clipboard_size = True, False
        self.bad_clipboard_utf16 = False
        self.version = ABI_VERSION

    def dl_rdp_abi_version(self):
        return self.version

    def dl_rdp_new(self, options, frame, closed, user):
        value = ctypes.cast(options, ctypes.POINTER(_Options)).contents
        self.options = {'host': value.hostname, 'username': value.username,
            'domain': value.domain, 'port': value.port, 'pin': value.certificate_sha256,
            'size': value.size, 'abi': value.abi_version}
        self.frame, self.closed = frame, closed
        return 42

    def dl_rdp_start(self, handle, timeout):
        self.calls.append(('start', timeout))
        self.emit()
        return 0 if self.failure == 'start' else 1

    def dl_rdp_error(self, handle, reason, capacity):
        raw = b'certificate: system trust or hostname verification rejected; ERRCONNECT_TLS_CONNECT_FAILED'
        ctypes.memmove(reason, raw + b'\0', min(capacity, len(raw) + 1))
        return 0x20008

    def emit(self, generation=1, red=50, width=4, height=3, dirty=None):
        self.pixels = (ctypes.c_uint8 * (width * height * 4))(*([0, 0, red, 0] * (width * height)))
        x, y, w, h = dirty or (0, 0, width, height)
        self.frame(None, self.pixels, width, height, width * 4, x, y, w, h, generation)

    def dl_rdp_keycode(self, name):
        return {b'VK_LEFT': 0x14b, b'VK_RIGHT': 0x14d, b'VK_RETURN': 0x1c,
            b'VK_TAB': 0x0f, b'VK_LCONTROL': 0x1d, b'VK_V': 0x2f}.get(name, 0)

    def _call(self, kind, *arguments):
        self.calls.append((kind, *arguments))
        return int(self.failure != kind)

    def dl_rdp_pointer(self, handle, x, y, buttons, steps, timeout):
        return self._call('pointer', x, y, buttons, steps)

    def dl_rdp_key(self, handle, value, unicode_key, down, timeout):
        return self._call('key', value, unicode_key, down)

    def dl_rdp_text(self, handle, text, units, timeout):
        return self._call('text', ctypes.string_at(text, units * 2))

    def dl_rdp_clipboard_write(self, handle, text, units, timeout):
        return self._call('clipboard-write', ctypes.string_at(text, units * 2))

    def dl_rdp_clipboard_read(self, handle, text, capacity, written, available, timeout):
        raw = b'\x00\xd8' if self.bad_clipboard_utf16 else self.clipboard.encode('utf-16-le')
        ctypes.memmove(text, raw, len(raw))
        ctypes.cast(written, _P32).contents.value = MAX_CLIPBOARD_UNITS + 1 if self.bad_clipboard_size else len(raw) // 2
        ctypes.cast(available, _P32).contents.value = int(self.clipboard_available)
        return self._call('clipboard-read')

    def dl_rdp_release_all(self, handle, timeout):
        return self._call('release')

    def dl_rdp_free(self, handle):
        self.free_count += 1
        self.closed(None)


@unittest.skipUnless(Image, 'optional desktop image dependency not installed')
class FreeRDPTests(unittest.TestCase):
    def provider(self, library=None, **config):
        library = library or FakeLibrary()
        provider = FreeRDPDesktop(dict(CONFIG, **config), library=library)
        self.addCleanup(provider.close)
        return provider, library

    def event(self, provider):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            event = provider.next_event(timeout=.05)
            if event:
                return event
        self.fail('native frame was not encoded')

    def test_credentials_and_pin_use_typed_abi_not_arguments(self):
        provider, library = self.provider()
        self.assertEqual(library.options, {'host': b'configured-host', 'username': b'tester',
            'domain': b'DOMAIN', 'port': 3389, 'pin': b'ab' * 32,
            'size': ctypes.sizeof(_Options), 'abi': ABI_VERSION})
        self.assertTrue(provider.capabilities()['clipboard']['write'])
        for value in ('wrong', 'aa:' * 32, '', None):
            if value == '':
                _, native = self.provider(certificate_sha256='')
                self.assertEqual(native.options['pin'], b'')
            else:
                with self.assertRaises(DesktopConnectionError) as caught:
                    FreeRDPDesktop(dict(CONFIG, certificate_sha256=value), library=FakeLibrary())
                self.assertEqual(caught.exception.code, 'desktop-configuration-failed')

    def test_accept_current_certificate_is_distinct_from_a_pin_and_system_trust(self):
        _, library = self.provider(certificate_sha256='', certificate_policy='accept')
        self.assertEqual(library.options['pin'], b'accept-current')
        with self.assertRaises(DesktopConnectionError):
            self.provider(certificate_policy='accept')

    def test_native_borrowed_frame_is_copied_and_png_is_in_memory(self):
        provider, library = self.provider()
        library.pixels[2] = 200
        capture = provider.screenshot()
        image = Image.open(io.BytesIO(base64.b64decode(capture['png_base64'])))
        self.assertEqual(image.size, (4, 3))
        self.assertEqual(image.getpixel((0, 0)), (50, 0, 0))
        self.assertEqual(capture['generation'], 1)

    def test_event_stream_and_same_size_handover_retire_old_generation(self):
        provider, library = self.provider()
        floor = provider.start_live()['sequence_floor']
        original = self.event(provider)
        self.assertTrue(original['reset'])
        self.assertGreater(original['sequence'], floor)
        library.emit(generation=2, red=100)
        replacement = self.event(provider)
        self.assertTrue(replacement['reset'])
        self.assertGreater(replacement['generation'], original['generation'])
        self.assertEqual(provider.screenshot()['generation'], 2)
        provider.stop_live()
        self.assertFalse(provider.live)
        self.assertEqual(library.calls[-1], ('release',))

    def test_live_pointer_buttons_and_wheel_have_existing_meaning(self):
        provider, library = self.provider()
        provider.start_live()
        provider.live_input({'type': 'pointer', 'x': 2, 'y': 1, 'buttons': 3})
        provider.live_input({'type': 'wheel', 'x': 2, 'y': 1, 'steps': 2})
        self.assertEqual(library.calls[-2:], [('pointer', 2, 1, 3, 0), ('pointer', 2, 1, 3, 2)])
        provider.release_all()
        self.assertEqual(provider.buttons, 0)
        for request in ({'type': 'pointer', 'x': 4, 'y': 1, 'buttons': 0},
                {'type': 'wheel', 'x': 0, 'y': 0, 'steps': 21}):
            before = len(library.calls)
            with self.assertRaises(ValueError):
                provider.live_input(request)
            self.assertEqual(len(library.calls), before)

    def test_named_and_live_keys_keep_navigation_and_ordinary_enter_tab(self):
        provider, library = self.provider()
        for name, expected in (('Left', 0x14b), ('Return', 0x1c), ('Tab', 0x0f)):
            provider.input({'action': 'key', 'keys': [name]})
            self.assertEqual(library.calls[-2:], [('key', expected, 0, 1), ('key', expected, 0, 0)])
        before = len(library.calls)
        with self.assertRaises(ValueError):
            provider.input({'action': 'key', 'keys': ['Return', 'not-a-key']})
        self.assertEqual(len(library.calls), before)
        provider.start_live()
        with mock.patch('seriald_core.desktop_freerdp.RDPDesktop._live_key', return_value=('scancode', 0xe04b)):
            provider.live_input({'type': 'key', 'keysym': 0xff51, 'code': 'ArrowLeft', 'down': True})
        self.assertEqual(library.calls[-1], ('key', 0x14b, 0, 1))
        before = len(library.calls)
        with mock.patch('seriald_core.desktop_freerdp.RDPDesktop._live_key', return_value=('char', '\ud800')):
            with self.assertRaises(ValueError):
                provider.live_input({'type': 'key', 'keysym': 0x0100d800, 'code': '', 'down': True})
        self.assertEqual(len(library.calls), before)

    def test_unicode_is_utf16_and_clipboard_mode_is_confirmed_before_paste(self):
        provider, library = self.provider()
        provider.input({'action': 'text', 'text': '中文\U0001f600'})
        self.assertEqual(library.calls[-1], ('text', '中文\U0001f600'.encode('utf-16-le')))
        for value in ('\ud800', 'contains\0nul', 'a' * 1025):
            before = len(library.calls)
            with self.assertRaises(ValueError):
                provider.input({'action': 'text', 'text': value})
            self.assertEqual(len(library.calls), before)
        provider, library = self.provider(text_input='clipboard')
        provider.input({'action': 'text', 'text': '中文'})
        self.assertEqual(library.calls[-5:], [('clipboard-write', '中文'.encode('utf-16-le')),
            ('key', 0x1d, 0, 1), ('key', 0x2f, 0, 1), ('key', 0x2f, 0, 0), ('key', 0x1d, 0, 0)])

    def test_clipboard_utf8_bounds_and_malformed_native_reply_close_transport(self):
        provider, library = self.provider()
        self.assertEqual(provider.clipboard_read()['text'], '远端\U0001f600')
        library.clipboard_available = False
        self.assertNotIn('text', provider.clipboard_read())
        provider.start_live()
        before = len(library.calls)
        with self.assertRaises(ValueError):
            provider.live_input({'type': 'clipboard', 'text': '中' * 22000})
        self.assertEqual(len(library.calls), before)
        for invalid in ('bad_clipboard_size', 'bad_clipboard_utf16'):
            provider, library = self.provider()
            setattr(library, invalid, True)
            with self.assertRaisesRegex(RuntimeError, 'clipboard response'):
                provider.clipboard_read()
            self.assertEqual(library.free_count, 1)

    def test_failed_input_is_not_replayed_and_transport_is_closed_once(self):
        provider, library = self.provider()
        library.failure = 'key'
        with self.assertRaisesRegex(RuntimeError, 'outcome unknown'):
            provider.input({'action': 'key', 'keys': ['Return']})
        self.assertEqual([c for c in library.calls if c[0] == 'key'], [('key', 0x1c, 0, 1)])
        self.assertEqual(library.free_count, 1)
        with self.assertRaises(RuntimeError):
            provider.input({'action': 'key', 'keys': ['Return']})
        provider.close()
        self.assertEqual(library.free_count, 1)

    def test_invalid_native_frame_closes_observation_without_callback_unwind(self):
        provider, library = self.provider()
        library.emit(dirty=(4, 0, 1, 1))
        self.assertTrue(provider.closed.is_set())
        with self.assertRaises(RuntimeError):
            provider.screenshot()
        provider.close()
        self.assertEqual(library.free_count, 1)

    def test_initialization_reports_native_reason_before_cleanup_and_abi_error_is_distinct(self):
        library = FakeLibrary()
        library.failure = 'start'
        with self.assertRaises(DesktopConnectionError) as caught:
            FreeRDPDesktop(CONFIG, library=library)
        self.assertEqual(caught.exception.code, 'desktop-certificate-rejected')
        self.assertEqual(caught.exception.details['native_code'], '0x00020008')
        self.assertIn('ERRCONNECT_TLS_CONNECT_FAILED', str(caught.exception))
        self.assertNotIn('private-password', str(caught.exception))
        self.assertEqual(library.free_count, 1)
        library = FakeLibrary()
        library.version = 999
        with self.assertRaises(DesktopConnectionError) as caught:
            FreeRDPDesktop(CONFIG, library=library)
        self.assertEqual(caught.exception.code, 'desktop-runtime-failed')
        self.assertIn('unsupported native RDP ABI', str(caught.exception))
        self.assertEqual(library.free_count, 0)

    def test_loader_uses_only_fixed_private_resource_directory(self):
        with mock.patch('seriald_core.desktop_freerdp.sys._MEIPASS', '/private/bundle', create=True), \
                mock.patch('seriald_core.desktop_freerdp.sys.platform', 'win32'):
            self.assertEqual(_library_path(), Path('/private/bundle/desktop-runtime/freerdp/decklab-freerdp.dll'))


if __name__ == '__main__':
    unittest.main()
