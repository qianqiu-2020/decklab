"""Adapters for existing desktop clients; no RFB/RDP protocol implementation."""
import base64
import asyncio
import copy
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from .desktop_errors import DesktopConnectionError, connection_error, redact


MAX_MESSAGE_BYTES = 12 * 1024 ** 2
MAX_FRAME_BYTES = 8 * 1024 ** 2
MAX_CLIPBOARD_BYTES = 65536


class LiveFrames:
    """Coalesce native paint callbacks; never request or poll screenshots.

    The encoder sleeps until a native update or an explicit resync. Each queue
    is bounded; an overrun discards patches and schedules a complete reset.
    """
    def __init__(self, capacity=4):
        self.events = queue.Queue(maxsize=capacity)
        self.condition = threading.Condition()
        self.image, self.dirty = None, None
        self.active, self.closed, self.reset = False, False, False
        self.generation, self.sequence, self.updated_at = 0, 0, 0
        self.epoch = 0
        self.thread = threading.Thread(target=self._encode, name='desktop-frame-encoder', daemon=True)
        self.thread.start()

    def _clear(self):
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                return

    def update(self, image, rectangles=None):
        if (not 0 < image.width <= 8192 or not 0 < image.height <= 8192
                or image.width * image.height > 32 * 1024 ** 2):
            raise ValueError('desktop display exceeds limit')
        with self.condition:
            if self.closed:
                return
            resized = self.image is None or self.image.size != image.size
            if resized:
                self.image = image.copy()
                self.generation += 1
                self.reset = True
                box = (0, 0, image.width, image.height)
            else:
                boxes = [(max(0, x), max(0, y), min(image.width, x + w), min(image.height, y + h))
                    for x, y, w, h in (rectangles or [(0, 0, image.width, image.height)])]
                boxes = [box for box in boxes if box[2] > box[0] and box[3] > box[1]]
                if not boxes:
                    return
                for box in boxes:
                    self.image.paste(image.crop(box), box[:2])
                box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes))
            self.updated_at = time.time()
            if self.active:
                if self.dirty:
                    box = (min(box[0], self.dirty[0]), min(box[1], self.dirty[1]),
                        max(box[2], self.dirty[2]), max(box[3], self.dirty[3]))
                self.dirty = box
                self.condition.notify()

    def start(self):
        with self.condition:
            if self.closed:
                raise RuntimeError('desktop connection ended')
            self._clear()
            self.active = True
            self.epoch += 1
            self._resync()
            # Encoded/queued frames from before this start are at or below
            # this watermark. An encoder still running on an older epoch
            # cannot publish after the lock is released.
            return self.sequence

    def _resync(self):
        self.reset = True
        if self.active and self.image is not None:
            self.dirty = (0, 0, self.image.width, self.image.height)
            self.condition.notify()

    def resync(self):
        with self.condition:
            self._resync()

    def stop(self):
        with self.condition:
            self.active, self.dirty = False, None
            self.epoch += 1
            self._clear()

    def next_event(self, timeout=1):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self, reason='connection closed'):
        with self.condition:
            if self.closed:
                return
            self.closed, self.active = True, False
            self._clear()
            self.events.put_nowait({'type': 'closed', 'reason': reason})
            self.condition.notify_all()

    def _encode(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or (self.active and self.dirty is not None))
                if self.closed:
                    return
                reset = self.reset
                box = (0, 0, self.image.width, self.image.height) if reset else self.dirty
                image = self.image.crop(box)
                width, height = self.image.size
                generation, updated_at = self.generation, self.updated_at
                epoch = self.epoch
                self.dirty, self.reset = None, False
            try:
                output = io.BytesIO()
                image.save(output, format='PNG')
                if len(output.getvalue()) > MAX_FRAME_BYTES:
                    raise ValueError('desktop frame exceeds image limit')
                encoded = base64.b64encode(output.getvalue()).decode('ascii')
                if len(encoded) > MAX_MESSAGE_BYTES - 4096:
                    raise ValueError('desktop frame exceeds transport limit')
                with self.condition:
                    if not self.active or self.closed or epoch != self.epoch:
                        continue
                    self.sequence += 1
                    event = {'type': 'frame', 'sequence': self.sequence, 'generation': generation,
                        'width': width, 'height': height, 'x': box[0], 'y': box[1],
                        'rect_width': image.width, 'rect_height': image.height,
                        'mime': 'image/png', 'png_base64': encoded, 'reset': reset,
                        'source_updated_at': updated_at}
                    try:
                        self.events.put_nowait(event)
                    except queue.Full:
                        self._clear()
                        self._resync()
            except Exception:
                self.close('native desktop frame failed')


