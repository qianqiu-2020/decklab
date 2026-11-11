"""Exercise installed, bundled MCP through its public URL without an SDK client.

Only use with the hardware-free installation fixtures: list_devices must return
an empty list. The target's system Python must not contain the optional SDK.
"""
import argparse
import importlib.util
import json
import os
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


MAX_RESPONSE = 8 * 1024 * 1024
PROTOCOL_VERSION = '2025-06-18'


def check(url='http://127.0.0.1:18898', owner_token=None):
    """Prove bundled SDK availability and public, revocable-grant tool access."""
    if importlib.util.find_spec('mcp') is not None:
        raise AssertionError('installation fixture system Python unexpectedly contains the MCP SDK')
    if os.environ.get('SERIALD_MCP_PYTHON'):
        raise AssertionError('installation fixture must not select an external MCP Python')
    parts = urlsplit(url)
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in ('', '/')):
        raise ValueError('expected the public service base URL without credentials or parameters')
    url = url.rstrip('/')
    # A package check must exercise this listener, independent of proxy settings.
    opener = build_opener(ProxyHandler({}))

    def request(path, body=None, token=None, extra_headers=None):
        headers = {'Accept': 'application/json, text/event-stream'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if extra_headers:
            headers.update(extra_headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        message = Request(url + path, data=data, headers=headers)
        try:
            response = opener.open(message, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            status = response.code
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise AssertionError('installed MCP response exceeded inspection bounds')
            response_headers = response.headers
        if not raw:
            return status, None, response_headers
        if response_headers.get_content_type() == 'text/event-stream':
            # Stateless workers currently return JSON. Retain framed SSE support
            # so this check verifies protocol messages rather than one encoding.
            messages = []
            for event in raw.decode('utf-8').replace('\r\n', '\n').split('\n\n'):
                lines = [line[5:].lstrip() for line in event.splitlines() if line.startswith('data:')]
                if lines:
                    messages.append(json.loads('\n'.join(lines)))
            expected_id = body.get('id') if isinstance(body, dict) else None
            value = next((item for item in messages if item.get('id') == expected_id), None)
            if value is None:
                raise AssertionError('installed MCP response contained no matching protocol message')
        else:
            value = json.loads(raw)
        return status, value, response_headers

    status, access, _ = request('/api/access', token=owner_token)
    if status != 200 or not access.get('mcp_enabled'):
        raise AssertionError('installed bundled MCP is unavailable at the public control URL')
    if access.get('mcp_url') != '/mcp':
        raise AssertionError('installed service did not advertise its same-origin MCP route')

    denied_body = {'jsonrpc': '2.0', 'id': 99, 'method': 'tools/list'}
    for token in (None, 'seriald-ag-invalid-installation-fixture'):
        status, _, _ = request('/mcp', denied_body, token=token)
        if status != 401:
            raise AssertionError('public MCP accepted a missing or invalid agent grant')
    status, issued, _ = request('/api/agent-share', {'scope': 'all'}, token=owner_token)
    if status != 200 or not isinstance(issued.get('token'), str) or not issued.get('grant', {}).get('id'):
        raise AssertionError('installation fixture could not issue a revocable agent grant')
    token, grant_id = issued['token'], issued['grant']['id']
    headers = {}
    revoked = False

    def rpc(identifier, method, params=None):
        body = {'jsonrpc': '2.0', 'id': identifier, 'method': method}
        if params is not None:
            body['params'] = params
        status, value, response_headers = request('/mcp', body, token, headers)
        if status != 200 or not isinstance(value, dict) or value.get('id') != identifier or 'error' in value:
            raise AssertionError('installed MCP protocol request failed: ' + method)
        session = response_headers.get('Mcp-Session-Id')
        if session:
            headers['Mcp-Session-Id'] = session
        return value['result']

    try:
        initialized = rpc(1, 'initialize', {
            'protocolVersion': PROTOCOL_VERSION,
            'capabilities': {},
            'clientInfo': {'name': 'decklab-installed-package-check', 'version': '1'},
        })
        negotiated = initialized.get('protocolVersion')
        if not isinstance(negotiated, str) or not negotiated:
            raise AssertionError('installed MCP did not negotiate a protocol version')
        headers['MCP-Protocol-Version'] = negotiated
        status, _, _ = request('/mcp', {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, token, headers)
        if status not in (200, 202, 204):
            raise AssertionError('installed MCP rejected initialization notification')
        tools = rpc(2, 'tools/list')
        names = {row['name'] for row in tools.get('tools', [])}
        if not {'list_devices', 'read_terminal'}.issubset(names):
            raise AssertionError('installed MCP did not expose the DeckLab terminal tools')
        listed = rpc(3, 'tools/call', {'name': 'list_devices', 'arguments': {}})
        if listed.get('isError'):
            raise AssertionError('installed MCP read-only tool call failed')
        if listed.get('structuredContent', {}).get('devices') != []:
            raise AssertionError('installation fixture must return an empty hardware-free device list')
        status, result, _ = request('/api/agent-revoke', {'id': grant_id}, token=owner_token)
        if status != 200 or result.get('ok') is not True:
            raise AssertionError('installation fixture could not revoke its agent grant')
        revoked = True
        status, _, _ = request('/mcp', denied_body, token, headers)
        if status != 401:
            raise AssertionError('public MCP accepted a revoked agent grant')
    finally:
        if not revoked:
            request('/api/agent-revoke', {'id': grant_id}, token=owner_token)
    print('PASS: bundled MCP, public initialization/tools, read-only call and grant revocation without a system SDK', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:18898')
    parser.add_argument('--owner-token-env', help='environment variable containing the owner credential')
    args = parser.parse_args()
    token = os.environ.get(args.owner_token_env) if args.owner_token_env else None
    if args.owner_token_env and not token:
        parser.error('the selected owner credential environment variable is empty')
    check(args.url, token)


if __name__ == '__main__':
    main()
