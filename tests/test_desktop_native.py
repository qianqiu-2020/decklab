"""Pinned native RDP adapter regressions; generated packets, no network or boards."""
import asyncio
import importlib.util
import io
import enum
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

from decklab_core.desktop_providers import RDPDesktop, VNCDesktop, LiveFrames, validate_live_input


@unittest.skipUnless(importlib.util.find_spec('PIL'), 'optional desktop image dependency not installed')
class VNCStreamTests(unittest.TestCase):
    """The pinned client's callback boundary, without a server or reactor."""
    def setUp(self):
        from PIL import Image
        class Client:
            def __init__(self):
                self.screen = Image.new('RGB', (8, 6), 'black')
                self.x, self.y, self.buttons = 0, 0, 0
                self.events, self.requests, self.captures = [], [], 0

            def captureScreen(self, output, format):
                self.captures += 1
                self.screen.save(output, format=format)
                self.commitUpdate([(0, 0, 8, 6)])

            def commitUpdate(self, rectangles=None):
                pass

            def updateDesktopSize(self, width, height):
                self.screen = Image.new('RGB', (width, height), 'black')

            def updateRectangle(self, x, y, width, height, data):
                self.screen.paste(Image.frombytes('RGB', (width, height), data), (x, y))

            def framebufferUpdateRequest(self, incremental=False):
                self.requests.append(incremental)

            def pointerEvent(self, x, y, buttonmask=0):
                self.events.append(('pointer', x, y, buttonmask))

            def keyEvent(self, key, down=True):
                self.events.append(('key', key, down))

            def _decodeKey(self, key):
                return [ord(key)]

            def keyPress(self, key):
                for value in self._decodeKey(key):
                    self.keyEvent(value, True)
                    self.keyEvent(value, False)
                return self

            def pause(self, seconds):
                return self

            def paste(self, text):
                self.events.append(('clipboard', text))

            def disconnect(self):
                self.events.append(('disconnect',))

            def connectionLost(self, reason):
                pass
        root, client = types.ModuleType('vncdotool'), types.ModuleType('vncdotool.client')
        client.VNCDoToolClient = Client
        client.VNCDoToolFactory = type('Factory', (), {})
        root.api = types.SimpleNamespace(connect=lambda *args, **kw: kw['factory_class'].protocol())
        self.modules = patch.dict(sys.modules, {'vncdotool': root, 'vncdotool.client': client})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.adapter = VNCDesktop({'secret_url': 'vnc+plain://synthetic@127.0.0.1:5900'})
        self.addCleanup(self.adapter.close)

    def test_native_commit_drives_rectangles_and_static_screen_does_not_poll(self):
        from PIL import Image
        result = self.adapter.start_live()
        first = self.adapter.next_event(1)
        self.assertGreater(first['sequence'], result['sequence_floor'])
        self.assertTrue(first['reset'])
        self.assertEqual(self.adapter.client.requests, [True])
        self.assertIsNone(self.adapter.next_event(.03))
        self.adapter.client.updateRectangle(2, 1, 2, 2, bytes([255, 0, 0]) * 4)
        self.adapter.client.commitUpdate([(2, 1, 2, 2)])
        frame = self.adapter.next_event(1)
        self.assertFalse(frame['reset'])
        self.assertEqual((frame['x'], frame['y'], frame['rect_width'], frame['rect_height']), (2, 1, 2, 2))
        image = Image.open(io.BytesIO(__import__('base64').b64decode(frame['png_base64'])))
        self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))
        self.adapter.screenshot()
        self.assertEqual(self.adapter.client.captures, 1, 'live screenshot reads the callback framebuffer')
        self.assertEqual(self.adapter.client.requests, [True, True])
        restarted = self.adapter.start_live()
        reset = self.adapter.next_event(1)
        self.assertTrue(reset['reset'])
        self.assertGreater(reset['sequence'], restarted['sequence_floor'])
        self.assertEqual(self.adapter.client.requests, [True, True])

    def test_held_pointer_keys_clipboard_and_cleanup_use_native_primitives(self):
        self.adapter.start_live()
        for request in ({'type': 'pointer', 'x': 1, 'y': 1, 'buttons': 1},
                {'type': 'pointer', 'x': 3, 'y': 2, 'buttons': 1},
                {'type': 'key', 'keysym': 0xffe3, 'code': 'ControlLeft', 'down': True},
                {'type': 'wheel', 'x': 3, 'y': 2, 'steps': 1}):
            self.adapter.live_input(request)
        self.assertEqual(self.adapter.client.events[:2], [('pointer', 1, 1, 1), ('pointer', 3, 2, 1)])
        self.assertEqual(self.adapter.client.events[-2:], [('pointer', 3, 2, 17), ('pointer', 3, 2, 1)])
        self.adapter.live_input({'type': 'clipboard', 'text': 'caf\u00e9'})
        self.assertEqual(self.adapter.client.events[-1], ('clipboard', 'caf\u00e9'))
        with self.assertRaises(UnicodeEncodeError):
            self.adapter.live_input({'type': 'clipboard', 'text': '\u4e2d\u6587'})
        self.assertFalse(self.adapter.clipboard_read()['available'])
        self.adapter.client.copy_text('remote text')
        self.assertEqual(self.adapter.clipboard_read()['text'], 'remote text')
        result = self.adapter.stop_live()
        self.assertIs(result['release_confirmed'], True)
        self.assertEqual(self.adapter.client.events[-2:], [('key', 0xffe3, False), ('pointer', 3, 2, 0)])
        count = len(self.adapter.client.events)
        self.adapter.release_all()
        self.assertEqual(len(self.adapter.client.events), count)
        with self.assertRaises(RuntimeError):
            self.adapter.live_input({'type': 'pointer', 'x': 0, 'y': 0, 'buttons': 0})

    def test_connection_loss_rejects_cached_frame_and_reports_closed(self):
        self.adapter.start_live()
        self.adapter.next_event(1)
        self.adapter.live_input({'type': 'key', 'keysym': 97, 'code': 'KeyA', 'down': True})
        self.adapter.client.connectionLost(None)
        self.assertEqual(self.adapter.next_event(1)['type'], 'closed')
        with self.assertRaises(ConnectionError):
            self.adapter.screenshot()
        with self.assertRaises(RuntimeError):
            self.adapter.live_input({'type': 'key', 'keysym': 97, 'code': 'KeyA', 'down': True})
        with self.assertRaisesRegex(RuntimeError, 'unknown'):
            self.adapter.release_all()

    def test_live_text_retains_unicode_input_separate_from_latin1_clipboard_and_clears_held(self):
        self.adapter.start_live()
        self.adapter.live_input({'type': 'key', 'keysym': 97, 'code': 'KeyA', 'down': True})
        self.adapter.live_input({'type': 'text', 'text': '\u4e2d\u6587\U0001f642a'})
        self.assertTrue(self.adapter.capabilities()['text'])
        self.assertEqual(self.adapter.capabilities()['clipboard']['encoding'], 'latin-1')
        self.assertEqual(self.adapter.client.events[1], ('key', 97, False))
        self.assertEqual(self.adapter.client._deck_keys, set())
        self.assertEqual(self.adapter.client.events[2:], [
            ('key', 0x01004e2d, True), ('key', 0x01004e2d, False),
            ('key', 0x01006587, True), ('key', 0x01006587, False),
            ('key', 0x0101f642, True), ('key', 0x0101f642, False),
            ('key', 97, True), ('key', 97, False)])
        with self.assertRaises(UnicodeEncodeError):
            self.adapter.live_input({'type': 'clipboard', 'text': '\u4e2d\u6587'})


