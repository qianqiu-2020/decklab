"""Windows COM collector; one pySerial handle owns both input and output."""
import hashlib
import re
import time

import serial
from serial.tools import list_ports

from .devices import Manager


def port_key(port):
    # USB topology survives COM-number changes.  If Windows cannot expose it,
    # the COM name is the only available temporary attachment anchor.
    location = (getattr(port, 'location', None) or '').strip()
    interface = re.sub(r'\s*\(COM\d+\)', '',
                       (getattr(port, 'interface', None) or '').strip(), flags=re.I)
    return 'usb-' + location + ('-' + interface if interface else '') if location else port.device


def port_identity(port):
    interface = re.sub(r'\s*\(COM\d+\)', '',
                       (getattr(port, 'interface', None) or '').strip(), flags=re.I)
    details = '|'.join(str(value or '') for value in (
        port.vid, port.pid, port.serial_number, port.location,
        interface, port.hwid))
    digest = hashlib.sha256(details.encode()).digest()
    return tuple(int.from_bytes(digest[n:n + 4], 'big') for n in (0, 4, 8))


class WindowsManager(Manager):
    @staticmethod
    def _handoff_matches(entry, dev, identity):
        # A stable USB topology + adapter identity survives COM renumbering.
        return tuple(entry['identity']) == identity

    def _discover(self):
        found = {}
        ports = sorted(list_ports.comports(), key=lambda item: item.device)
        keys = [port_key(port) for port in ports]
        for port, base_key in zip(ports, keys):
            # If topology cannot distinguish ports on one adapter, keep *all*
            # of them provisional by COM name instead of arbitrarily pinning one.
            key = port.device if keys.count(base_key) > 1 else base_key
            found[key] = (port.device, port_identity(port))
        return found

    @staticmethod
    def _matches(device):
        ports = list(list_ports.comports())
        keys = [port_key(port) for port in ports]
        return any(port.device == device.dev
                   and device.by_path == (port.device if keys.count(key) > 1 else key)
                   and port_identity(port) == device.identity
                   for port, key in zip(ports, keys))

    @staticmethod
    def _close_reader(device):
        with device.write_lock:
            handle, device.fd = device.fd, None
            if handle is not None:
                try:
                    handle.close()
                except (OSError, serial.SerialException):
                    pass
        with device.cv:
            device.cv.notify_all()

    def _capture(self, device):
        try:
            if device.stop.is_set() or not self._matches(device):
                return
            try:
                with device.write_lock:
                    if device.stop.is_set() or not self._matches(device):
                        return
                    with device.cv:
                        device.console_tail = ''
                        device.newline = None
                    device.fd = serial.Serial(device.dev, baudrate=115200,
                                              bytesize=serial.EIGHTBITS,
                                              parity=serial.PARITY_NONE,
                                              stopbits=serial.STOPBITS_ONE,
                                              timeout=0.25, write_timeout=5,
                                              xonxoff=False, rtscts=False, dsrdtr=False)
                device.fail_count = 0
                device._say('seriald 已接入 %s' % device.dev)
            except (OSError, serial.SerialException) as error:
                device.fail_count += 1
                device._say('打不开 %s: %s' % (device.dev, error))
                return
            while not device.stop.is_set():
                try:
                    data = device.fd.read(min(max(device.fd.in_waiting, 1), 65536))
                    if data:
                        device.append(data)
                        device.last_health_check = time.time()
                    elif time.time() - device.last_health_check >= 30:
                        if not device.fd.is_open or not self._matches(device):
                            device._say('%s 串口已断开，等待重新枚举' % device.dev)
                            break
                        device.last_health_check = time.time()
                except (OSError, serial.SerialException) as error:
                    device._say('%s 读取中断: %s' % (device.dev, error))
                    break
        finally:
            device.stop.set()
            self._close_reader(device)
            device.dead = True
            device.present = False
            device.close_log()
            if self.store and device.serial:
                device.record['last_seen'] = self.store.touch(device.serial)
