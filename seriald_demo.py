#!/usr/bin/env python3
"""Run DeckLab with simulated boards in an isolated temporary directory."""
import argparse
import os
from pathlib import Path
import tempfile
import threading
import signal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8899)
    args = parser.parse_args()
    # Set paths before importing modules whose constants read the environment.
    with tempfile.TemporaryDirectory(prefix='decklab-demo-') as temporary:
        os.environ['SERIALD_BASE'] = temporary
        os.environ['SERIALD_STATIC'] = str(Path(__file__).resolve().parent)
        os.environ['SERIALD_AUTH_TOKEN'] = ''
        for key in list(os.environ):
            if key.startswith('SERIALD_') and key not in ('SERIALD_BASE', 'SERIALD_STATIC', 'SERIALD_AUTH_TOKEN'):
                del os.environ[key]
        from seriald_core import web
        from seriald_core.config import STATIC
        from seriald_core.control_server import ControlHandler
        from seriald_core.demo import DemoManager
        from seriald_core.httpd import BoundedThreadingTCPServer
        from seriald_core.storage import Store

        with BoundedThreadingTCPServer(('127.0.0.1', args.port), ControlHandler) as control:
            with BoundedThreadingTCPServer(('127.0.0.1', 0), web.Handler) as collector:
                store = Store(os.path.join(temporary, 'registry.sqlite3'))
                manager = DemoManager(store)
                collector.manager = manager
                ControlHandler.collector_socket = collector.server_address
                ControlHandler.static_root = STATIC
                ControlHandler.auth_token = ''
                worker = threading.Thread(target=collector.serve_forever, daemon=True)
                worker.start()
                signal.signal(signal.SIGTERM, lambda *_:
                              threading.Thread(target=control.shutdown, daemon=True).start())
                print('DeckLab DEMO: http://127.0.0.1:%d/ (simulated boards; temporary data)' %
                      control.server_address[1], flush=True)
                try:
                    control.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    manager.close()
                    collector.shutdown()
                    worker.join(timeout=6)
                    store.close()


if __name__ == '__main__':
    main()