def drag_points(request):
    x, y = request['x'], request['y']
    dx, dy = request['end_x'] - x, request['end_y'] - y
    steps = max(1, min(24, (max(abs(dx), abs(dy)) + 7) // 8))
    for i in range(1, steps + 1):
        yield x + round(dx * i / steps), y + round(dy * i / steps)


def validate_live_input(request):
    """Defence in depth at the native-process boundary (no secrets in errors)."""
    kind = request.get('type')
    if kind in ('pointer', 'wheel'):
        if any(type(request.get(key)) is not int or not 0 <= request[key] <= 65535 for key in ('x', 'y')):
            raise ValueError('invalid desktop coordinates')
        if kind == 'pointer' and (type(request.get('buttons')) is not int or not 0 <= request['buttons'] <= 7):
            raise ValueError('invalid desktop buttons')
        if kind == 'wheel' and (type(request.get('steps')) is not int or not 1 <= abs(request['steps']) <= 20):
            raise ValueError('invalid desktop wheel')
    elif kind == 'key':
        if (type(request.get('keysym')) is not int or not 0 < request['keysym'] <= 0xffffffff
                or type(request.get('down')) is not bool or not isinstance(request.get('code', ''), str)
                or len(request.get('code', '')) > 64):
            raise ValueError('invalid desktop key')
    elif kind == 'clipboard':
        if not isinstance(request.get('text'), str) or len(request['text'].encode('utf-8')) > MAX_CLIPBOARD_BYTES:
            raise ValueError('desktop clipboard exceeds limit')
    elif kind == 'text':
        value = request.get('text')
        if not isinstance(value, str) or len(value) > 1024 or '\0' in value:
            raise ValueError('desktop text exceeds 1024 characters or contains NUL')
        try:
            value.encode('utf-8')  # Reject lone surrogates before any native input.
        except UnicodeEncodeError:
            raise ValueError('invalid desktop Unicode text') from None
    else:
        raise ValueError('unsupported desktop input')


class HelperDesktop:
    """Fixed administrator-owned bridge, also used by the existing GNOME prototype."""
    def __init__(self, config):
        self.argv = config['argv']
        self.closed = False
        self.call({'action': 'status'})

    def call(self, request):
        if self.closed:
            raise RuntimeError('desktop closed')
        result = subprocess.run(self.argv, input=json.dumps(request).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=25, check=True)
        if len(result.stdout) > 12 * 1024 ** 2:
            raise ValueError('desktop response too large')
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or data.get('ok') is not True:
            raise ValueError('desktop bridge did not confirm request')
        return data

    def screenshot(self):
        status = self.call({'action': 'status'})
        data = self.call({'action': 'screenshot'})
        profile = status.get('profile', {})
        data['generation'] = status.get('generation') or (profile.get('boot_id', ''),
            profile.get('WAYLAND_DISPLAY', ''), status.get('connector', ''))
        return data

    def input(self, request):
        return self.call(request)

    def capabilities(self):
        return {'live': False, 'pointer': False, 'key': False, 'text': False,
            'clipboard': {'read': False, 'write': False, 'encoding': None}}

    def start_live(self):
        raise ValueError('desktop helper does not support live attachment')

    def close(self):
        # Closing this attachment must not stop the target desktop or its other users.
        self.closed = True


class VNCDesktop:
    def __init__(self, config):
        from urllib.parse import urlsplit, unquote
        from vncdotool import api
        from vncdotool.client import VNCDoToolClient, VNCDoToolFactory
        self.frames = LiveFrames()
        self.live = False
        self.clipboard_text = None
        owner = self
        class UnicodeClient(VNCDoToolClient):
            _deck_live = False
            _deck_disconnected = False

            def _decodeKey(self, key):
                if len(key) == 1 and ord(key) > 255:
                    return [0x01000000 | ord(key)]
                return super()._decodeKey(key)

            def commitUpdate(self, rectangles=None):
                if self.screen is not None and rectangles:
                    owner.frames.update(self.screen, rectangles)
                super().commitUpdate(rectangles)
                if self._deck_live:
                    self.framebufferUpdateRequest(incremental=True)

            def updateRectangle(self, x, y, width, height, data):
                image_width = max(self.screen.width if self.screen is not None else 0, x + width)
                image_height = max(self.screen.height if self.screen is not None else 0, y + height)
                if (not 0 < image_width <= 8192 or not 0 < image_height <= 8192
                        or image_width * image_height > 32 * 1024 ** 2):
                    raise ValueError('desktop display exceeds limit')
                super().updateRectangle(x, y, width, height, data)

            def updateDesktopSize(self, width, height):
                if not 0 < width <= 8192 or not 0 < height <= 8192 or width * height > 32 * 1024 ** 2:
                    raise ValueError('desktop display exceeds limit')
                super().updateDesktopSize(width, height)
                owner.frames.update(self.screen)

            def copy_text(self, text):
                # The upstream default logs clipboard contents. Keep text only
                # in this attachment, never in stderr/history or the browser stream.
                if len(text.encode('utf-8')) <= MAX_CLIPBOARD_BYTES:
                    owner.clipboard_text = text

            def connectionLost(self, reason):
                self._deck_disconnected = True
                owner.frames.close('VNC connection ended')
                super().connectionLost(reason)

            def deck_start_live(self):
                if not self._deck_live:
                    self._deck_live = True
                    self.framebufferUpdateRequest(incremental=True)
                # ThreadedVNCClientProxy chains method results through its
                # factory Deferred. Like upstream input methods, return this
                # protocol so the next command still receives a client.
                return self

            def deck_live_input(self, request):
                kind = request['type']
                if not hasattr(self, '_deck_keys'):
                    self._deck_keys = set()
                if kind == 'pointer':
                    if self.screen is None or request['x'] >= self.screen.width or request['y'] >= self.screen.height:
                        raise ValueError('desktop coordinates outside display')
                    self.x, self.y, self.buttons = request['x'], request['y'], request['buttons']
                    self.pointerEvent(self.x, self.y, self.buttons)
                elif kind == 'wheel':
                    self.x, self.y = request['x'], request['y']
                    wheel = 16 if request['steps'] > 0 else 8
                    for _ in range(abs(request['steps'])):
                        self._deck_wheel = wheel
                        self.pointerEvent(self.x, self.y, self.buttons | wheel)
                        self.pointerEvent(self.x, self.y, self.buttons)
                        self._deck_wheel = 0
                elif kind == 'key':
                    key, down = request['keysym'], request['down']
                    if down:
                        self._deck_keys.add(key)
                        self.keyEvent(key, down=True)
                    elif key in self._deck_keys:
                        self.keyEvent(key, down=False)
                        self._deck_keys.remove(key)
                else:
                    # vncdotool 1.4.2's native ClientCutText is ISO-8859-1.
                    request['text'].encode('latin-1')
                    self.paste(request['text'])
                return self

            def deck_release_all(self):
                if self._deck_disconnected and (getattr(self, '_deck_keys', None)
                        or self.buttons or getattr(self, '_deck_wheel', 0)):
                    raise RuntimeError('native input release outcome unknown')
                failed = False
                for key in list(getattr(self, '_deck_keys', ())):
                    try:
                        self.keyEvent(key, down=False)
                        self._deck_keys.remove(key)
                    except Exception:
                        failed = True
                if self.buttons or getattr(self, '_deck_wheel', 0):
                    try:
                        self.pointerEvent(self.x, self.y, 0)
                        self.buttons, self._deck_wheel = 0, 0
                    except Exception:
                        failed = True
                if failed:
                    raise RuntimeError('native input release outcome unknown')
                return self

            def deck_stop_live(self):
                self._deck_live = False
                self.deck_release_all()
                return self
        class Factory(VNCDoToolFactory):
            protocol = UnicodeClient
        if 'usb_vnc_argv' in config:
            from twisted.internet import reactor
            from twisted.internet.endpoints import ProcessEndpoint, StandardErrorBehavior
            argv = config['usb_vnc_argv']

            class USBProxy(api.ThreadedVNCClientProxy):
                def connect(self, host, port, family):
                    def launch():
                        from twisted.internet.defer import maybeDeferred
                        env = os.environ.copy()
                        if getattr(sys, 'frozen', False):
                            original = env.pop('LD_LIBRARY_PATH_ORIG', '')
                            if original:
                                env['LD_LIBRARY_PATH'] = original
                            else:
                                env.pop('LD_LIBRARY_PATH', None)
                        def attach():
                            endpoint = ProcessEndpoint(reactor, argv[0], args=argv, env=env,
                                                       errFlag=StandardErrorBehavior.DROP)
                            return endpoint.connect(self.factory)
                        def failed(reason):
                            if not self.factory.deferred.called:
                                self.factory.deferred.errback(reason)
                        maybeDeferred(attach).addErrback(failed)
                    reactor.callFromThread(launch)

            # vncdotool requires a display name; USBProxy opens only the selected adb process.
            self.client = api.connect('localhost', factory_class=Factory, proxy=USBProxy, timeout=15)
        else:
            url = urlsplit(config['secret_url'])
            if url.scheme != 'vnc+plain' or not url.hostname:
                raise ValueError('unsupported VNC connection')
            self.client = api.connect(url.hostname + '::' + str(url.port or 5900),
                factory_class=Factory, password=unquote(url.password or url.username or ''), timeout=15)
        try:
            self.screenshot()  # Do not report a connected session before authentication/framebuffer setup.
        except Exception:
            self.client.disconnect()
            raise

    def screenshot(self):
        if self.frames.closed:
            raise ConnectionError('VNC connection ended')
        if self.live:
            with self.frames.condition:
                if self.frames.image is None:
                    raise RuntimeError('VNC framebuffer not received')
                image = self.frames.image.copy()
                updated_at = self.frames.updated_at
            output = io.BytesIO()
            image.save(output, format='PNG')
            return {'png_base64': base64.b64encode(output.getvalue()).decode(),
                'source_updated_at': updated_at}
        output = io.BytesIO()
        self.client.captureScreen(output, format='PNG')
        return {'png_base64': base64.b64encode(output.getvalue()).decode()}

    def input(self, request):
        action = request['action']
        if action in ('click', 'scroll', 'drag'):
            self.client.mouseMove(request['x'], request['y'])
            if action == 'drag':
                button = {'left': 1, 'middle': 2, 'right': 3}[request['button']]
                try:
                    self.client.mouseDown(button)
                    for x, y in drag_points(request):
                        self.client.mouseMove(x, y)
                        time.sleep(.016)
                finally:
                    self.client.mouseUp(button)
            elif action == 'click':
                self.client.mousePress({'left': 1, 'middle': 2, 'right': 3}[request['button']])
            else:
                for _ in range(abs(request['steps'])):
                    self.client.mousePress(5 if request['steps'] > 0 else 4)
        elif action == 'key':
            aliases = {'Return': 'enter', 'Escape': 'esc', 'Control_L': 'ctrl', 'Alt_L': 'alt',
                'Shift_L': 'shift', 'Super_L': 'super', 'BackSpace': 'bsp',
                'Left': 'left', 'Right': 'right', 'Up': 'up', 'Down': 'down', 'Tab': 'tab', 'Delete': 'delete'}
            self.client.keyPress('-'.join(aliases.get(k, k.lower()) for k in request['keys']))
        else:
            for char in request['text']:
                self.client.keyPress(char)
        self.client.pause(.15)  # Allow the server display loop to consume input; effect still needs observation.
        return {'ok': True}

    def capabilities(self):
        return {'live': True, 'pointer': True, 'key': True, 'text': True,
            'clipboard': {'read': True, 'write': True, 'encoding': 'latin-1'}}

    def start_live(self):
        sequence_floor = self.frames.start()
        self.client.deck_start_live()
        self.live = True
        return {'ok': True, 'capabilities': self.capabilities(), 'sequence_floor': sequence_floor}

    def next_event(self, timeout=1):
        return self.frames.next_event(timeout)

    def resync_live(self):
        self.frames.resync()
        return {'ok': True}

    def live_input(self, request):
        validate_live_input(request)
        if not self.live or self.frames.closed:
            raise RuntimeError('desktop live attachment closed')
        if request['type'] == 'text':
            self.release_all()
            return self.input({'action': 'text', 'text': request['text']})
        self.client.deck_live_input(request)
        return {'ok': True}

    def clipboard_read(self):
        if self.frames.closed:
            raise ConnectionError('VNC connection ended')
        result = {'ok': True, 'available': self.clipboard_text is not None, 'encoding': 'latin-1'}
        if self.clipboard_text is not None:
            result['text'] = self.clipboard_text
        return result

    def release_all(self):
        self.client.deck_release_all()
        return {'ok': True, 'release_confirmed': True}

    def stop_live(self):
        self.client.deck_stop_live()
        self.live = False
        self.frames.stop()
        return {'ok': True, 'release_confirmed': True}

    def close(self):
        self.frames.close()
        try:
            self.release_all()
        except Exception:
            pass  # Explicit release_all carries the receipt; close must end IO.
        finally:
            self.client.disconnect()


class RDPDesktop:
    """Keep the native library connection on one event loop, with no GUI toolkit."""
    def __init__(self, config):
        self.frames = LiveFrames()
        self.live = False
        self.held_keys, self.buttons, self.pointer = {}, 0, (0, 0)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name='desktop-rdp', daemon=True)
        self.thread.start()
        self.connection, self.pump, self.slow_pump = None, None, None
        self.last_update = 0
        try:
            self.call(self.open(config))
        except Exception:
            self.close()
            raise

    def call(self, coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=25)
        except Exception:
            future.cancel()
            raise

    async def open(self, config):
        from aardwolf.commons.factory import RDPConnectionFactory
        from aardwolf.commons.iosettings import RDPIOSettings
        settings = RDPIOSettings()
        settings.video_width, settings.video_height = config.get('width', 1280), config.get('height', 800)
        settings.channels, settings.vchannels = [], {}
        self.text_input = config.get('text_input', 'unicode')
        self.clipboard_ready, self.clipboard_replies = asyncio.Event(), asyncio.Queue(maxsize=8)
        self.clipboard_remote_available = False
        from aardwolf.extensions.RDPECLIP.channel import RDPECLIPChannel
        from aardwolf.extensions.RDPECLIP.protocol.clipboardcapabilities import CB_GENERAL_FLAGS
        owner = self
        class TextChannel(RDPECLIPChannel):
            def __init__(self, iosettings):
                super().__init__(iosettings)
                self.client_general_caps_flags = CB_GENERAL_FLAGS.USE_LONG_FORMAT_NAMES

            async def _handle_format_list(self, formats):
                # Reuse the native text request/decoder; do not request file
                # descriptors merely because the remote clipboard advertises them.
                formats.templist = [f for f in formats.templist if f.clpfmt == 13]
                # A new format announcement invalidates the previous text,
                # including when the remote user has copied a file/image.
                owner.clipboard_remote_available = False
                return await super()._handle_format_list(formats)
        settings.channels = [TextChannel]
        settings.clipboard_use_pyperclip, settings.clipboard_store_data = False, False
        settings.clipboard.formats.clear()
        settings.clipboard.formats[13] = ''  # Native CF_UNICODETEXT only; no file transfer.
        self.connection = RDPConnectionFactory.from_url(config['secret_url'], settings).get_connection(settings)
        _, error = await self.connection.connect()
        if error:
            if isinstance(error, Exception):
                raise RuntimeError('RDP connection failed: ' + str(error)) from error
            raise RuntimeError('RDP connection failed: ' + str(error))
        from PIL import Image
        self.image = Image.new('RGB', (settings.video_width, settings.video_height))
        self.pump = asyncio.create_task(self.consume())
        # 0.2.17 exposes xrdp's slow-path bitmaps on the original MCS queue,
        # while ext_out_queue contains only fast-path video. Reuse its native
        # parsers and decoder; keep this pinned-library bridge in the adapter.
        queue = self.connection._RDPConnection__joined_channels['MCS'].out_queue
        self.slow_pump = asyncio.create_task(self.consume_slow(queue))
        self.pump.add_done_callback(self._pump_ended)
        self.slow_pump.add_done_callback(self._pump_ended)

    def _pump_ended(self, task):
        if hasattr(self, 'frames'):
            self.frames.close('RDP connection ended')

    def check_connected(self):
        disconnected = getattr(self.connection, 'disconnected_evt', None)
        if ((hasattr(self, 'frames') and self.frames.closed)
                or (disconnected is not None and disconnected.is_set())
                or (self.pump and self.pump.done()) or (self.slow_pump and self.slow_pump.done())):
            raise ConnectionError('RDP connection ended; reconnect and observe')

    def paint(self, data):
        if not (0 <= data.x < self.image.width and 0 <= data.y < self.image.height
                and 0 < data.width <= self.image.width - data.x
                and 0 < data.height <= self.image.height - data.y):
            raise ValueError('RDP display changed; reconnect and observe')
        self.image.paste(data.data, (data.x, data.y))
        self.last_update = time.time()
        if hasattr(self, 'frames'):
            self.frames.update(self.image, [(data.x, data.y, data.width, data.height)])

    async def consume(self):
        while True:
            data = await self.connection.ext_out_queue.get()
            if data is None:
                return
            from aardwolf.commons.queuedata import RDPDATATYPE
            if data.type == RDPDATATYPE.VIDEO:
                self.paint(data)
            elif data.type == RDPDATATYPE.CLIPBOARD_READY:
                self.clipboard_ready.set()
            elif data.type == RDPDATATYPE.CLIPBOARD_FORMAT_LIST_RESPONSE:
                if self.clipboard_replies.full():
                    self.clipboard_replies.get_nowait()
                self.clipboard_replies.put_nowait(data.accepted)
            elif data.type == RDPDATATYPE.CLIPBOARD_DATA_TXT:
                self.clipboard_remote_available = getattr(data, 'datatype', None) == 13

    async def consume_slow(self, queue):
        from aardwolf.protocol.T128.share import TS_SHARECONTROLHEADER, TS_SHAREDATAHEADER, PDUTYPE, PDUTYPE2
        from aardwolf.protocol.fastpath.bitmap import TS_UPDATE_BITMAP_DATA
        from aardwolf.commons.queuedata.video import RDP_VIDEO
        while True:
            data, error = await queue.get()
            if error or data is None:
                raise ConnectionError('RDP connection ended')
            stream = io.BytesIO(data)
            header = TS_SHARECONTROLHEADER.from_buffer(stream)
            if header.pduType != PDUTYPE.DATAPDU:
                raise ConnectionError('RDP mode changed; reconnect and observe')
            stream.seek(0)
            header = TS_SHAREDATAHEADER.from_buffer(stream)
            if header.pduType2 == PDUTYPE2.UPDATE:
                update = TS_UPDATE_BITMAP_DATA.from_buffer(stream)
                if update.updateType != 1:
                    raise ValueError('unsupported RDP drawing mode')
                for bitmap in update.rectangles:
                    if bitmap.width * bitmap.height > 32 * 1024 ** 2:
                        raise ValueError('RDP rectangle exceeds display limit')
                    # xrdp pads encoded scanlines. The native converter takes
                    # its width from destRight; decode the encoded width first,
                    # then clip the padding to the advertised visible rectangle.
                    encoded = copy.copy(bitmap)
                    encoded.destRight = encoded.destLeft + bitmap.width - 1
                    encoded.destBottom = encoded.destTop + bitmap.height - 1
                    rectangle, image = RDP_VIDEO.from_bitmapdata(encoded)
                    rectangle.width = bitmap.destRight - bitmap.destLeft + 1
                    rectangle.height = bitmap.destBottom - bitmap.destTop + 1
                    rectangle.data = image.crop((0, 0, rectangle.width, rectangle.height))
                    self.paint(rectangle)

    async def capture(self):
        for _ in range(150):
            self.check_connected()
            if self.last_update:
                image = self.image.copy()
                if hasattr(image, 'save'):
                    output = io.BytesIO()
                    image.save(output, format='PNG')
                    return {'png_base64': base64.b64encode(output.getvalue()).decode(),
                            'source_updated_at': self.last_update}
            await asyncio.sleep(.1)
        raise TimeoutError('RDP framebuffer not received')

    def screenshot(self):
        return self.call(self.capture())

    async def send(self, request):
        from aardwolf.commons.queuedata.constants import MOUSEBUTTON
        async def checked(coroutine):
            _, error = await coroutine
            if error:
                raise RuntimeError('RDP input failed')
        action = request['action']
        if action in ('click', 'scroll', 'drag'):
            x, y = request['x'], request['y']
            if action == 'drag':
                button = getattr(MOUSEBUTTON, 'MOUSEBUTTON_' + request['button'].upper())
                try:
                    await checked(self.connection.send_mouse(button, x, y, True))
                    for x, y in drag_points(request):
                        await checked(self.connection.send_mouse(MOUSEBUTTON.MOUSEBUTTON_HOVER, x, y, False))
                        await asyncio.sleep(.016)
                finally:
                    await checked(self.connection.send_mouse(button, x, y, False))
            elif action == 'click':
                button = getattr(MOUSEBUTTON, 'MOUSEBUTTON_' + request['button'].upper())
                for pressed in (True, False):
                    await checked(self.connection.send_mouse(button, x, y, pressed))
            else:
                # Native DOWN omits WHEEL in 0.2.17. Its UP path correctly
                # encodes signed rotation; wheels have no button-down event.
                rotation = -120 if request['steps'] > 0 else 120
                for _ in range(abs(request['steps'])):
                    await checked(self.connection.send_mouse(MOUSEBUTTON.MOUSEBUTTON_WHEEL_UP,
                        x, y, False, steps=rotation))
        elif action == 'text':
            if getattr(self, 'text_input', 'unicode') == 'clipboard':
                await asyncio.wait_for(self.clipboard_ready.wait(), 8)
                while not self.clipboard_replies.empty():
                    self.clipboard_replies.get_nowait()
                await self.connection.set_current_clipboard_text(request['text'])
                if not await asyncio.wait_for(self.clipboard_replies.get(), 8):
                    raise RuntimeError('RDP server rejected clipboard text')
                await self.send({'action': 'key', 'keys': ['Control_L', 'v']})
            elif any(ord(char) > 0xffff for char in request['text']):
                raise ValueError('native RDP client supports BMP text only')
            else:
                for char in request['text']:
                    for pressed in (True, False):
                        await checked(self.connection.send_key_char(char, pressed))
        else:
            from aardwolf.keyboard import KeyboardLayout
            from aardwolf.keyboard.layouts.layout_KBDUS import layoutdata
            layout = KeyboardLayout.from_layoutdata(layoutdata)
            pressed_keys = []
            try:
                for key in request['keys']:
                    name = {'Return': 'VK_RETURN', 'Escape': 'VK_ESCAPE', 'Tab': 'VK_TAB',
                        'Control_L': 'VK_LCONTROL', 'Alt_L': 'VK_LMENU', 'Shift_L': 'VK_LSHIFT',
                        'Super_L': 'VK_LWIN', 'BackSpace': 'VK_BACK', 'Delete': 'VK_DELETE',
                        'Left': 'VK_LEFT', 'Right': 'VK_RIGHT', 'Up': 'VK_UP', 'Down': 'VK_DOWN'}.get(key, 'VK_' + key.upper())
                    code = layout.vk_to_scancode(name)
                    # Navigation keys have keypad aliases in the native reverse
                    # map. Prefer E0 there; preserve ordinary Enter/Tab defaults.
                    if name in {'VK_LEFT', 'VK_RIGHT', 'VK_UP', 'VK_DOWN', 'VK_HOME', 'VK_END',
                                'VK_INSERT', 'VK_DELETE', 'VK_PRIOR', 'VK_NEXT'}:
                        code = max(sc for sc, vk in layout.sc_to_vk.items() if vk == name)
                    await checked(self.connection.send_key_scancode(code, True, code > 255))
                    pressed_keys.append(code)
            finally:
                for code in reversed(pressed_keys):
                    await checked(self.connection.send_key_scancode(code, False, code > 255))
        await asyncio.sleep(.1)
        return {'ok': True}

    def input(self, request):
        return self.call(self.send(request))

    def capabilities(self):
        return {'live': True, 'pointer': True, 'key': True, 'text': True,
            'clipboard': {'read': True, 'write': True, 'encoding': 'utf-8'}}

    def start_live(self):
        sequence_floor = self.call(self._start_live())
        return {'ok': True, 'capabilities': self.capabilities(), 'sequence_floor': sequence_floor}

    async def _start_live(self):
        self.check_connected()
        sequence_floor = self.frames.start()
        self.live = True
        return sequence_floor

    def next_event(self, timeout=1):
        return self.frames.next_event(timeout)

    def resync_live(self):
        self.frames.resync()
        return {'ok': True}

    async def _checked(self, coroutine):
        _, error = await coroutine
        if error:
            raise RuntimeError('RDP input failed')

    @staticmethod
    def _live_key(request):
        """Map browser physical codes through the library's native US layout.

        Printable characters without a physical code use the native Unicode
        input path. No RDP packet or keyboard layout is implemented here.
        """
        from aardwolf.keyboard import KeyboardLayout
        from aardwolf.keyboard.layouts.layout_KBDUS import layoutdata
        code, keysym = request.get('code', ''), request['keysym']
        names = {'Enter': 'VK_RETURN', 'NumpadEnter': 'VK_RETURN', 'Escape': 'VK_ESCAPE',
            'Tab': 'VK_TAB', 'ControlLeft': 'VK_LCONTROL', 'ControlRight': 'VK_RCONTROL',
            'AltLeft': 'VK_LMENU', 'AltRight': 'VK_RMENU', 'ShiftLeft': 'VK_LSHIFT',
            'ShiftRight': 'VK_RSHIFT', 'MetaLeft': 'VK_LWIN', 'MetaRight': 'VK_RWIN',
            'Backspace': 'VK_BACK', 'Delete': 'VK_DELETE', 'Insert': 'VK_INSERT',
            'ArrowLeft': 'VK_LEFT', 'ArrowRight': 'VK_RIGHT', 'ArrowUp': 'VK_UP', 'ArrowDown': 'VK_DOWN',
            'Home': 'VK_HOME', 'End': 'VK_END', 'PageUp': 'VK_PRIOR', 'PageDown': 'VK_NEXT',
            'Space': 'VK_SPACE', 'CapsLock': 'VK_CAPITAL', 'NumLock': 'VK_NUMLOCK',
            'ScrollLock': 'VK_SCROLL', 'ContextMenu': 'VK_APPS', 'Backquote': 'VK_OEM_3',
            'Minus': 'VK_OEM_MINUS', 'Equal': 'VK_OEM_PLUS', 'BracketLeft': 'VK_OEM_4',
            'BracketRight': 'VK_OEM_6', 'Backslash': 'VK_OEM_5', 'Semicolon': 'VK_OEM_1',
            'Quote': 'VK_OEM_7', 'Comma': 'VK_OEM_COMMA', 'Period': 'VK_OEM_PERIOD',
            'Slash': 'VK_OEM_2', 'NumpadAdd': 'VK_ADD', 'NumpadSubtract': 'VK_SUBTRACT',
            'NumpadMultiply': 'VK_MULTIPLY', 'NumpadDivide': 'VK_DIVIDE', 'NumpadDecimal': 'VK_DELETE'}
        name = names.get(code)
        if len(code) == 4 and code.startswith('Key') and 'A' <= code[3] <= 'Z':
            name = 'VK_' + code[3]
        elif len(code) == 6 and code.startswith('Digit') and code[5].isdigit():
            name = 'VK_' + code[5]
        elif code.startswith('Numpad') and len(code) == 7 and code[6].isdigit():
            # The native US layout represents keypad digits by their unlocked
            # navigation aliases; use that map's non-E0 entry, not a new table
            # of protocol scancodes. NumLock remains the remote keyboard state.
            name = ['VK_INSERT', 'VK_END', 'VK_DOWN', 'VK_NEXT', 'VK_LEFT',
                'VK_CLEAR', 'VK_RIGHT', 'VK_HOME', 'VK_UP', 'VK_PRIOR'][int(code[6])]
        elif code.startswith('F') and code[1:].isdigit() and 1 <= int(code[1:]) <= 24:
            name = 'VK_' + code
        if name:
            layout = KeyboardLayout.from_layoutdata(layoutdata)
            scancode = layout.vk_to_scancode(name)
            if code.startswith('Numpad') and code != 'NumpadDivide':
                scancode = min(sc for sc, vk in layout.sc_to_vk.items() if vk == name)
            if code in {'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End',
                    'Insert', 'Delete', 'PageUp', 'PageDown', 'ControlRight', 'AltRight', 'NumpadDivide'}:
                scancode = max(sc for sc, vk in layout.sc_to_vk.items() if vk == name)
            if code == 'NumpadEnter':
                scancode |= 0xe000
            return 'scancode', scancode
        charcode = keysym & 0xffffff if keysym & 0xff000000 == 0x01000000 else keysym
        if 0 < charcode <= 0xffff and not 0xff00 <= charcode <= 0xffff:
            return 'char', chr(charcode)
        raise ValueError('unsupported native RDP key')

    async def _send_live(self, request):
        self.check_connected()
        if not self.live:
            raise RuntimeError('desktop live attachment closed')
        from aardwolf.commons.queuedata.constants import MOUSEBUTTON
        kind = request['type']
        if kind in ('pointer', 'wheel'):
            x, y = request['x'], request['y']
            if x >= self.image.width or y >= self.image.height:
                raise ValueError('desktop coordinates outside display')
            self.pointer = (x, y)
            if kind == 'pointer':
                buttons = request['buttons']
                for bit, button in ((1, MOUSEBUTTON.MOUSEBUTTON_LEFT),
                        (2, MOUSEBUTTON.MOUSEBUTTON_MIDDLE), (4, MOUSEBUTTON.MOUSEBUTTON_RIGHT)):
                    if (buttons ^ self.buttons) & bit:
                        # Record before sending; a failed write still needs an UP
                        # attempt during release/close, never a replayed DOWN.
                        if buttons & bit:
                            self.buttons |= bit
                        await self._checked(self.connection.send_mouse(button, x, y, bool(buttons & bit)))
                        if not buttons & bit:
                            self.buttons &= ~bit
                await self._checked(self.connection.send_mouse(MOUSEBUTTON.MOUSEBUTTON_HOVER, x, y, False))
            else:
                for _ in range(abs(request['steps'])):
                    await self._checked(self.connection.send_mouse(MOUSEBUTTON.MOUSEBUTTON_WHEEL_UP,
                        x, y, False, steps=-120 if request['steps'] > 0 else 120))
        elif kind == 'key':
            identity = request.get('code') or str(request['keysym'])
            if request['down']:
                key = self.held_keys.get(identity) or self._live_key(request)
                self.held_keys[identity] = key
            else:
                key = self.held_keys.get(identity)
                if key is None:
                    return {'ok': True}
            method, value = key
            if method == 'scancode':
                await self._checked(self.connection.send_key_scancode(value, request['down'], value > 255))
            else:
                await self._checked(self.connection.send_key_char(value, request['down']))
            if not request['down']:
                self.held_keys.pop(identity, None)
        elif kind == 'text':
            # Preserve the existing explicit text action, including the
            # administrator's unicode/clipboard RDP input mode.
            await self._release_all()
            return await self.send({'action': 'text', 'text': request['text']})
        else:
            await asyncio.wait_for(self.clipboard_ready.wait(), 2)
            while not self.clipboard_replies.empty():
                self.clipboard_replies.get_nowait()
            await self.connection.set_current_clipboard_text(request['text'])
            if not await asyncio.wait_for(self.clipboard_replies.get(), 2):
                raise RuntimeError('RDP server rejected clipboard text')
            self.clipboard_remote_available = True
        return {'ok': True}

    def live_input(self, request):
        validate_live_input(request)
        return self.call(self._send_live(request))

    async def _clipboard_read(self):
        self.check_connected()
        await asyncio.wait_for(self.clipboard_ready.wait(), 2)
        text = (await self.connection.get_current_clipboard_text()
            if getattr(self, 'clipboard_remote_available', True) else None)
        result = {'ok': True, 'available': isinstance(text, str), 'encoding': 'utf-8'}
        if isinstance(text, str):
            if len(text.encode('utf-8')) > MAX_CLIPBOARD_BYTES:
                raise ValueError('desktop clipboard exceeds limit')
            result['text'] = text
        return result

    def clipboard_read(self):
        return self.call(self._clipboard_read())

    async def _release_all(self):
        from aardwolf.commons.queuedata.constants import MOUSEBUTTON
        disconnected = getattr(self.connection, 'disconnected_evt', None)
        if (self.held_keys or self.buttons) and ((disconnected is not None and disconnected.is_set())
                or (self.pump and self.pump.done()) or (self.slow_pump and self.slow_pump.done())):
            raise RuntimeError('native input release outcome unknown')
        errors = []
        for identity, (method, value) in list(self.held_keys.items())[::-1]:
            try:
                coroutine = (self.connection.send_key_scancode(value, False, value > 255)
                    if method == 'scancode' else self.connection.send_key_char(value, False))
                await self._checked(coroutine)
                self.held_keys.pop(identity, None)
            except Exception as error:
                errors.append(error)
        for bit, button in ((1, MOUSEBUTTON.MOUSEBUTTON_LEFT),
                (2, MOUSEBUTTON.MOUSEBUTTON_MIDDLE), (4, MOUSEBUTTON.MOUSEBUTTON_RIGHT)):
            if self.buttons & bit:
                try:
                    await self._checked(self.connection.send_mouse(button, *self.pointer, False))
                    self.buttons &= ~bit
                except Exception as error:
                    errors.append(error)
        if errors:
            raise RuntimeError('native input release outcome unknown')
        return {'ok': True, 'release_confirmed': True}

    def release_all(self):
        return self.call(self._release_all())

    async def _stop_live(self):
        self.live = False
        self.frames.stop()
        return await self._release_all()

    def stop_live(self):
        return self.call(self._stop_live())

    async def stop(self):
        if self.connection:
            if getattr(self, 'held_keys', None) or getattr(self, 'buttons', 0):
                try:
                    await asyncio.wait_for(self._release_all(), 1)
                except Exception:
                    pass  # Closing transport is required even after an unknown UP.
            # terminate() sends a server shutdown request and can wait for its
            # reply. This attachment owns only its transport, never the desktop.
            # Its reader's finally block must not start that graceful shutdown
            # after we close the transport below (pinned 0.2.17 lifecycle).
            self.connection._RDPConnection__terminate_called = True
            transport = self.connection._RDPConnection__connection
            if transport is not None:
                await transport.close()
        pumps = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for pump in pumps:
            pump.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)

    def close(self):
        if hasattr(self, 'frames'):
            self.frames.close()
        if self.loop.is_closed():
            return
        try:
            self.call(self.stop())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=3)
            if not self.thread.is_alive():
                self.loop.close()


