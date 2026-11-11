"""Exercise the packaged MCP worker against a fake HTTP API, never COM ports.

The official SDK acts only as the test client. The worker process must obtain all
of its SDK modules and native dependencies from the frozen distribution.
"""
import argparse
import asyncio
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'seriald-ag-' + 'packagedFixtureOnly' * 2
KEY = 'packaged-unknown-os'
CONNECTION = 'packaged-fixture-connection'


class FixtureAPI(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if not self.authorized():
            return
        parsed = urlsplit(self.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        if parsed.path == '/api/devices':
            return self.reply(200, {'devices': [{'key': KEY, 'label': KEY, 'serial': '',
                'connection_id': CONNECTION, 'alive': True}]})
        if parsed.path == '/api/session' and query.get('key') == KEY:
            data = b'packaged unknown OS> '
            return self.reply(200, {'key': KEY, 'sid': 'serial', 'off': len(data), 'alive': True,
                                   'data_b64': base64.b64encode(data).decode()})
        return self.reply(404, {'error': 'fixture has no board/probe endpoint'})

    def do_POST(self):
        if not self.authorized():
            return
        payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        if (self.path == '/api/session-send' and payload.get('key') == KEY
                and payload.get('connection_id') == CONNECTION and payload.get('sid') == 'serial'):
            self.server.inputs.append(payload['data'])
            return self.reply(200, {'ok': True, 'key': KEY})
        return self.reply(404, {'error': 'fixture has no board/probe endpoint'})

    def authorized(self):
        if self.server.active and self.headers.get('Authorization') == 'Bearer ' + TOKEN:
            return True
        self.reply(401, {'error': 'fixture grant revoked', 'code': 'unauthorized'})
        return False

    def reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def check_tools(endpoint, api):
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    import httpx2

    for mode in ('legacy', 'auto'):
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + TOKEN}) as http:
            async with Client(streamable_http_client(endpoint, http_client=http), mode=mode) as client:
                tools = await client.list_tools()
                assert {'list_devices', 'read_terminal', 'send_input', 'get_task'}.issubset(
                    {tool.name for tool in tools.tools})
                listed = await client.call_tool('list_devices', {})
                assert not listed.is_error, repr(listed)
                assert listed.structured_content['devices'][0]['key'] == KEY
                read = await client.call_tool('read_terminal', {'device': KEY})
                assert not read.is_error, repr(read)
                assert 'packaged unknown OS>' in read.structured_content['data']
                sent = await client.call_tool('send_input', {'device': KEY, 'data': 'help',
                    'enter': True, 'connection_id': CONNECTION})
                assert not sent.is_error, repr(sent)
                assert sent.structured_content['ok']
    assert api.inputs == ['help\r', 'help\r'], api.inputs


def check_public_control(url):
    """Check the packaged host's same-origin route, without selecting a board."""
    from urllib.request import Request
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    import httpx2

    base = url.rstrip('/')
    with urlopen(base + '/api/access', timeout=5) as response:
        access = json.load(response)
    assert access['mcp_enabled'], access.get('mcp_error', 'public MCP disabled')
    assert access['mcp_url'] == '/mcp', access['mcp_url']
    request = Request(base + '/api/agent-share', data=b'{"scope":"all"}',
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=5) as response:
        grant = json.load(response)
    endpoint = base + access['mcp_url']
    try:
        async def check():
            async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + grant['token']}) as http:
                async with Client(streamable_http_client(endpoint, http_client=http), mode='auto') as client:
                    tools = await client.list_tools()
                    assert 'read_terminal' in {tool.name for tool in tools.tools}
                    listed = await client.call_tool('list_devices', {})
                    assert not listed.is_error, repr(listed)
                    assert isinstance(listed.structured_content['devices'], list)
        asyncio.run(check())
    finally:
        request = Request(base + '/api/agent-revoke', data=json.dumps({'id': grant['grant']['id']}).encode(),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=5) as response:
            revoked = json.load(response)
        assert revoked['id'] == grant['grant']['id'], revoked
    print('PASS: packaged public /mcp, server-managed SDK worker, MCP tools, revocable grant; no board selected')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--executable', type=Path, help='Frozen seriald.exe built by PyInstaller')
    mode.add_argument('--source', action='store_true', help='Validate the harness against the source worker')
    mode.add_argument('--control-url', help='Already running isolated packaged host with open loopback access')
    args = parser.parse_args(argv)
    if args.control_url:
        return check_public_control(args.control_url)
    command = ([str(args.executable.resolve()), 'mcp'] if args.executable else
               [sys.executable, str(ROOT / 'seriald_mcp.py')])
    with ThreadingHTTPServer(('127.0.0.1', 0), FixtureAPI) as api, tempfile.TemporaryDirectory() as folder:
        api.active, api.inputs = True, []
        worker = threading.Thread(target=api.serve_forever, daemon=True)
        worker.start()
        ready = Path(folder) / 'mcp-ready.json'
        log = Path(folder) / 'mcp-worker.log'
        # An inherited owner token must never give HTTP callers implicit access.
        environment = dict(os.environ, SERIALD_TOKEN='fixture-owner-must-not-be-used')
        with log.open('wb') as output, subprocess.Popen(command + [
                '--transport', 'streamable-http', '--url', 'http://127.0.0.1:%d' % api.server_port,
                '--host', '127.0.0.1', '--port', '0', '--ready-file', str(ready)],
                env=environment, stdout=output, stderr=output) as process:
            try:
                deadline = time.monotonic() + 25
                while not ready.is_file():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        output.flush()
                        raise AssertionError('MCP worker startup failed: ' + log.read_text(errors='replace'))
                    time.sleep(.05)
                selected = json.loads(ready.read_text())
                endpoint = 'http://%s:%d/mcp' % (selected['host'], selected['port'])
                try:
                    urlopen(endpoint, timeout=3).close()
                except HTTPError as error:
                    assert error.code == 401, error.code
                    error.close()
                else:
                    raise AssertionError('packaged HTTP worker accepted a missing grant')
                asyncio.run(check_tools(endpoint, api))
                api.active = False
                from urllib.request import Request
                try:
                    urlopen(Request(endpoint, headers={'Authorization': 'Bearer ' + TOKEN}), timeout=3).close()
                except HTTPError as error:
                    assert error.code == 401, error.code
                    error.close()
                else:
                    raise AssertionError('packaged HTTP worker accepted a revoked grant')
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                api.shutdown()
                worker.join(timeout=3)
    print('PASS: packaged MCP SDK, legacy/current HTTP, unknown-OS terminal, grant enforcement; no COM access')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
