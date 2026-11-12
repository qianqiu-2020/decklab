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

# Offline administration must not start HTTP servers or discover COM ports.
if len(sys.argv) > 1 and sys.argv[1] == 'admin':
    from seriald_core.admin import main as admin_main
    sys.argv.pop(1)
    raise SystemExit(admin_main())

# The bundled MCP worker is a separate process and must never discover COM
# ports or create a second collector. The public host proxies /mcp to it.
if len(sys.argv) > 1 and sys.argv[1] == 'mcp':
    import seriald_mcp
    sys.argv.pop(1)
    raise SystemExit(seriald_mcp.main())

import argparse
import threading
import webbrowser

from seriald_core import web
from seriald_core.config import AUTH_TOKEN, BASE, HANDOFF, LOG_ROOT, STATIC, read_ui_language, SETTINGS, LAUNCH_ENVIRONMENT
from seriald_core.settings_restart import configure_collector, preflight_listener, preflight_storage, reexecute
from seriald_core.control_server import ControlHandler, configured_agent_tokens
from seriald_core.agent_grants import AgentGrantStore
from seriald_core.handoff import consume as consume_handoff, write as write_handoff
from seriald_core.httpd import BoundedThreadingTCPServer
from seriald_core.storage import Store
from seriald_core.windows_manager import WindowsManager
from seriald_core.profiles import ProfileBook
from seriald_core.mcp_host import MCPHost, local_control_url


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'ctl':
        import serialctl
        sys.argv.pop(1)
        return serialctl.main()
    if SETTINGS.path.exists():
        SETTINGS.validate(SETTINGS.effective())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=int(os.environ.get('SERIALD_PORT', '8898')))
    parser.add_argument('--bind', default=os.environ.get('SERIALD_BIND', '127.0.0.1'))
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    saved_environment = SETTINGS.read().get('environment', {})
    args.port = int(saved_environment.get('PORT', args.port))
    args.bind = saved_environment.get('BIND', args.bind)
    SETTINGS.environment.update({'SERIALD_PORT': str(args.port), 'SERIALD_BIND': args.bind})
    profiles = ProfileBook.from_env()
    # Bind the public port first, so a second copy never takes ownership of COM ports.
    with BoundedThreadingTCPServer((args.bind, args.port), ControlHandler) as control, \
            MCPHost(local_control_url(control)) as mcp:
        control.mcp_host = mcp
        os.makedirs(LOG_ROOT, exist_ok=True)
        store = Store(os.path.join(BASE, 'registry.sqlite3'))
        grants = AgentGrantStore(os.path.join(BASE, 'agent-grants.sqlite3'))
        store.recover_sessions()
        try:
            with BoundedThreadingTCPServer(('127.0.0.1', 0), web.Handler) as collector:
                manager = WindowsManager(store, consume_handoff(HANDOFF), profiles=profiles)
                collector.manager = manager
                configure_collector(collector, SETTINGS)
                control.settings_revision = collector.settings_revision
                control.settings_activation = {'state': 'idle'}
                def activate_settings(revision):
                    environment = SETTINGS.validate(SETTINGS.effective())['environment']
                    preflight_listener(control, environment)
                    preflight_storage(environment)
                    with collector.configuration_gate:
                        if collector.configuration_posts:
                            from seriald_core.web_settings import SettingsError
                            raise SettingsError('还有 API 操作未完成，请稍后重试', 423)
                        result = collector.activate_settings(revision)
                        collector.activation_reply.set()
                        control.settings_activation = {'state': 'applying', 'revision': revision}
                        control.activation_reply = threading.Event()
                    def shutdown():
                        control.activation_reply.wait(2)
                        control.shutdown()
                    threading.Thread(target=shutdown, daemon=True).start()
                    return result
                control.activate_settings = activate_settings
                ControlHandler.collector_socket = ('127.0.0.1', collector.server_address[1])
                ControlHandler.static_root = STATIC
                ControlHandler.auth_token = AUTH_TOKEN
                ControlHandler.settings = SETTINGS
                ControlHandler.agent_tokens = configured_agent_tokens(
                    os.environ.get('SERIALD_AGENT_TOKENS_FILE', ''), AUTH_TOKEN)
                ControlHandler.default_language = read_ui_language(BASE)
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
                    mcp.close()
                    connections = manager.close()
                    write_handoff(HANDOFF, connections)
                    collector.shutdown()
                    worker.join(timeout=6)
                    scanner.join(timeout=6)
        finally:
            store.close()
            grants.close()
    if collector.restart_requested:
        reexecute(LAUNCH_ENVIRONMENT)


if __name__ == '__main__':
    main()
