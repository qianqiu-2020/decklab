#!/usr/bin/env python3
"""Agent-facing client for seriald; never opens a serial port locally."""
import argparse
import base64
import binascii
import http.client
import json
import math
import os
import re
import secrets
import shlex
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


DEFAULT_URL = 'http://127.0.0.1:8898'
OUTPUT_LIMIT = 1024 * 1024
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')


class ClientError(Exception):
    pass


class Client:
    def __init__(self, url, timeout=90, token=''):
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.netloc
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ClientError('URL must be an http(s) service URL without credentials/query/fragment')
        self.url = url.rstrip('/')
        self.timeout = timeout
        self.key = None
        self.serial = None
        self.actor = os.environ.get('SERIALD_ACTOR', 'agent')
        self.ssh_user = None
        self.ssh_password = None
        self.token = token

    def request(self, route, payload=None, query=None, timeout=None):
        if payload is not None:
            payload = dict(payload, actor=self.actor)
            if self.serial and payload.get('key'):
                payload['serial'] = self.serial
        if query and self.serial and query.get('key'):
            query = dict(query, serial=self.serial)
        url = self.url + route
        if query:
            url += '?' + urlencode(query)
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        if data is not None and len(data) > 65536:
            raise ClientError('Request exceeds the service limit of 65536 bytes')
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        request = Request(url, data=data, headers=headers)
        try:
            with urlopen(request, timeout=timeout or self.timeout) as response:
                result = json.load(response)
        except HTTPError as error:
            with error:
                detail = error.read(4096).decode('utf-8', 'replace')
            raise ClientError('HTTP %s: %s' % (error.code, detail)) from error
        except (URLError, OSError, ValueError, http.client.HTTPException) as error:
            raise ClientError(str(error)) from error
        if not isinstance(result, dict):
            raise ClientError('Expected a JSON object from seriald')
        if result.get('ok') is False:
            raise ClientError(result.get('error') or 'Service reported failure')
        return result

    def select(self, selector):
        devices = self.request('/api/devices')['devices']
        exact = [d for d in devices if d['key'] == selector]
        matches = exact or [d for d in devices if selector in (d.get('label'), d.get('serial'))]
        if len(matches) != 1:
            raise ClientError('Device selector must match exactly one key/label or serial; '
                              'run devices and use the full key')
        self.key = matches[0]['key']
        self.serial = matches[0].get('serial')
        return matches[0]

    def get(self, route, **query):
        return self.request(route, query=dict(query, key=self.key))

    def post(self, route, **payload):
        return self.request(route, payload=dict(payload, key=self.key))

    def read(self, sid, off, timeout=None):
        result = self.request('/api/session', query={'key': self.key, 'sid': sid, 'off': off},
                              timeout=timeout)
        if result.get('key') != self.key or result.get('sid') != sid:
            raise ClientError('Response belongs to a different device or session')
        try:
            data = base64.b64decode(result['data_b64'], validate=True)
            end = result['off']
            if type(end) is not int or end < len(data) or end < off:
                raise ValueError('invalid/regressed offset; service may have restarted')
        except (KeyError, ValueError, TypeError, binascii.Error) as error:
            raise ClientError('Invalid stream response: %s' % error) from error
        result['gap'] = off > 0 and end - len(data) > off
        return result, data


def frame_command(command, nonce):
    if not command.strip() or len(command.encode('utf-8')) > 2048:
        raise ClientError('Command must contain 1–2048 UTF-8 bytes')
    if any(ord(char) < 32 and char not in '\n\t' or ord(char) == 127 for char in command):
        raise ClientError('Command contains terminal control characters; use send for raw input')
    # A child shell prevents `exit`, `exec`, and `set -e` from eating the end marker.
    # Split markers cannot be mistaken for their echoed command line.
    line = ("printf '\\n%s%s\\n' '__SD_AGENT_B_' %s; sh -c %s </dev/null; "
            "printf '\\n%s%s:%%s\\n' '__SD_AGENT_E_' %s \"$?\"\r")
    line = line % ('%s', '%s', shlex.quote(nonce + '__'), shlex.quote(command),
                   '%s', '%s', shlex.quote(nonce + '__'))
    if max(len(part.encode('utf-8')) for part in line.split('\n')) > 3500:
        raise ClientError('Escaped command is too long for a terminal input line')
    return line


def parse_frame(data, nonce):
    text = ANSI.sub('', data.decode('utf-8', 'replace')).replace('\r\n', '\n')
    start = re.search(r'(?m)^__SD_AGENT_B_' + nonce + r'__\n', text)
    if not start:
        return None, ''
    body = text[start.end():]
    end = re.search(r'\n__SD_AGENT_E_' + nonce + r'__:(\d{1,3})\n', body)
    if not end:
        return None, body
    return int(end.group(1)), body[:end.start()]


