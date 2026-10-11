"""Validated host/board configuration, independent of serial and SSH adapters."""
from dataclasses import dataclass, field, replace
import json
import os


@dataclass(frozen=True)
class SerialSettings:
    baud: int = 115200
    data_bits: int = 8
    parity: str = 'N'
    stop_bits: int = 1
    flow_control: str = 'none'

    def __post_init__(self):
        if type(self.baud) is not int or not 50 <= self.baud <= 4000000:
            raise ValueError('serial baud must be an integer between 50 and 4000000')
        if type(self.data_bits) is not int or self.data_bits not in (5, 6, 7, 8):
            raise ValueError('serial data_bits must be 5, 6, 7 or 8')
        if self.parity not in ('N', 'E', 'O'):
            raise ValueError('serial parity must be N, E or O')
        if type(self.stop_bits) is not int or self.stop_bits not in (1, 2):
            raise ValueError('serial stop_bits must be 1 or 2')
        if self.flow_control not in ('none', 'rtscts', 'xonxoff'):
            raise ValueError('serial flow_control must be none, rtscts or xonxoff')

    @classmethod
    def from_env(cls):
        return cls(baud=int(os.environ.get('SERIALD_BAUD', '115200')),
                   data_bits=int(os.environ.get('SERIALD_DATA_BITS', '8')),
                   parity=os.environ.get('SERIALD_PARITY', 'N'),
                   stop_bits=int(os.environ.get('SERIALD_STOP_BITS', '1')),
                   flow_control=os.environ.get('SERIALD_FLOW_CONTROL', 'none'))

    def pyserial_options(self):
        return {'baudrate': self.baud, 'bytesize': self.data_bits,
                'parity': self.parity, 'stopbits': self.stop_bits,
                'xonxoff': self.flow_control == 'xonxoff',
                'rtscts': self.flow_control == 'rtscts', 'dsrdtr': False}


def _boolean(value, name):
    if value not in ('0', '1', 'false', 'true'):
        raise ValueError(name + ' must be 0, 1, false or true')
    return value in ('1', 'true')


@dataclass(frozen=True)
class BoardProfile:
    name: str = 'default'
    serial: SerialSettings = field(default_factory=SerialSettings)
    console_user: str = 'root'
    console_password: str = field(default='bianbu', repr=False)
    ssh_user: str = 'root'
    ssh_password: str = field(default='bianbu', repr=False)
    newline: str = 'auto'
    probe: str = 'linux'
    auto_configure_ssh: bool = True

    def __post_init__(self):
        if self.newline not in ('auto', 'cr', 'lf', 'crlf'):
            raise ValueError('profile newline must be auto, cr, lf or crlf')
        if self.probe not in ('linux', 'manual'):
            raise ValueError('profile probe must be linux or manual')
        if type(self.auto_configure_ssh) is not bool:
            raise ValueError('profile auto_configure_ssh must be boolean')
        for key in ('console_user', 'console_password', 'ssh_user', 'ssh_password'):
            value = getattr(self, key)
            if not isinstance(value, str) or any(char in value for char in '\r\n\x00'):
                raise ValueError('profile ' + key + ' must be a single-line string')
        if not self.console_user or not self.ssh_user:
            raise ValueError('profile login users must not be empty')
        for key in ('console_user', 'ssh_user'):
            value = getattr(self, key)
            if value.startswith('-') or '@' in value or any(char.isspace() for char in value):
                raise ValueError('profile ' + key + ' must be an unambiguous login name')
        if self.probe == 'manual' and self.auto_configure_ssh:
            raise ValueError('manual profile cannot automatically provision SSH')

    @property
    def line_ending(self):
        return {'auto': None, 'cr': '\r', 'lf': '\n', 'crlf': '\r\n'}[self.newline]

    @classmethod
    def from_env(cls):
        user = os.environ.get('SERIALD_SSH_USER', 'root')
        password = os.environ.get('SERIALD_SSH_PASSWORD', 'bianbu')
        probe = os.environ.get('SERIALD_PROBE', 'linux')
        automatic = _boolean(os.environ.get('SERIALD_AUTO_CONFIGURE_SSH',
                                            '0' if probe == 'manual' else '1'),
                             'SERIALD_AUTO_CONFIGURE_SSH')
        return cls(serial=SerialSettings.from_env(), ssh_user=user, ssh_password=password,
                   console_user=os.environ.get('SERIALD_CONSOLE_USER', user),
                   console_password=os.environ.get('SERIALD_CONSOLE_PASSWORD', password),
                   newline=os.environ.get('SERIALD_NEWLINE', 'auto'), probe=probe,
                   auto_configure_ssh=automatic)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate profile configuration field: ' + key)
        result[key] = value
    return result


class ProfileBook:
    """Explicit assignments by stable serial or attachment; no hardware guessing."""
    def __init__(self, document=None):
        document = {} if document is None else document
        if not isinstance(document, dict) or set(document) - {'default', 'profiles', 'boards', 'ports'}:
            raise ValueError('invalid profile configuration structure')
        self.default = document.get('default', 'default')
        self.profiles = document.get('profiles', {})
        self.boards = document.get('boards', {})
        self.ports = document.get('ports', {})
        if not isinstance(self.default, str):
            raise ValueError('default profile must be a name')
        for mapping in (self.profiles, self.boards, self.ports):
            if not isinstance(mapping, dict):
                raise ValueError('profile collections must be objects')
        for name in self.profiles:
            if not isinstance(name, str) or not name or len(name) > 64:
                raise ValueError('invalid profile name')
            self._make(name)
        if self.default != 'default' and self.default not in self.profiles:
            raise ValueError('unknown default profile')
        for mapping in (self.boards, self.ports):
            if any(not isinstance(key, str) or not key or not isinstance(name, str)
                   or (name != 'default' and name not in self.profiles)
                   for key, name in mapping.items()):
                raise ValueError('invalid profile assignment')
        # Validate environment defaults even when no ports exist.
        BoardProfile.from_env()

    @classmethod
    def from_env(cls):
        path = os.environ.get('SERIALD_PROFILES')
        if not path:
            return cls()
        with open(path, 'rb') as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError('profile configuration exceeds 64 KiB')
        return cls(json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_object))

    def _make(self, name):
        values = self.profiles.get(name, {})
        fields = {'serial', 'console_user', 'console_password', 'ssh_user', 'ssh_password',
                  'newline', 'probe', 'auto_configure_ssh'}
        if not isinstance(values, dict) or set(values) - fields:
            raise ValueError('invalid fields for profile ' + name)
        values = dict(values)
        base = BoardProfile.from_env()
        serial = values.pop('serial', {})
        if not isinstance(serial, dict) or set(serial) - set(SerialSettings.__dataclass_fields__):
            raise ValueError('invalid serial fields for profile ' + name)
        if values.get('probe') == 'manual' and 'auto_configure_ssh' not in values:
            values['auto_configure_ssh'] = False
        return replace(base, name=name, serial=replace(base.serial, **serial), **values)

    def resolve(self, serial='', port=''):
        return self._make(self.boards.get(serial, self.ports.get(port, self.default)))
