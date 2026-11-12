#!/usr/bin/env python3
"""Real separate Web/backend processes and TLS, using explicitly simulated boards.

Runs on Windows/Ubuntu; optional --exe exercises the packaged Windows entrypoint.
No serial discovery, USB opening, reset, SSH or board storage writes occur.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as stream:
        stream.bind(('127.0.0.1', 0))
        return stream.getsockname()[1]


def request(address, route, payload=None):
    raw = json.dumps(payload).encode() if payload is not None else None
    req = Request(address + route, data=raw, headers={'Authorization': 'Bearer split-web-owner',
        'Content-Type': 'application/json'})
    with urlopen(req, timeout=3) as incoming:
        return json.load(incoming)


def ready(address, route, process, log):
    for _ in range(150):
        if process.poll() is not None:
            raise RuntimeError('node exited: ' + log.read_text(errors='replace')[-4096:])
        try:
            return request(address, route)
        except (OSError, ValueError, URLError):
            time.sleep(.1)
    raise RuntimeError('node not ready: ' + log.read_text(errors='replace')[-4096:])


def stop(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path)
    args = parser.parse_args()
    backend = control = None
    with tempfile.TemporaryDirectory(prefix='decklab-split-') as directory:
        base = Path(directory)
        hardware, web = base / 'backend', base / 'web'
        hardware.mkdir()
        web.mkdir()
        backend_port, web_port = port(), port()
        address = 'http://127.0.0.1:%d' % web_port
        common = {key: value for key, value in os.environ.items() if not key.startswith('SERIALD_')}
        common.update(SERIALD_STATIC=str(ROOT), SERIALD_MCP_ENABLED='0', SERIALD_AUTH_TOKEN='split-web-owner',
                      SERIALD_BACKEND_TOKEN='split-backend-' + 'x' * 48)
        cert = ROOT / 'tests/fixtures/backend-tls/certificate.pem'
        key = ROOT / 'tests/fixtures/backend-tls/public-test-key.pem'
        backend_environment = dict(common, SERIALD_BASE=str(hardware), SERIALD_BACKEND_BIND='127.0.0.1',
            SERIALD_BACKEND_PORT=str(backend_port), SERIALD_BACKEND_CERT=str(cert), SERIALD_BACKEND_KEY=str(key))
        web_environment = dict(common, SERIALD_BASE=str(web), SERIALD_BACKEND_URL='https://127.0.0.1:%d' % backend_port,
            SERIALD_BACKEND_CA=str(cert), SERIALD_PORT=str(web_port), SERIALD_BIND='127.0.0.1')
        def command(role):
            if args.exe:
                return [str(args.exe.resolve()), role]
            return [sys.executable, '-m', 'seriald_core.' + ('backend_server' if role == 'backend' else 'control_server')]
        backend_log, web_log = base / 'backend.log', base / 'web.log'
        with backend_log.open('wb') as backend_output, web_log.open('wb') as web_output:
            try:
                backend = subprocess.Popen(command('backend') + ['--demo'], cwd=ROOT, env=backend_environment,
                    stdout=backend_output, stderr=subprocess.STDOUT)
                def start_web():
                    return subprocess.Popen(command('web') + ['--no-browser', '--require-backend'], cwd=ROOT,
                        env=web_environment, stdout=web_output, stderr=subprocess.STDOUT)
                control = start_web()
                devices = ready(address, '/api/devices', control, web_log)['devices']
                if len(devices) != 3:
                    raise AssertionError('demo backend boards unavailable')
                board = next(row for row in devices if row.get('serial') == 'DEMO-001')
                if not (hardware / 'registry.sqlite3').is_file() or (web / 'registry.sqlite3').exists() or (web / '.collector-owner.lock').exists():
                    raise AssertionError('hardware ownership leaked into Web node')
                scope = {'key': board['key'], 'serial': board['serial']}
                request(address, '/api/session-send', dict(scope, sid='serial', data='echo split-node-proof\r'))
                output = request(address, '/api/session?key=' + board['key'] + '&sid=serial&off=0')
                if 'split-node-proof' not in output['data']:
                    raise AssertionError('terminal write did not reach backend')
                snapshot = request(address, '/api/settings?target=backend')
                if snapshot['node_role'] != 'backend':
                    raise AssertionError('wrong settings node')
                web_snapshot = request(address, '/api/settings')
                if web_snapshot['node_role'] != 'web':
                    raise AssertionError('wrong Web settings node')
                before = backend.pid
                stop(control)
                if backend.poll() is not None:
                    raise AssertionError('Web stop terminated hardware backend')
                control = start_web()
                restored = ready(address, '/api/devices', control, web_log)['devices']
                after = next(row for row in restored if row.get('serial') == board['serial'])
                if after['connection_id'] != board['connection_id'] or backend.pid != before:
                    raise AssertionError('Web restart replaced backend connection')
                print('PASS: separate processes, authenticated TLS, backend-only registry, terminal I/O, node settings, Web restart preserves backend connection')
            finally:
                stop(control)
                stop(backend)


if __name__ == '__main__':
    main()