def execute(client, command, timeout):
    nonce = secrets.token_hex(12)
    framed = frame_command(command, nonce)
    result = {'ok': False, 'key': client.key, 'completed': False, 'exit_code': None,
              'output': '', 'sent': False, 'session_closed': False}
    sid = None
    captured = bytearray()
    try:
        # Non-web actors reuse one persistent shell.  The service still records
        # this command's result, while the PTY remains available for the next
        # invocation and visible in the human UI until ssh-close.
        credentials = {}
        if isinstance(getattr(client, 'ssh_user', None), str):
            credentials['ssh_user'] = client.ssh_user
        if isinstance(getattr(client, 'ssh_password', None), str):
            credentials['ssh_password'] = client.ssh_password
        opened = client.post('/api/ssh-connect', title=command, **credentials)
        client.key = opened.get('device_key') or client.key
        client.serial = opened.get('serial') or client.serial
        result['key'] = client.key
        sid = opened.get('sid')
        if not isinstance(sid, str) or not re.fullmatch(r'ssh[0-9a-f]+', sid):
            sid = None
            raise ClientError('Service did not return a valid SSH session ID')
        result['sid'] = sid
        # The server advertises the policy explicitly.  Keeping the default
        # false lets this client continue to clean up correctly against an
        # older collector during an upgrade.
        result['session_persistent'] = bool(opened.get('persistent', False))
        deadline = time.monotonic() + timeout
        off = 0
        ready = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClientError('Timed out waiting for SSH login or command completion')
            packet, data = client.read(sid, off, timeout=min(client.timeout, remaining))
            off = packet['off']
            if packet['gap']:
                raise ClientError('Output was lost from the service scrollback buffer')
            captured.extend(data)
            if len(captured) > OUTPUT_LIMIT:
                del captured[:-OUTPUT_LIMIT]
                raise ClientError('Command exceeded the 1 MiB output limit')
            if not ready:
                tail = ANSI.sub('', captured.decode('utf-8', 'replace'))[-512:]
                if packet['alive'] and re.search(r'[#$]\s*$', tail):
                    captured.clear()
                    # Once dispatch starts, a network failure cannot prove non-execution.
                    result['sent'] = True
                    client.request('/api/session-send', payload={
                        'key': client.key, 'sid': sid, 'data': framed},
                        timeout=max(0.01, deadline - time.monotonic()))
                    ready = True
            else:
                code, output = parse_frame(captured, nonce)
                result['output'] = output
                if code is not None:
                    result.update(ok=code == 0, completed=True, exit_code=code)
                    break
            if not packet['alive']:
                raise ClientError('SSH session ended before command completion')
            if not data:
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    except (ClientError, KeyboardInterrupt) as error:
        result['error'] = str(error) or 'Interrupted'
        if result['sent']:
            _, result['output'] = parse_frame(captured, nonce)
        else:
            result['output'] = captured.decode('utf-8', 'replace')
    finally:
        if sid is not None:
            try:
                client.post('/api/session-result', sid=sid, completed=result['completed'],
                            exit_code=result['exit_code'])
            except ClientError as error:
                result['recording_error'] = str(error)
                result['ok'] = False
            if not result.get('session_persistent'):
                try:
                    client.post('/api/session-close', sid=sid)
                    result['session_closed'] = True
                except (ClientError, KeyboardInterrupt) as error:
                    result['cleanup_error'] = str(error) or 'Cleanup interrupted'
                    result['ok'] = False
        result['may_still_be_running'] = result['sent'] and not result['completed']
    return result