@unittest.skipUnless(importlib.util.find_spec('PIL'), 'optional desktop image dependency not installed')
class RDPStreamBoundaryTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        class Mouse(enum.Enum):
            MOUSEBUTTON_HOVER = -1
            MOUSEBUTTON_LEFT = 0
            MOUSEBUTTON_RIGHT = 1
            MOUSEBUTTON_MIDDLE = 2
            MOUSEBUTTON_WHEEL_UP = 3
        module = types.ModuleType('aardwolf.commons.queuedata.constants')
        module.MOUSEBUTTON = Mouse
        self.modules = patch.dict(sys.modules, {'aardwolf.commons.queuedata.constants': module})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.mouse = Mouse
        self.adapter = RDPDesktop.__new__(RDPDesktop)
        self.adapter.image = Image.new('RGB', (8, 6), 'black')
        self.adapter.frames = LiveFrames()
        self.addCleanup(self.adapter.frames.close)
        self.adapter.live = True
        self.adapter.held_keys, self.adapter.buttons, self.adapter.pointer = {}, 0, (0, 0)
        self.adapter.pump, self.adapter.slow_pump = None, None
        self.adapter.connection = types.SimpleNamespace(
            send_mouse=AsyncMock(return_value=(True, None)),
            send_key_scancode=AsyncMock(return_value=(True, None)),
            send_key_char=AsyncMock(return_value=(True, None)))

    def test_live_start_returns_native_sequence_watermark(self):
        self.adapter.frames.update(self.adapter.image)
        with patch.object(self.adapter, 'call', side_effect=asyncio.run):
            started = self.adapter.start_live()
            first = self.adapter.next_event(1)
            self.assertGreater(first['sequence'], started['sequence_floor'])
            restarted = self.adapter.start_live()
        reset = self.adapter.next_event(1)
        self.assertTrue(reset['reset'])
        self.assertGreaterEqual(restarted['sequence_floor'], first['sequence'])
        self.assertGreater(reset['sequence'], restarted['sequence_floor'])

    def test_drag_native_move_and_release_all_never_synthesize_down(self):
        async def run():
            await self.adapter._send_live({'type': 'pointer', 'x': 1, 'y': 2, 'buttons': 1})
            await self.adapter._send_live({'type': 'pointer', 'x': 4, 'y': 3, 'buttons': 1})
            self.assertEqual([c.args for c in self.adapter.connection.send_mouse.call_args_list],
                [(self.mouse.MOUSEBUTTON_LEFT, 1, 2, True), (self.mouse.MOUSEBUTTON_HOVER, 1, 2, False),
                 (self.mouse.MOUSEBUTTON_HOVER, 4, 3, False)])
            self.adapter.held_keys = {'ControlLeft': ('scancode', 29), 'KeyA': ('scancode', 30)}
            result = await self.adapter._release_all()
            self.assertIs(result['release_confirmed'], True)
            self.assertEqual([c.args for c in self.adapter.connection.send_key_scancode.call_args_list],
                [(30, False, False), (29, False, False)])
            self.assertEqual(self.adapter.connection.send_mouse.call_args.args,
                (self.mouse.MOUSEBUTTON_LEFT, 4, 3, False))
            self.assertEqual(self.adapter.held_keys, {})
            self.assertEqual(self.adapter.buttons, 0)
        asyncio.run(run())

    def test_release_attempts_all_held_inputs_and_failure_retains_unknown_state(self):
        async def run():
            self.adapter.held_keys = {'KeyA': ('scancode', 30), 'KeyB': ('scancode', 48)}
            self.adapter.buttons = 1
            self.adapter.connection.send_key_scancode.side_effect = [(None, RuntimeError()), (True, None)]
            with self.assertRaisesRegex(RuntimeError, 'unknown'):
                await self.adapter._release_all()
            self.assertEqual(self.adapter.connection.send_key_scancode.call_count, 2)
            self.adapter.connection.send_mouse.assert_awaited_once()
            self.assertEqual(self.adapter.held_keys, {'KeyB': ('scancode', 48)})
        asyncio.run(run())

    def test_clipboard_write_only_and_explicit_native_read(self):
        async def run():
            self.adapter.clipboard_ready, self.adapter.clipboard_replies = asyncio.Event(), asyncio.Queue()
            self.adapter.clipboard_ready.set()
            async def copy(text):
                self.adapter.clipboard_replies.put_nowait(True)
            self.adapter.connection.set_current_clipboard_text = AsyncMock(side_effect=copy)
            self.adapter.connection.get_current_clipboard_text = AsyncMock(return_value='\u4e2d\u6587')
            await self.adapter._send_live({'type': 'clipboard', 'text': '\u4e2d\u6587'})
            self.adapter.connection.send_key_scancode.assert_not_awaited()
            self.adapter.connection.send_key_char.assert_not_awaited()
            self.assertEqual((await self.adapter._clipboard_read())['text'], '\u4e2d\u6587')
            self.adapter.connection.get_current_clipboard_text.return_value = None
            self.assertFalse((await self.adapter._clipboard_read())['available'])
        asyncio.run(run())

    def test_disconnected_native_state_rejects_cached_capture_and_input(self):
        async def run():
            self.adapter.last_update = time.time()
            self.adapter.connection.disconnected_evt = asyncio.Event()
            self.adapter.connection.disconnected_evt.set()
            with self.assertRaises(ConnectionError):
                await self.adapter.capture()
            with self.assertRaises(ConnectionError):
                await self.adapter._send_live({'type': 'pointer', 'x': 1, 'y': 2, 'buttons': 1})
            self.adapter.connection.send_mouse.assert_not_awaited()
            self.adapter.held_keys = {'KeyA': ('scancode', 30)}
            with self.assertRaisesRegex(RuntimeError, 'unknown'):
                await self.adapter._release_all()
        asyncio.run(run())

    def test_live_input_validation_rejects_boolean_coordinates_and_oversize_clipboard(self):
        for value in ({'type': 'pointer', 'x': True, 'y': 0, 'buttons': 0},
                {'type': 'key', 'keysym': 97, 'down': 'true'},
                {'type': 'clipboard', 'text': 'x' * 65537},
                {'type': 'wheel', 'x': 0, 'y': 0, 'steps': 0}):
            with self.assertRaises(ValueError):
                validate_live_input(value)

    def test_live_text_uses_existing_rdp_text_mode_after_clearing_held_state(self):
        async def run():
            self.assertTrue(self.adapter.capabilities()['text'])
            self.adapter.held_keys = {'ControlLeft': ('scancode', 29)}
            self.adapter.send = AsyncMock(return_value={'ok': True})
            for mode in ('unicode', 'clipboard'):
                self.adapter.text_input = mode
                self.adapter.send.reset_mock()
                result = await self.adapter._send_live({'type': 'text', 'text': '\u4e2d\u6587'})
                self.assertIs(result['ok'], True)
                self.adapter.send.assert_awaited_once_with({'action': 'text', 'text': '\u4e2d\u6587'})
                self.assertEqual(self.adapter.text_input, mode)
                self.assertEqual(self.adapter.held_keys, {})
            self.adapter.connection.send_key_scancode.assert_awaited_once_with(29, False, False)
        asyncio.run(run())

    def test_live_text_validation_matches_character_limit_and_rejects_invalid_unicode_before_input(self):
        validate_live_input({'type': 'text', 'text': '\U0001f642' * 1024})
        for value in ('x' * 1025, 'before\0after', '\ud800', '\udfff', None):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                validate_live_input({'type': 'text', 'text': value})


