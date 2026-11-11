"""Check installed credential persistence with fake SSH and a separate fixture DB.

Run as the service account inside a disposable package-test container or VM.
The installed Store and Device are real; authentication and transports are
strictly mocked. No collector is created and no UART or network is opened.
"""
import argparse
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import threading
import time
from types import SimpleNamespace
from unittest import mock


SERIAL = 'installation-ssh-fixture-one'
OTHER_SERIAL = 'installation-ssh-fixture-two'
USER = 'fixture-user'
PASSWORD = "  synthetic-installation-only:$'password  "
DEFAULT_USER = 'fixture-default'
DEFAULT_PASSWORD = 'synthetic-default-only'


class FakeSession:
    def __init__(self, sid, ip, label, logdir, user=None, password=None):
        self.sid, self.ip = sid, ip
        self.cv = threading.Condition()
        self._stop = threading.Event()
        self.closed = False
        self.fd = None
        self.last_touch = time.monotonic()

    def ensure(self):
        return True

    def is_idle(self, now, ttl):
        return False

    def close(self):
        self.closed = True
        self._stop.set()
        if getattr(self, 'store', None) is not None:
            self.store.close_session(self.sid)


def private_files(database):
    if os.name == 'posix':
        for suffix in ('', '-wal', '-shm', '-journal'):
            path = Path(str(database) + suffix)
            if path.exists():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600, 'credential database file is not private'
        backups = database.parent / 'backups'
        for path in backups.glob(database.name + '.*.sqlite3*'):
            if path.is_file():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600, 'credential snapshot file is not private'


def snapshots(database):
    paths = list((database.parent / 'backups').glob(database.name + '.*.sqlite3'))
    assert len(paths) == 1, 'remembered credential snapshot is missing or ambiguous'
    with closing(sqlite3.connect(paths[0].as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT ssh_user,ssh_password,revision FROM ssh_credentials WHERE serial=?',
                         (SERIAL,)).fetchone()
        assert row == (USER, PASSWORD, 1), 'retained snapshot does not contain the verified fixture credentials'
        private_files(database)


def purged(database):
    """After package removal, inspect retained data using only system SQLite."""
    private_files(database)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT ssh_user,ssh_password,revision FROM ssh_credentials WHERE serial=?',
                         (SERIAL,)).fetchone()
        assert row == (USER, PASSWORD, 1), 'package purge changed the remembered fixture credentials'
    private_files(database)
    snapshots(database)
    print('PASS: package purge retains the private credential fixture', flush=True)


