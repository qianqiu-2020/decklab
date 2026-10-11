#!/usr/bin/env python3
"""Portable Windows COM-port host for the seriald web and agent API."""
import os
import sys

if sys.platform != 'win32':
    raise SystemExit('This launcher runs on Windows only')

from pathlib import Path

ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
os.environ.setdefault('SERIALD_BASE', str(ROOT / 'data'))
os.environ.setdefault('SERIALD_STATIC', str(getattr(sys, '_MEIPASS', ROOT)))

# Demo chooses isolated storage before application configuration is imported.
if len(sys.argv) > 1 and sys.argv[1] == 'demo':
    import seriald_demo
    sys.argv.pop(1)
    raise SystemExit(seriald_demo.main())

import argparse
import threading
import webbrowser

from seriald_core import web
from seriald_core.config import AUTH_TOKEN, BASE, HANDOFF, LOG_ROOT, STATIC
from seriald_core.control_server import ControlHandler
from seriald_core.agent_grants import AgentGrantStore
from seriald_core.handoff import consume as consume_handoff, write as write_handoff
from seriald_core.httpd import BoundedThreadingTCPServer
from seriald_core.storage import Store
from seriald_core.windows_manager import WindowsManager
from seriald_core.profiles import ProfileBook


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'ctl':
        import serialctl
        sys.argv.pop(1)
        return serialctl.main()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=int(os.environ.get('SERIALD_PORT', '8898')))
    parser.add_argument('--bind', default=os.environ.get('SERIALD_BIND', '127.0.0.1'))
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    profiles = ProfileBook.from_env()
    # Bind the public port first, so a second copy never takes ownership of COM ports.
    with BoundedThreadingTCPServer((args.bind, args.port), ControlHandler) as control:
        os.makedirs(LOG_ROOT, exist_ok=True)
        store = Store(os.path.join(BASE, 'registry.sqlite3'))
        grants = AgentGrantStore(os.path.join(BASE, 'agent-grants.sqlite3'))
        store.recover_sessions()
        try:
            with BoundedThreadingTCPServer(('127.0.0.1', 0), web.Handler) as collector:
                manager = WindowsManager(store, consume_handoff(HANDOFF), profiles=profiles)
                collector.manager = manager
                ControlHandler.collector_socket = ('127.0.0.1', collector.server_address[1])
                ControlHandler.static_root = STATIC
                ControlHandler.auth_token = AUTH_TOKEN
                ControlHandler.grant_store = grants
                ControlHandler.trust_loopback = os.environ.get('SERIALD_TRUST_LOOPBACK') == '1'
                scanner = threading.Thread(target=manager.loop, daemon=True)
                scanner.start()
                worker = threading.Thread(target=collector.serve_forever, daemon=True)
                worker.start()
                url = 'http://127.0.0.1:%d/' % args.port
                print('seriald Windows COM host: %s' % url, flush=True)
                if not args.no_browser:
                    webbrowser.open(url)
                try:
                    control.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    connections = manager.close()
                    write_handoff(HANDOFF, connections)
                    collector.shutdown()
                    worker.join(timeout=6)
                    scanner.join(timeout=6)
        finally:
            store.close()
            grants.close()


if __name__ == '__main__':
    main()
