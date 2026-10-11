"""Long-lived serial/SSH collector exposed only through a local Unix socket."""
import argparse
import os
import signal
import socket
import threading

from .config import BASE, HANDOFF, LOG_ROOT, SOCKET
from .devices import Manager
from .handoff import consume as consume_handoff, write as write_handoff
from .httpd import BoundedThreadingUnixServer
from .storage import Store
from . import web
from .profiles import ProfileBook


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', default=SOCKET)
    args = parser.parse_args()
    profiles = ProfileBook.from_env()
    os.makedirs(BASE, exist_ok=True)
    os.makedirs(LOG_ROOT, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.socket)), exist_ok=True)
    if os.path.lexists(args.socket):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(1)
            probe.connect(args.socket)
        except ConnectionRefusedError:
            os.unlink(args.socket)
        else:
            raise RuntimeError('collector socket already active: ' + args.socket)
        finally:
            probe.close()
    # Never unlink an existing socket here: a second launch must fail before touching tty state.
    with BoundedThreadingUnixServer(args.socket, web.Handler) as httpd:
        store = Store(os.path.join(BASE, 'registry.sqlite3'))
        store.recover_sessions()
        manager = Manager(store, consume_handoff(HANDOFF), profiles=profiles)
        httpd.manager = manager
        scanner = threading.Thread(target=manager.loop, daemon=True)
        scanner.start()
        signal.signal(signal.SIGTERM,
                      lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
        print('seriald collector on unix:%s' % args.socket, flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            connections = manager.close()
            try:
                write_handoff(HANDOFF, connections)
            except OSError as error:
                print('seriald handoff: %s' % error, flush=True)
            scanner.join(timeout=6)
            store.close()
    try:
        os.unlink(args.socket)
    except FileNotFoundError:
        pass


if __name__ == '__main__':
    main()