def installed(database, code_root, seed):
    sys.path.insert(0, str(code_root))
    from seriald_core import device as device_module
    from seriald_core.profiles import ProfileBook
    from seriald_core.storage import Store

    assert Path(device_module.__file__).resolve().is_relative_to(code_root), 'wrong code root imported'
    profiles = ProfileBook({'profiles': {'fixture': {
        'ssh_user': DEFAULT_USER, 'ssh_password': DEFAULT_PASSWORD,
        'console_user': 'fixture-console', 'console_password': 'synthetic-console-only',
    }}, 'default': 'fixture'})
    if seed:
        assert not database.exists(), 'refusing to replace an existing credential fixture'
    else:
        assert database.is_file(), 'remembered credential fixture is missing'
        # Check package-owned permission policy before Store can tighten files.
        private_files(database)
        snapshots(database)
    store = Store(database)
    try:
        manager = SimpleNamespace(store=store, profiles=profiles)
        with ExitStack() as patches:
            for method in ('run_probe', '_write', 'configure_root_ssh'):
                patches.enter_context(mock.patch.object(device_module.Device, method,
                    side_effect=AssertionError('serial operations are forbidden in credential acceptance')))
            patches.enter_context(mock.patch.object(device_module.Device, 'preload'))
            patches.enter_context(mock.patch.object(device_module.Device, 'stable_dir', return_value='/unused'))
            patches.enter_context(mock.patch.object(device_module, 'write_serial',
                side_effect=AssertionError('serial writes are forbidden in credential acceptance')))
            patches.enter_context(mock.patch.object(device_module, 'SshSession', FakeSession))
            patches.enter_context(mock.patch('subprocess.Popen',
                side_effect=AssertionError('subprocesses are forbidden in credential acceptance')))
            patches.enter_context(mock.patch('socket.create_connection',
                side_effect=AssertionError('network connections are forbidden in credential acceptance')))
            authentication = patches.enter_context(mock.patch.object(device_module, 'check_password_login',
                                                                     return_value=(True, False, '')))

            def board(serial, port):
                info = {'serial': serial, 'hostname': serial, 'serial_source': 'verified-manual',
                        'identity_status': 'confirmed', 'ips': ['eth0 192.0.2.44/24']}
                record = store.bind(info, port, '/fixture-only-tty')
                device = device_module.Device(port, '/fixture-only-tty', manager=manager, record=record)
                store.bind(info, port, '/fixture-only-tty', connection_id=device.connection_id)
                return device

            # Separate helper processes use different USB locations for the same
            # stable serial. Never address the installed collector's real DB.
            device = board(SERIAL, 'fixture-usb-original' if seed else 'fixture-usb-moved')
            credentials = {'ssh_user': USER, 'ssh_password': PASSWORD} if seed else {}
            session, error = device.ssh_connect(**credentials)
            assert session is not None and not error, 'fixture SSH connection failed'
            assert session.ssh_credentials_saved, 'verified credentials were not remembered'
            assert authentication.call_args_list == [mock.call('192.0.2.44', USER, PASSWORD)], \
                'same-board connection did not use the remembered credentials'
            assert store.get_ssh_credentials(SERIAL) == {
                'ssh_user': USER, 'ssh_password': PASSWORD, 'revision': 1}, \
                'remembered credential contents changed'
            snapshot = device.snapshot()
            assert snapshot['ssh_settings'] == {'saved': True, 'user': USER}
            assert PASSWORD not in json.dumps(snapshot), 'public device data disclosed the password'
            assert PASSWORD not in json.dumps(store.boards()), 'public board data disclosed the password'
            device.close_session(session.sid)
            if seed:
                from seriald_core.schema import _backup
                version = store.db.execute('PRAGMA user_version').fetchone()[0]
                _backup(str(database), version, version + 1)
            private_files(database)
            snapshots(database)

            if not seed:
                # The same address/attachment must not reuse another board's
                # credentials. Agent verification also must not create settings.
                authentication.reset_mock()
                other = board(OTHER_SERIAL, 'fixture-usb-moved')
                other_session, error = other.ssh_connect(actor='installation-check-agent')
                assert other_session is not None and not error, 'second fixture board failed'
                assert authentication.call_args_list == [
                    mock.call('192.0.2.44', DEFAULT_USER, DEFAULT_PASSWORD)], \
                    'a different board reused remembered credentials'
                assert store.get_ssh_credentials(OTHER_SERIAL) is None, 'agent connection saved credentials'
                other.close_session(other_session.sid)
        print('PASS: installed credentials %s with fake SSH only; private data and board isolation verified' % (
            'saved by successful web connection' if seed else 'reused by stable identity after process restart'),
            flush=True)
    finally:
        store.close()
    private_files(database)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--code-root', default='/usr/lib/seriald', type=Path)
    parser.add_argument('--phase', required=True, choices=('seed', 'check', 'purged'))
    args = parser.parse_args()
    database = args.database.resolve()
    if database.name != 'ssh-credentials-fixture.sqlite3':
        parser.error('use a separate ssh-credentials-fixture.sqlite3, never a live collector database')
    if args.phase == 'purged':
        purged(database)
    else:
        installed(database, args.code_root.resolve(), args.phase == 'seed')


if __name__ == '__main__':
    main()