class DesktopWorker:
    """Use the bundled private runtime, keeping optional native libraries out of the collector."""
    def __init__(self, config):
        root = Path(__file__).resolve().parents[1]
        bundled = root / 'mcp-runtime' / 'seriald-mcp-worker'
        if getattr(sys, 'frozen', False):
            command = [sys.executable, 'mcp']
        elif not os.environ.get('SERIALD_MCP_PYTHON') and bundled.is_file():
            command = [str(bundled)]
        else:
            command = [os.environ.get('SERIALD_MCP_PYTHON') or sys.executable, str(root / 'seriald_mcp.py')]
        self.process = subprocess.Popen(command + ['--desktop-worker'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.diagnostic_lock, self.connecting, self.connection_stderr = threading.Lock(), True, b''
        self.events, self.outgoing = queue.Queue(maxsize=4), queue.Queue(maxsize=32)
        self.pending, self.command_id = {}, 0
        self.lock, self.close_lock = threading.Lock(), threading.Lock()
        self.closed = threading.Event()
        self.needs_reset = False
        self.stderr_reader = threading.Thread(target=self._read_stderr, name='desktop-worker-diagnostics', daemon=True)
        self.stderr_reader.start()
        self.reader = threading.Thread(target=self._read, name='desktop-worker-output', daemon=True)
        self.writer = threading.Thread(target=self._write, name='desktop-worker-input', daemon=True)
        self.reader.start()
        self.writer.start()
        try:
            result = self.request({'config': config})
            self._capabilities = result['capabilities']
            with self.diagnostic_lock:
                self.connecting, self.connection_stderr = False, b''
        except Exception:
            self.close()
            raise

    def _read_stderr(self):
        # Drain native output so it cannot stall IO. Retain only bounded startup
        # output, never diagnostics from later keyboard/clipboard operations.
        try:
            while True:
                data = self.process.stderr.read1(2048)
                if not data:
                    break
                with self.diagnostic_lock:
                    if self.connecting:
                        self.connection_stderr = (self.connection_stderr + data)[-16384:]
        except (OSError, ValueError):
            pass

    def _connection_failure(self, config, error):
        failure = connection_error(config, 'worker', error)
        with self.diagnostic_lock:
            stderr = self.connection_stderr.decode('utf-8', 'replace')
            self.connecting, self.connection_stderr = False, b''
        failure.details['worker_exit_code_after_cleanup'] = self.process.poll()
        if stderr:
            safe_stderr = redact(stderr, config)
            failure.details['worker_stderr'] = safe_stderr[:4000]
            failure.details['worker_stderr_truncated'] = len(safe_stderr) > 4000
        return failure

    def _clear_events(self):
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                return

    def _read(self):
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(MAX_MESSAGE_BYTES + 1)
                if not line or len(line) > MAX_MESSAGE_BYTES or not line.endswith(b'\n'):
                    break
                result = json.loads(line)
                if not isinstance(result, dict):
                    break
                if result.get('type') == 'reply':
                    with self.lock:
                        response = self.pending.get(result.get('id'))
                    if response is not None:
                        response.put_nowait(result)
                elif result.get('type') == 'event' and isinstance(result.get('event'), dict):
                    event = result['event']
                    with self.lock:
                        if event.get('type') == 'closed':
                            self._clear_events()
                            self.needs_reset = False
                        elif self.needs_reset:
                            if event.get('type') != 'frame' or not event.get('reset'):
                                continue
                            self._clear_events()
                            self.needs_reset = False
                        try:
                            self.events.put_nowait(event)
                        except queue.Full:
                            self._clear_events()
                            self.needs_reset = True
                else:
                    break
        except (OSError, ValueError, TypeError, queue.Full):
            pass
        finally:
            self.close()

    def _write(self):
        try:
            while not self.closed.is_set():
                try:
                    line = self.outgoing.get(timeout=.2)
                except queue.Empty:
                    continue
                self.process.stdin.write(line)
                self.process.stdin.flush()
        except (OSError, ValueError):
            self.close()

    def request(self, value, timeout=28):
        response = queue.Queue(maxsize=1)
        try:
            with self.lock:
                if self.closed.is_set():
                    raise RuntimeError()
                self.command_id += 1
                command_id = self.command_id
                self.pending[command_id] = response
            line = json.dumps(dict(value, id=command_id), ensure_ascii=False).encode() + b'\n'
            if len(line) > 512 * 1024:
                raise ValueError('desktop command exceeds limit')
            self.outgoing.put_nowait(line)
            result = response.get(timeout=timeout)
            if result.get('ok') is not True:
                if 'config' in value and isinstance(result.get('error_details'), dict):
                    raise DesktopConnectionError(result['error_details'])
                raise RuntimeError('desktop native adapter failed')
            return result
        except DesktopConnectionError as error:
            self.close()
            raise self._connection_failure(value.get('config', {}), error) from None
        except Exception as error:
            self.close()
            if 'config' in value:
                if isinstance(error, queue.Empty):
                    timeout_error = TimeoutError('desktop worker did not return a connection receipt within %ss' % timeout)
                    timeout_error.__cause__ = error
                    error = timeout_error
                raise self._connection_failure(value['config'], error) from None
            raise RuntimeError('desktop native adapter unavailable or outcome unknown') from None
        finally:
            with self.lock:
                self.pending.pop(locals().get('command_id'), None)

    def screenshot(self):
        return self.request({'action': 'screenshot'})

    def input(self, request):
        return self.request(request)

    def capabilities(self):
        return copy.deepcopy(self._capabilities)

    def start_live(self, timeout=4):
        return self.request({'action': 'start_live'}, timeout=min(timeout, 4))

    def next_event(self, timeout=1):
        with self.lock:
            reset = self.needs_reset
        if reset:
            self.request({'action': 'resync_live'}, timeout=4)
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def live_input(self, request, timeout=4):
        validate_live_input(request)
        return self.request({'action': 'live_input', 'input': request}, timeout=min(timeout, 4))

    def clipboard_read(self, timeout=4):
        return self.request({'action': 'clipboard_read'}, timeout=min(timeout, 4))

    def release_all(self, timeout=4):
        return self.request({'action': 'release_all'}, timeout=min(timeout, 4))

    def stop_live(self, timeout=4):
        return self.request({'action': 'stop_live'}, timeout=min(timeout, 4))

    def close(self):
        # A timed-out command is never replayed. Killing the isolated transport
        # prevents any queued input from executing after an unknown result.
        with self.close_lock:
            if self.closed.is_set():
                return
            self.closed.set()
            with self.lock:
                for response in self.pending.values():
                    try:
                        response.put_nowait({'ok': False})
                    except queue.Full:
                        pass
                self._clear_events()
                self.events.put_nowait({'type': 'closed', 'reason': 'native desktop worker ended'})
            if self.process.poll() is None:
                self.process.kill()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            self.stderr_reader.join(timeout=.5)
            # The killed process closes the pipes before close can wait for a
            # buffered reader/writer's lock. No global lock is held during IO.
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass


def worker_main():
    output, provider = sys.stdout, None
    # Optional libraries must never put banners/diagnostics on the IPC stream.
    sys.stdout = sys.stderr
    output_lock, stopping = threading.Lock(), threading.Event()
    def emit(value):
        with output_lock:
            output.write(json.dumps(value) + '\n')
            output.flush()

    def events():
        try:
            while not stopping.is_set():
                event = provider.next_event(timeout=.2)
                if event is not None:
                    emit({'type': 'event', 'event': event})
        except (OSError, ValueError):
            stopping.set()

    command_id, event_thread, config = None, None, {}
    try:
        while True:
            line = sys.stdin.buffer.readline(512 * 1024 + 1)
            if not line:
                break
            if len(line) > 512 * 1024 or not line.endswith(b'\n'):
                raise ValueError('desktop request too large')
            request = json.loads(line)
            command_id = request['id']
            if type(command_id) is not int or command_id < 1:
                raise ValueError('invalid desktop command')
            action = request.get('action')
            if provider is None:
                config = request['config']
                if config['provider'] == 'vnc':
                    provider = VNCDesktop(config)
                elif config.get('engine') == 'freerdp':
                    from .desktop_freerdp import FreeRDPDesktop
                    provider = FreeRDPDesktop(config)
                else:
                    provider = RDPDesktop(config)
                result = {'ok': True, 'capabilities': provider.capabilities()}
                event_thread = threading.Thread(target=events, name='desktop-worker-events', daemon=True)
                event_thread.start()
            elif action == 'screenshot':
                result = dict(provider.screenshot(), ok=True)
            elif action == 'live_input':
                result = provider.live_input(request['input'])
            elif action in ('start_live', 'stop_live', 'release_all', 'resync_live', 'clipboard_read'):
                result = getattr(provider, action)()
            else:
                result = provider.input(request)
            emit(dict(result, type='reply', id=command_id))
    except Exception as error:
        reply = {'type': 'reply', 'id': command_id, 'ok': False, 'error': 'native desktop request failed'}
        if provider is None:
            failure = connection_error(config, 'connect', error)
            reply.update(error=str(failure), error_details=failure.details)
        emit(reply)
    finally:
        stopping.set()
        try:
            if provider is not None:
                provider.close()
            if event_thread is not None:
                event_thread.join(timeout=.5)
        finally:
            sys.stdout = output
    return 0


def connect(config):
    return HelperDesktop(config) if config['provider'] == 'helper' else DesktopWorker(config)
