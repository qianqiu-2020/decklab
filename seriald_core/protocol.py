"""Shell identification protocol and response parsing."""
import binascii
import re

from .boot_marker import read_command

# 每次命令由 _run_cmd 加独立 nonce 帧;命令回显不含完整 marker,迟到结果不会串单。
SSH_STATE_CMD = (
    "if ! command -v systemctl >/dev/null 2>&1; then echo SSHD=unsupported-systemd; "
    "elif systemctl is-active --quiet ssh; then echo SSHD=active; "
    "elif systemctl is-active --quiet sshd; then echo SSHD=active; "
    "else echo SSHD=inactive; fi"
)
PROBE_CMD = (
    "printf 'HOST=%s\\n' \"$(hostname 2>/dev/null | tr '\\r\\n' '  ')\"; "
    "printf 'SERIAL=%s\\n' \"$(cat /proc/device-tree/serial-number 2>/dev/null | tr -d '\\0' | tr '\\r\\n' '  ')\"; "
    "printf 'SOC_SERIAL=%s\\n' \"$(cat /sys/devices/soc0/serial_number 2>/dev/null | tr '\\r\\n' '  ')\"; "
    "printf 'SOC_FAMILY=%s\\n' \"$(cat /sys/devices/soc0/family 2>/dev/null | tr '\\r\\n' '  ')\"; "
    "printf 'SOC_ID=%s\\n' \"$(cat /sys/devices/soc0/soc_id 2>/dev/null | tr '\\r\\n' '  ')\"; "
    "es=''; for ep in /sys/bus/nvmem/devices/*/cells/serial-number@*; do "
    "[ -r \"$ep\" ] || continue; es=$(head -c 100 \"$ep\" 2>/dev/null | tr '\\r\\n' '  '); "
    "[ -n \"$es\" ] && break; done; printf 'EEPROM_SERIAL=%s\\n' \"$es\"; "
    "eh=''; if [ -z \"$es\" ]; then for ed in /sys/bus/nvmem/devices/*; do "
    "[ -r \"$ed/nvmem\" ] && [ -r \"$ed/of_node/compatible\" ] || continue; "
    "ec=$(tr -d '\\0' < \"$ed/of_node/compatible\" 2>/dev/null); "
    "case \"$ec\" in *24c*) [ \"$(head -c 7 \"$ed/nvmem\" 2>/dev/null)\" = TlvInfo ] || continue; "
    "eh=$(head -c 2048 \"$ed/nvmem\" 2>/dev/null | od -An -v -tx1 | tr -d ' \\n'); break;; esac; "
    "done; fi; printf 'EEPROM_HEX=%s\\n' \"$eh\"; "
    "printf 'BOOT_MARKER=%s\\n' \"$(" + read_command() + ")\"; "
    "printf 'MODEL=%s\\n' \"$(cat /proc/device-tree/model 2>/dev/null | tr -d '\\0' | tr '\\r\\n' '  ')\"; "
    "ip -4 -o addr show 2>/dev/null | awk '{print \"IP=\"$2\" \"$4}'; "
    + SSH_STATE_CMD
)
SSH_CMD = (
    "if [ \"$(id -u)\" != 0 ]; then echo SSHD=requires-root; "
    "elif ! command -v systemctl >/dev/null 2>&1; then echo SSHD=unsupported-systemd; "
    "elif ! command -v sshd >/dev/null 2>&1 && ! command -v apt-get >/dev/null 2>&1; "
    "then echo SSHD=unsupported-package-manager; else "
    "if ! command -v sshd >/dev/null 2>&1; then "
    "apt-get update -qq >/dev/null 2>&1 && DEBIAN_FRONTEND=noninteractive "
    "apt-get install -y -qq openssh-server >/dev/null 2>&1; fi; "
    "if ! command -v sshd >/dev/null 2>&1; then echo SSHD=install-failed; else "
    "systemctl enable --now ssh >/dev/null 2>&1 || systemctl enable --now sshd >/dev/null 2>&1; "
    + SSH_STATE_CMD + "; fi; fi"
)

# 登录提示符 / 密码提示符 —— **必须中英双认**。
# 实测:k1 跑 Bianbu 2.3.5 且是中文 locale,getty 显示的是 `密码：`(全角冒号)、
# `用户名`、`登录错误`。只认 `Password:` 会永远等不到,自动登录静默失败。
RE_LOGIN = r'(?:login|用户名)[:：]?'
RE_PASSWD = r'(?:[Pp]assword|密码)[:：]'
RE_PROMPT = r'[#$]\s*$'


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


def parse_probe(raw):
    """解析已校验的 nonce 帧正文;字段缺失或重复必须报错,不能把残帧当成功。"""
    out = {'hostname': '', 'serial': '', 'model': '', 'ips': [], 'sshd': ''}
    # 调用者已经验证完整 nonce 帧;字段值里的 marker 样文字不能再截断正文。
    body = raw.replace('\r\n', '\n')
    for key in ('HOST', 'SERIAL', 'MODEL', 'SSHD'):
        if len(re.findall(r'^' + key + r'=', body, re.M)) != 1:
            raise ValueError('识别结果缺少或重复字段: ' + key)

    def last(key, pat):
        vals = []
        for m in re.finditer(pat, body, re.M):
            v = m.group(1).strip()
            if v and '$(' not in v and '2>/dev/null' not in v:
                vals.append(v)
        return vals[-1] if vals else ''

    out['hostname'] = last('HOST', r'^HOST=(.*)$')
    out['serial'] = last('SERIAL', r'^SERIAL=(.*)$')
    for field in ('SOC_SERIAL', 'SOC_FAMILY', 'SOC_ID', 'EEPROM_SERIAL', 'EEPROM_HEX', 'BOOT_MARKER'):
        if len(re.findall(r'^' + field + r'=', body, re.M)) > 1:
            raise ValueError('识别结果重复字段: ' + field)
        out[field.lower()] = last(field, r'^' + field + r'=(.*)$')
    raw_serial, eeprom_present = parse_onie_serial(out.pop('eeprom_hex', ''))
    if not out.get('eeprom_serial'):
        out['eeprom_serial'] = raw_serial
    out['eeprom_present'] = eeprom_present or bool(out.get('eeprom_serial'))
    out['model'] = last('MODEL', r'^MODEL=(.*)$')
    out['sshd'] = last('SSHD', r'^SSHD=(.*)$')
    seen = set()
    for m in re.finditer(r'^IP=(\S+)\s+(\d+\.\d+\.\d+\.\d+/\d+)', body, re.M):
        iface, cidr = m.group(1), m.group(2)
        if cidr.startswith('127.') or cidr in seen:
            continue
        seen.add(cidr)
        out['ips'].append('%s %s' % (iface, cidr))
    safe_body = re.sub(r'(?m)^EEPROM_HEX=.*$', 'EEPROM_HEX=<redacted>', body)
    out['raw'] = safe_body[-1200:]
    return out
