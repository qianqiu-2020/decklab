"""Select a board identity from verified probe fields, never from USB topology."""
import re

from .storage import IdentityError, serial_component
from .boot_marker import marker_serial

K1_SOC_IDS = {'M1-8571', 'K1-6370', 'K1-6350', 'K1-6371', 'M103-6370', 'M103-6371'}


def uboot_injected_serial(info):
    """Trust a usable DT serial only when the booted DT records U-Boot provenance."""
    serial = str(info.get('serial') or '').strip()
    return serial if info.get('uboot_version') and usable_serial(serial) else ''

def usable_serial(value):
    if not isinstance(value, str):
        return False
    value = value.strip()
    if value.lower() in ('', 'unknown', 'none', 'null', 'n/a', '0123456789abcdef'):
        return False
    if re.fullmatch(r'0[xX][0fF]+|0+|[fF]+', value):
        return False
    try:
        serial_component(value)
        return True
    except IdentityError:
        return False


def hardware_serial(info):
    """Normalize only formats whose kernel driver contract is known."""
    soc_serial = str(info.get('soc_serial') or '').strip()
    if info.get('soc_family') == 'spacemit socs' and info.get('soc_id') in K1_SOC_IDS:
        # The K1 vendor driver exposes the 55-bit eFuse chip ID as 16 uppercase hex digits.
        if (not re.fullmatch(r'[0-9a-fA-F]{16}', soc_serial)
                or not 0 < int(soc_serial, 16) < (1 << 55) - 1):
            soc_serial = ''
        else:
            soc_serial = soc_serial.upper()
    return soc_serial if usable_serial(soc_serial) else ''


def eeprom_serial(info):
    value = str(info.get('eeprom_serial') or '').strip()
    return value if (usable_serial(value) and len(value.encode()) <= 32
                     and all(33 <= ord(char) <= 126 for char in value)) else ''


def select_identity(info, conflicted=False, known_soc=False, known_marker=False,
                    soc_conflicted=False, eeprom_conflicted=False):
    """Preserve established identities; use an explicit /boot marker as the last resort."""
    reported = str(info.get('serial') or '').strip()
    marker = marker_serial(info)
    provisioned = eeprom_serial(info)
    if provisioned and not eeprom_conflicted:
        return dict(info, serial=provisioned, reported_serial=reported,
                    serial_source='eeprom-onie', identity_status='confirmed')
    uboot_serial = uboot_injected_serial(info)
    if uboot_serial and not conflicted:
        return dict(info, serial=uboot_serial, reported_serial=reported,
                    serial_source='uboot-injected', identity_status='confirmed')
    if marker and known_marker:
        return dict(info, serial=marker, reported_serial=reported, serial_source='boot-marker',
                    identity_status='provisional')
    if usable_serial(reported) and not conflicted and not known_soc:
        return dict(info, serial=reported, reported_serial=reported, serial_source='device-tree',
                    identity_status='provisional')
    soc_serial = hardware_serial(info)
    if known_soc and soc_serial and not conflicted and not soc_conflicted:
        return dict(info, serial=soc_serial, reported_serial=reported, serial_source='soc-sysfs',
                    identity_status='provisional')
    if usable_serial(soc_serial) and not soc_conflicted and soc_serial.lower() != reported.lower():
        return dict(info, serial=soc_serial, reported_serial=reported, serial_source='soc-sysfs',
                    identity_status='provisional')
    if marker:
        return dict(info, serial=marker, reported_serial=reported, serial_source='boot-marker',
                    identity_status='provisional')
    if conflicted and usable_serial(reported):
        raise IdentityError('固件序列号已确认不唯一；请写入 EEPROM ONIE TLV 0x23')
    raise IdentityError('EEPROM 尚未写入唯一序列号；当前仅按 USB 插口临时区分，请写入 ONIE TLV 0x23')