@unittest.skipUnless(importlib.util.find_spec('aardwolf'), 'optional native desktop dependencies not installed')
class NativeDesktopTests(unittest.TestCase):
    def test_live_physical_keys_use_native_layout_and_keep_keypad_distinct(self):
        for code, keysym, expected in (('KeyA', 97, 30), ('ControlRight', 0xffe4, 0xe01d),
                ('ArrowLeft', 0xff51, 0xe04b), ('Enter', 0xff0d, 28),
                ('NumpadEnter', 0xff0d, 0xe01c), ('Numpad1', 0xffb1, 0x4f),
                ('NumpadDecimal', 0xffae, 0x53)):
            self.assertEqual(RDPDesktop._live_key({'code': code, 'keysym': keysym}), ('scancode', expected))
        self.assertEqual(RDPDesktop._live_key({'code': '', 'keysym': 0x01004e2d}), ('char', '\u4e2d'))

    @unittest.skipUnless(importlib.util.find_spec('vncdotool'), 'optional native VNC dependency not installed')
    def test_vnc_live_methods_preserve_real_threaded_proxy_deferred_chain(self):
        from PIL import Image
        from vncdotool import api
        from unittest.mock import Mock
        clients = []
        def connect(*args, **kwargs):
            factory = kwargs['factory_class']()
            protocol = factory.protocol()
            protocol.factory, protocol.transport = factory, Mock()
            protocol.width, protocol.height = 8, 6
            protocol.screen = Image.new('RGB', (8, 6), 'black')
            def capture(output, format):
                protocol.screen.save(output, format=format)
                protocol.commitUpdate([(0, 0, 8, 6)])
            protocol.captureScreen, protocol.disconnect = capture, lambda: None
            clients.append(protocol)
            return protocol
        with patch.object(api, 'connect', side_effect=connect):
            adapter = VNCDesktop({'secret_url': 'vnc+plain://synthetic@127.0.0.1:5900'})
        self.addCleanup(adapter.close)
        protocol = clients[0]
        proxy = api.ThreadedVNCClientProxy(protocol.factory, timeout=.1)
        protocol.factory.deferred.callback(protocol)
        # Dispatch on this thread, retaining the real native proxy, Deferred,
        # queue and method lookup. No reactor/socket/display is started.
        with patch.object(api.reactor, 'callFromThread', side_effect=lambda fn, *args, **kw: fn(*args, **kw)):
            self.assertIs(proxy.deck_start_live(), protocol)
            self.assertIs(proxy.deck_live_input({'type': 'pointer', 'x': 1, 'y': 2, 'buttons': 1}), protocol)
            self.assertIs(proxy.deck_live_input({'type': 'key', 'keysym': 97, 'code': 'KeyA', 'down': True}), protocol)
            self.assertIs(proxy.deck_live_input({'type': 'clipboard', 'text': 'native proxy'}), protocol)
            self.assertIs(proxy.deck_release_all(), protocol)
            self.assertIs(proxy.deck_stop_live(), protocol)
            # A subsequent upstream method must still receive the protocol,
            # rather than None from a preceding custom hook.
            self.assertIs(proxy.keyPress('b'), protocol)
            self.assertIs(proxy.deck_start_live(), protocol)
        self.assertEqual(protocol.buttons, 0)
        self.assertEqual(protocol._deck_keys, set())

    @unittest.skipUnless(importlib.util.find_spec('vncdotool'), 'optional native VNC dependency not installed')
    def test_vnc_stream_hooks_reuse_real_native_decoder_and_input_methods(self):
        from PIL import Image
        from vncdotool import api
        from unittest.mock import Mock
        clients = []
        def connect(*args, **kwargs):
            factory = kwargs['factory_class']()
            protocol = factory.protocol()
            protocol.factory, protocol.transport = factory, Mock()
            protocol.width, protocol.height = 8, 6
            protocol.screen = Image.new('RGB', (8, 6), 'black')
            protocol._image_mode = 'RGB'
            def capture(output, format):
                protocol.screen.save(output, format=format)
                protocol.commitUpdate([(0, 0, 8, 6)])
            protocol.captureScreen, protocol.disconnect = capture, lambda: None
            clients.append(protocol)
            return protocol
        with patch.object(api, 'connect', side_effect=connect):
            adapter = VNCDesktop({'secret_url': 'vnc+plain://synthetic@127.0.0.1:5900'})
        self.addCleanup(adapter.close)
        adapter.start_live()
        self.assertTrue(adapter.next_event(1)['reset'])
        protocol = clients[0]
        protocol.updateRectangle(1, 2, 2, 1, bytes([255, 0, 0]) * 2)
        protocol.commitUpdate([(1, 2, 2, 1)])
        event = adapter.next_event(1)
        self.assertFalse(event['reset'])
        self.assertEqual((event['x'], event['y'], event['rect_width'], event['rect_height']), (1, 2, 2, 1))
        adapter.live_input({'type': 'pointer', 'x': 1, 'y': 2, 'buttons': 1})
        adapter.live_input({'type': 'key', 'keysym': 0xffe3, 'code': 'ControlLeft', 'down': True})
        adapter.live_input({'type': 'clipboard', 'text': 'caf\u00e9'})
        self.assertIs(adapter.release_all()['release_confirmed'], True)
        self.assertGreater(protocol.transport.write.call_count, 4)
        self.assertEqual(protocol.buttons, 0)

    def test_slow_path_padded_bitmap_uses_encoded_width_and_clips_visible_rectangle(self):
        from PIL import Image
        from aardwolf.protocol.T128.share import TS_SHAREDATAHEADER, TS_SHARECONTROLHEADER, CompType, PDUTYPE2, PDUTYPE, STREAM_TYPE
        from aardwolf.protocol.fastpath.bitmap import TS_BITMAP_DATA, TS_BITMAP_FLAG, TS_UPDATE_BITMAP_DATA
        async def check():
            adapter = RDPDesktop.__new__(RDPDesktop)
            adapter.image = Image.new('RGB', (8, 4))
            adapter.last_update = 0
            bitmap = TS_BITMAP_DATA()
            bitmap.destLeft, bitmap.destTop, bitmap.destRight, bitmap.destBottom = 1, 1, 3, 1
            bitmap.width, bitmap.height, bitmap.bitsPerPixel = 4, 1, 16
            bitmap.flags = TS_BITMAP_FLAG(0)
            bitmap.bitmapDataStream = bytes.fromhex('00f8e0071f000000')
            update = TS_UPDATE_BITMAP_DATA()
            update.updateType, update.rectangles = 1, [bitmap]
            data = update.to_bytes()
            header = TS_SHAREDATAHEADER()
            header.shareID, header.streamID, header.pduType2 = 1, STREAM_TYPE.MED, PDUTYPE2.UPDATE
            header.shareControlHeader = TS_SHARECONTROLHEADER()
            header.shareControlHeader.totalLength = 18 + len(data)
            header.uncompressedLength = 12 + len(data)
            header.compressedType, header.compressedLength = CompType(0), 0
            header.shareControlHeader.pduType = PDUTYPE.DATAPDU
            header.shareControlHeader.pduVersion, header.shareControlHeader.pduSource = 1, 1003
            raw = header.to_bytes() + data
            queue = asyncio.Queue()
            await queue.put((raw, None))
            task = asyncio.create_task(adapter.consume_slow(queue))
            for _ in range(20):
                if adapter.last_update or task.done():
                    break
                await asyncio.sleep(.01)
            if task.done():
                task.result()
            self.assertGreater(adapter.last_update, 0)
            self.assertGreater(adapter.image.getpixel((1, 1))[0], 240)
            self.assertGreater(adapter.image.getpixel((2, 1))[1], 240)
            self.assertGreater(adapter.image.getpixel((3, 1))[2], 240)
            self.assertEqual(adapter.image.getpixel((4, 1)), (0, 0, 0))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        asyncio.run(check())

    def test_unicode_text_and_extended_keys_use_native_input_types(self):
        async def check():
            adapter = RDPDesktop.__new__(RDPDesktop)
            class Connection:
                send_key_char = AsyncMock(return_value=(True, None))
                send_key_scancode = AsyncMock(return_value=(True, None))
            adapter.connection = Connection()
            await adapter.send({'action': 'text', 'text': '中文'})
            self.assertEqual([c.args for c in adapter.connection.send_key_char.call_args_list],
                [('中', True), ('中', False), ('文', True), ('文', False)])
            await adapter.send({'action': 'key', 'keys': ['Left']})
            self.assertEqual(adapter.connection.send_key_scancode.call_args_list[0].args, (0xe04b, True, True))
            for key, code in (('Return', 28), ('Tab', 15)):
                adapter.connection.send_key_scancode.reset_mock()
                await adapter.send({'action': 'key', 'keys': [key]})
                self.assertEqual(adapter.connection.send_key_scancode.call_args_list[0].args, (code, True, False))
        asyncio.run(check())

    def test_signed_wheel_and_explicit_clipboard_paste_use_native_methods(self):
        from aardwolf.commons.queuedata.constants import MOUSEBUTTON
        async def check():
            adapter = RDPDesktop.__new__(RDPDesktop)
            adapter.connection = type('Connection', (), {
                'send_mouse': AsyncMock(return_value=(True, None)),
                'send_key_scancode': AsyncMock(return_value=(True, None)),
                'send_key_char': AsyncMock(return_value=(True, None))})()
            for steps, rotation in ((2, -120), (-1, 120)):
                adapter.connection.send_mouse.reset_mock()
                await adapter.send({'action': 'scroll', 'x': 2, 'y': 3, 'steps': steps})
                self.assertEqual(adapter.connection.send_mouse.call_count, abs(steps))
                self.assertEqual(adapter.connection.send_mouse.call_args.args,
                    (MOUSEBUTTON.MOUSEBUTTON_WHEEL_UP, 2, 3, False))
                self.assertEqual(adapter.connection.send_mouse.call_args.kwargs, {'steps': rotation})
            adapter.text_input = 'clipboard'
            adapter.clipboard_ready, adapter.clipboard_replies = asyncio.Event(), asyncio.Queue()
            adapter.clipboard_ready.set()
            async def copy(text):
                self.assertEqual(text, '中文')
                adapter.clipboard_replies.put_nowait(True)
            adapter.connection.set_current_clipboard_text = AsyncMock(side_effect=copy)
            await adapter.send({'action': 'text', 'text': '中文'})
            adapter.connection.set_current_clipboard_text.assert_awaited_once_with('中文')
            adapter.connection.send_key_char.assert_not_called()
            self.assertEqual([c.args for c in adapter.connection.send_key_scancode.call_args_list],
                [(29, True, False), (47, True, False), (47, False, False), (29, False, False)])
            async def reject(text):
                adapter.clipboard_replies.put_nowait(False)
            adapter.connection.set_current_clipboard_text.side_effect = reject
            adapter.connection.send_key_scancode.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'rejected'):
                await adapter.send({'action': 'text', 'text': '中文'})
            adapter.connection.send_key_scancode.assert_not_called()
        asyncio.run(check())