def positive_timeout(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0 or value > 3600:
        raise argparse.ArgumentTypeError('timeout must be greater than 0 and at most 3600 seconds')
    return value


def nonnegative(value):
    value = int(value)
    if value < 0:
        raise argparse.ArgumentTypeError('value must be nonnegative')
    return value


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', default=os.environ.get('SERIALD_URL', DEFAULT_URL))
    p.add_argument('--http-timeout', type=positive_timeout, default=90)
    p.add_argument('--token', default=os.environ.get('SERIALD_TOKEN', ''),
                   help='access token; defaults to SERIALD_TOKEN')
    p.add_argument('--device', help='device label (key) or immutable board serial')
    p.add_argument('--actor', default=os.environ.get('SERIALD_ACTOR', 'agent'),
                   help='operator name shown in history, e.g. claude-code/session-name')
    p.add_argument('--ssh-user', default=os.environ.get('SERIALD_SSH_USER_OVERRIDE'),
                   help='SSH login user for this request; otherwise server configuration')
    p.add_argument('--ssh-password', default=os.environ.get('SERIALD_SSH_PASSWORD_OVERRIDE'),
                   help='SSH password for this request; prefer the environment variable')
    commands = p.add_subparsers(dest='action', required=True)
    commands.add_parser('health', help='show collector process health and uptime')
    commands.add_parser('devices', help='list devices without probing them')
    read = commands.add_parser('read', help='read captured output; save off for the next call')
    read.add_argument('--sid', default='serial')
    read.add_argument('--off', type=nonnegative, default=0)
    read.add_argument('--tail', type=nonnegative, default=16384, help='maximum bytes; 0 returns all')
    send = commands.add_parser('send', help='send terminal input; does not wait for a command result')
    send.add_argument('--sid', default='serial')
    source = send.add_mutually_exclusive_group(required=True)
    source.add_argument('--text')
    source.add_argument('--file', help='UTF-8 input file, or - for stdin')
    send.add_argument('--enter', action='store_true', help='append CR (otherwise input is literal)')
    run = commands.add_parser('exec', help='run a command in your persistent SSH session')
    run.add_argument('--command', required=True, help='quote for the LOCAL shell')
    run.add_argument('--timeout', type=positive_timeout, default=60,
                     help='SSH login + command timeout, after session creation')
    identify = commands.add_parser('identify', help='actively identify board; may log in on the serial console')
    identify.add_argument('--init-boot-marker', action='store_true',
                          help='explicitly create /boot/seriald-id once as a software identity fallback')
    commands.add_parser('sessions', help='list serial and SSH sessions')
    commands.add_parser('ssh-open', help='open or reuse your persistent interactive SSH session')
    close = commands.add_parser('ssh-close', help='close only your own SSH session')
    close.add_argument('--sid', required=True)
    commands.add_parser('logs', help='list stored log files')
    commands.add_parser('activity', help='show persisted SSH sessions and agent activity')
    log = commands.add_parser('log', help='read a stored log online')
    log.add_argument('--file', required=True)
    log.add_argument('--off', type=nonnegative)
    rename = commands.add_parser('rename', help='change a known board label without moving its logs')
    rename.add_argument('--label', required=True)
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        client = Client(args.url, args.http_timeout, args.token)
        client.actor = args.actor
        client.ssh_user, client.ssh_password = args.ssh_user, args.ssh_password
        if args.action in ('devices', 'health'):
            result = client.request('/api/' + args.action)
        else:
            if not args.device:
                raise ClientError('--device is required; run devices to find the full key')
            client.select(args.device)
            if args.action == 'read':
                result, data = client.read(args.sid, args.off)
                dropped = max(0, len(data) - args.tail) if args.tail else 0
                data = data[dropped:]
                result.update(data=data.decode('utf-8', 'replace'),
                              data_b64=base64.b64encode(data).decode('ascii'),
                              truncated_bytes=dropped, start_off=result['off'] - len(data))
            elif args.action == 'rename':
                result = client.post('/api/label', label=args.label)
            elif args.action == 'log':
                result = client.get('/api/log', file=args.file, **({} if args.off is None else {'off': args.off}))
            elif args.action == 'send':
                text = args.text
                if args.file == '-':
                    text = sys.stdin.read(65537)
                elif args.file:
                    with open(args.file, encoding='utf-8') as source:
                        text = source.read(65537)
                result = client.post('/api/session-send', sid=args.sid,
                                     data=text + ('\r' if args.enter else ''))
            elif args.action == 'exec':
                result = execute(client, args.command, args.timeout)
            elif args.action == 'ssh-close':
                if not re.fullmatch(r'ssh[0-9a-f]+', args.sid):
                    raise ClientError('--sid must be an SSH session ID returned by ssh-open')
                result = client.post('/api/session-close', sid=args.sid)
            elif args.action in ('identify', 'ssh-open'):
                route = '/api/identify' if args.action == 'identify' else '/api/ssh-connect'
                payload = {'init_boot_marker': True} if args.action == 'identify' and args.init_boot_marker else {}
                if args.action == 'ssh-open':
                    if client.ssh_user is not None: payload['ssh_user'] = client.ssh_user
                    if client.ssh_password is not None: payload['ssh_password'] = client.ssh_password
                result = client.post(route, **payload)
            else:
                result = client.get('/api/' + args.action)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('ok', True) else 1
    except (ClientError, OSError, ValueError, KeyboardInterrupt) as error:
        print(json.dumps({'ok': False, 'error': str(error) or 'Interrupted'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
