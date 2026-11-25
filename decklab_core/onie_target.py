"""Read the board serial from a CRC-checked ONIE EEPROM."""
import binascii
from pathlib import Path
import re


def parse_onie_serial(value):
    """Return the ONIE 0x23 serial only after validating framing and CRC."""
    if not value or len(value) > 4096 or not re.fullmatch(r'[0-9a-fA-F]+', value) or len(value) % 2:
        return '', False
    try:
        data = bytes.fromhex(value)
    except ValueError:
        return '', False
    if len(data) < 17 or data[:8] != b'TlvInfo\0' or data[8] != 1:
        return '', False
    end = 11 + int.from_bytes(data[9:11], 'big')
    if end > len(data) or end > 2048 or data[end - 6:end - 4] != b'\xfe\x04':
        return '', False
    if int.from_bytes(data[end - 4:end], 'big') != binascii.crc32(data[:end - 4]):
        return '', False
    serials = []
    offset = 11
    while offset < end:
        if offset + 2 > end:
            return '', False
        code, length = data[offset], data[offset + 1]
        value = data[offset + 2:offset + 2 + length]
        if len(value) != length:
            return '', False
        if code == 0x23:
            try:
                serials.append(value.decode('ascii'))
            except UnicodeDecodeError:
                return '', True
        offset += 2 + length
    if offset != end or len(serials) > 1:
        return '', True
    serial = serials[0].strip() if serials else ''
    if not serial or len(serial.encode()) > 32 or any(ord(char) < 33 or ord(char) > 126 for char in serial):
        return '', True
    return serial, True


def board_eeprom_serial():
    serials = set()
    for dev in Path('/sys/bus/nvmem/devices').glob('*'):
        try:
            if b'24c' not in (dev / 'of_node/compatible').read_bytes():
                continue
            with (dev / 'nvmem').open('rb') as stream:
                serial, valid = parse_onie_serial(stream.read(256).hex())
            if valid and serial:
                serials.add(serial)
        except OSError:
            continue
    return serials.pop() if len(serials) == 1 else ''


if __name__ == '__main__':
    print(board_eeprom_serial())
