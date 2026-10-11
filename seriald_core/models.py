"""Board identity and session ownership, independent of a UART connection."""
import threading

from .storage import IdentityError, archive_component


class SessionRegistry:
    """One board's SSH sessions and in-progress creation reservations."""
    def __init__(self):
        self.lock = threading.RLock()
        self.sessions = {}
        self.pending = 0

    def transfer_to(self, other):
        if self is other:
            return
        first, second = sorted((self, other), key=id)
        with first.lock, second.lock:
            if self.pending:
                raise IdentityError('SSH 会话正在建立，稍后再绑定设备')
            if set(self.sessions) & set(other.sessions):
                raise IdentityError('SSH session ID collision; refusing to replace a session')
            other.sessions.update(self.sessions)
            self.sessions.clear()


class Board:
    """A persistent serial/label/identity record; ports never define its identity.

    connection is the current (possibly offline) UART attachment. history is a
    lazily created offline API view when no connection is attached. Sessions
    stay with the board across serial re-enumeration.
    """
    def __init__(self, record):
        self._serial = record['serial']
        self.record = record
        self.sessions = SessionRegistry()
        self.connection = None
        self.history = None

    @property
    def serial(self):
        return self._serial

    @property
    def label(self):
        return self.record['label']

    @property
    def archive_path(self):
        return archive_component(self.serial, self.record['info'].get('serial_source', ''))

    def update(self, record):
        if record['serial'] != self.serial:
            raise IdentityError('Board serial is immutable; migrate identity explicitly')
        self.record = record

    def attach(self, connection):
        current = self.connection
        if current is not None and current is not connection and current.present:
            raise IdentityError('Board already has an online serial connection')
        connection.bind_board(self)
        self.connection = connection

    def detach(self, connection):
        if self.connection is connection:
            self.connection = None
