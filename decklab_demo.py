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
        os.environ['DECKLAB_BASE'] = temporary
        os.environ['DECKLAB_STATIC'] = str(Path(__file__).resolve().parent)
        os.environ['DECKLAB_AUTH_TOKEN'] = ''
        for key in list(os.environ):
            if key.startswith('DECKLAB_') and key not in ('DECKLAB_BASE', 'DECKLAB_STATIC', 'DECKLAB_AUTH_TOKEN',
                                                        'DECKLAB_MCP_ENABLED', 'DECKLAB_MCP_PYTHON'):
                del os.environ[key]
        from decklab_core import web
        from decklab_core.config import STATIC
        from decklab_core.control_server import ControlHandler, RevocationReconciler, collector_connection
        from decklab_core.demo import DemoManager
        from decklab_core.httpd import BoundedThreadingTCPServer
        from decklab_core.storage import Store
        from decklab_core.agent_grants import AgentGrantStore, attach_agent_grants
        from decklab_core.mcp_host import MCPHost

        with BoundedThreadingTCPServer(('127.0.0.1', args.port), ControlHandler) as control, \
                MCPHost('http://127.0.0.1:%d' % control.server_address[1]) as mcp:
            control.mcp_host = mcp
            with BoundedThreadingTCPServer(('127.0.0.1', 0), web.Handler) as collector:
                store = Store(os.path.join(temporary, 'registry.sqlite3'))
                grants = AgentGrantStore(os.path.join(temporary, 'agent-grants.sqlite3'))
                manager = DemoManager(store)
                attach_agent_grants(manager, grants)
                collector.manager = manager
                ControlHandler.collector_socket = collector.server_address
                ControlHandler.collector_link = None
                ControlHandler.static_root = STATIC
                ControlHandler.auth_token = ''
                ControlHandler.grant_store = grants
                worker = threading.Thread(target=collector.serve_forever, daemon=True)
                worker.start()
                reconciler = RevocationReconciler(grants,
                    lambda timeout: collector_connection(collector.server_address, timeout=timeout)).start()
                control.revocation_reconciler = reconciler
                signal.signal(signal.SIGTERM, lambda *_:
                              threading.Thread(target=control.shutdown, daemon=True).start())
                print('DeckLab DEMO: http://127.0.0.1:%d/ (simulated boards; temporary data)' %
                      control.server_address[1], flush=True)
                try:
                    control.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    mcp.close()
                    reconciler.close()
                    manager.close()
                    collector.shutdown()
                    worker.join(timeout=6)
                    store.close()
                    grants.close()


if __name__ == '__main__':
    main()
