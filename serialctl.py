#!/usr/bin/env python3
"""Agent-facing client for seriald; never opens a serial port locally."""
import argparse
import base64
import binascii
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import math
import os
import re
import secrets
import shlex
import sys
import tempfile
import hashlib
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


DEFAULT_URL = 'http://127.0.0.1:8898'
OUTPUT_LIMIT = 1024 * 1024
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')


class ClientError(Exception):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class Client:
    def __init__(self, url, timeout=180, token=''):
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.netloc
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ClientError('URL must be an http(s) service URL without credentials/query/fragment')
        self.url = url.rstrip('/')
        self.timeout = timeout
        self.key = None
        self.serial = None
        self.connection_id = None
        self.actor = os.environ.get('SERIALD_ACTOR', 'agent')
        self.ssh_user = None
        self.ssh_password = None
        self.token = token

    def request(self, route, payload=None, query=None, timeout=None):
        if payload is not None:
            payload = dict(payload, actor=self.actor)
            if self.serial and payload.get('key') and route != '/api/task-cancel':
                payload['serial'] = self.serial
            if route.startswith('/api/file-transfer/'):
                payload.setdefault('connection_id', self.connection_id)
        if query and self.serial and query.get('key') and route not in ('/api/tasks', '/api/task'):
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
            try:
                failure = json.loads(detail)
                code = failure.get('code') if isinstance(failure, dict) else None
            except ValueError:
                code = None
            raise ClientError('HTTP %s: %s' % (error.code, detail), status=error.code, code=code) from error
        except (URLError, OSError, ValueError, http.client.HTTPException) as error:
            raise ClientError(str(error)) from error
        if not isinstance(result, dict):
            raise ClientError('Expected a JSON object from seriald')
        if result.get('ok') is False:
            raise ClientError(result.get('error') or 'Service reported failure', code=result.get('code'))
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
        self.connection_id = matches[0].get('connection_id')
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


def _abort_transfer(client, transfer_id):
    if transfer_id:
        try:
            client.post('/api/file-transfer/abort', id=transfer_id)
        except ClientError:
            pass


def _upload_slice(client, transfer_id, offset, data):
    """Fit a raw slice under the board's canonical tty line limit."""
    length = len(data)
    while length:
        try:
            result = client.post('/api/file-transfer/chunk', id=transfer_id, offset=offset,
                                 data_b64=base64.b64encode(data[:length]).decode('ascii'))
            if result.get('offset') != offset + length:
                raise ClientError('Upload offset mismatch')
            return result
        except ClientError as error:
            if error.status != 413 or length == 1:
                raise
            length = max(1, length // 2)
    raise ClientError('Empty upload slice')


def file_put(client, local, remote, overwrite=False):
    path = Path(local).expanduser()
    if not path.is_file():
        raise ClientError('Local source must be a regular file')
    size = path.stat().st_size
    transfer_id = None
    try:
        started = client.post('/api/file-transfer/start', direction='upload', path=remote,
                              size=size, overwrite=overwrite)
        transfer_id = started['id']
        batch_size = started.get('batch_size', started['chunk_size'])
        offset = 0
        with path.open('rb') as source:
            while offset < size:
                data = source.read(min(batch_size, size - offset))
                if not data:
                    raise ClientError('Local file shrank during upload')
                if started.get('batch_size'):
                    response = client.post('/api/file-transfer/batch', id=transfer_id, offset=offset,
                                           data_b64=base64.b64encode(data).decode('ascii'))
                    if response.get('offset') != offset + len(data):
                        raise ClientError('Upload batch offset mismatch')
                    offset = response['offset']
                else:
                    sent = 0
                    while sent < len(data):
                        response = _upload_slice(client, transfer_id, offset, data[sent:])
                        sent += response['offset'] - offset
                        offset = response['offset']
        result = client.post('/api/file-transfer/finish', id=transfer_id)
        transfer_id = None
        return result
    finally:
        _abort_transfer(client, transfer_id)


def file_get(client, remote, local, overwrite=False):
    target = Path(local).expanduser().absolute()
    if not target.parent.is_dir():
        raise ClientError('Local destination directory does not exist')
    if not overwrite and os.path.lexists(target):
        raise ClientError('Local destination already exists; pass --overwrite to replace it')
    transfer_id = None
    temp = None
    try:
        started = client.post('/api/file-transfer/start', direction='download', path=remote)
        transfer_id = started['id']
        expected_size = started['size']
        digest = hashlib.sha256()
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.seriald-', delete=False) as sink:
            temp = sink.name
            offset = 0
            while offset < expected_size:
                response = client.post('/api/file-transfer/chunk', id=transfer_id, offset=offset)
                data = base64.b64decode(response['data_b64'], validate=True)
                if not data or response.get('offset') != offset + len(data):
                    raise ClientError('Download offset mismatch')
                sink.write(data)
                digest.update(data)
                offset += len(data)
        result = client.post('/api/file-transfer/finish', id=transfer_id)
        transfer_id = None
        if offset != expected_size or digest.hexdigest() != result.get('sha256'):
            raise ClientError('Downloaded file SHA-256 mismatch')
        if overwrite:
            os.replace(temp, target)
        else:
            os.link(temp, target)
            os.unlink(temp)
        temp = None
        return dict(result, local=str(target))
    finally:
        _abort_transfer(client, transfer_id)
        if temp and os.path.exists(temp):
            os.unlink(temp)


def file_copy(source, destination, remote, to_path, overwrite=False):
    if source.connection_id == destination.connection_id and source.key == destination.key:
        raise ClientError('Source and destination must be different devices')
    source_id = destination_id = None
    source_seconds = destination_seconds = 0.0
    def timed_post(client, route, **payload):
        started = time.monotonic()
        result = client.post(route, **payload)
        return result, time.monotonic() - started
    try:
        incoming = source.post('/api/file-transfer/start', direction='download', path=remote)
        source_id = incoming['id']
        outgoing = destination.post('/api/file-transfer/start', direction='upload', path=to_path,
                                    size=incoming['size'], overwrite=overwrite)
        destination_id = outgoing['id']
        offset = 0
        started = time.monotonic()
        # Each board owns a different serial port. Prefetch the next source
        # batch while the current one is being written to the destination.
        batch_size = outgoing.get('batch_size', incoming['chunk_size'])
        def read_batch(first_offset):
            data = bytearray()
            seconds = 0.0
            while (first_offset + len(data) < incoming['size']
                   and len(data) < batch_size):
                position = first_offset + len(data)
                chunk, elapsed = timed_post(source, '/api/file-transfer/chunk',
                                            id=source_id, offset=position)
                part = base64.b64decode(chunk['data_b64'], validate=True)
                if not part or chunk.get('offset') != position + len(part):
                    raise ClientError('Source transfer offset mismatch')
                data.extend(part)
                seconds += elapsed
            return bytes(data), seconds
        with ThreadPoolExecutor(max_workers=1) as reader:
            pending = reader.submit(read_batch, 0) if incoming['size'] else None
            while offset < incoming['size']:
                data, read_seconds = pending.result()
                source_seconds += read_seconds
                next_offset = offset + len(data)
                if not data or next_offset > incoming['size']:
                    raise ClientError('Source transfer offset mismatch')
                pending = (reader.submit(read_batch, next_offset)
                           if next_offset < incoming['size'] else None)
                write_started = time.monotonic()
                if outgoing.get('batch_size'):
                    sent = destination.post('/api/file-transfer/batch', id=destination_id,
                                            offset=offset,
                                            data_b64=base64.b64encode(data).decode('ascii'))
                else:
                    sent_offset = offset
                    while sent_offset < next_offset:
                        sent = _upload_slice(destination, destination_id, sent_offset,
                                             data[sent_offset - offset:])
                        sent_offset = sent['offset']
                write_seconds = time.monotonic() - write_started
                destination_seconds += write_seconds
                if sent.get('offset') != next_offset:
                    raise ClientError('Destination transfer offset mismatch')
                offset = next_offset
        verified = source.post('/api/file-transfer/finish', id=source_id)
        source_id = None
        if verified.get('sha256') != incoming['sha256']:
            raise ClientError('Source file changed during transfer')
        result = destination.post('/api/file-transfer/finish', id=destination_id)
        destination_id = None
        if result.get('sha256') != verified['sha256']:
            raise ClientError('Destination SHA-256 mismatch')
        elapsed = time.monotonic() - started
        size = incoming['size']
        return dict(result, source_device=source.key, destination_device=destination.key,
                    rate_diagnostics={
                        'source_kib_s': round(size / max(source_seconds, 0.001) / 1024, 2),
                        'destination_kib_s': round(size / max(destination_seconds, 0.001) / 1024, 2),
                        'effective_kib_s': round(size / max(elapsed, 0.001) / 1024, 2),
                        'source_request_s': round(source_seconds, 3),
                        'destination_request_s': round(destination_seconds, 3),
                        'total_s': round(elapsed, 3)})
    finally:
        _abort_transfer(source, source_id)
        _abort_transfer(destination, destination_id)


def nonnegative(value):
    value = int(value)
    if value < 0:
        raise argparse.ArgumentTypeError('value must be nonnegative')
    return value


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', default=os.environ.get('SERIALD_URL', DEFAULT_URL))
    p.add_argument('--http-timeout', type=positive_timeout, default=180)
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
    commands.add_parser('capabilities', help='show supported protocol features without accessing boards')
    commands.add_parser('diagnostics', help='export a sanitized platform summary without logs or credentials')
    tasks = commands.add_parser('tasks', help='list durable tasks; optionally select a device')
    tasks.add_argument('--limit', type=nonnegative, default=20)
    tasks.add_argument('--offset', type=nonnegative, default=0)
    task = commands.add_parser('task', help='read a durable task by ID')
    task.add_argument('id')
    task_cancel = commands.add_parser('task-cancel', help='request cancellation of a durable task')
    task_cancel.add_argument('id')
    task_start = commands.add_parser('task-start', help='start a background device operation')
    task_start.add_argument('kind', choices=('identify', 'ssh-enable', 'ssh-connect'))
    commands.add_parser('operation', help='show the active serial operation and cancellation ID')
    cancel = commands.add_parser('operation-cancel', help='cancel your active serial operation by ID')
    cancel.add_argument('id', help='operation ID returned by operation')
    commands.add_parser('input-claim', help='reserve serial input for your actor; others can watch')
    release = commands.add_parser('input-release', help='release your serial input reservation')
    release.add_argument('id', help='reservation ID returned by input-claim or operation')
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
    commands.add_parser('logs', help='list stored log files and boot IDs')
    boot_log = commands.add_parser('boot-log', help='read one complete archived boot, including across days')
    boot_choice = boot_log.add_mutually_exclusive_group(required=True)
    boot_choice.add_argument('--latest', action='store_true', help='read the newest detected boot')
    boot_choice.add_argument('--id', help='stable boot ID from logs')
    boot_search = commands.add_parser('boot-search', help='search all archived boots on this device')
    boot_search.add_argument('--query', required=True)
    boot_search.add_argument('--offset', type=nonnegative, default=0)

    compare = commands.add_parser('boot-compare', help='compare two archived boots, possibly across boards')
    compare.add_argument('--baseline-id', required=True, help='boot ID from logs on baseline device')
    compare.add_argument('--target-id', required=True, help='boot ID from logs on --device')
    compare.add_argument('--baseline-device', help='different baseline board; defaults to --device')
    commands.add_parser('activity', help='show persisted SSH sessions and agent activity')
    log = commands.add_parser('log', help='read a stored log online')
    log.add_argument('--file', required=True)
    log.add_argument('--off', type=nonnegative)
    rename = commands.add_parser('rename', help='change a known board label without moving its logs')
    rename.add_argument('--label', required=True)
    put = commands.add_parser('file-put', help='upload a local file through the serial console')
    put.add_argument('--local', required=True)
    put.add_argument('--remote', required=True, help='absolute path on the board')
    put.add_argument('--overwrite', action='store_true')
    get = commands.add_parser('file-get', help='download a board file through the serial console')
    get.add_argument('--remote', required=True, help='absolute path on the board')
    get.add_argument('--local', required=True)
    get.add_argument('--overwrite', action='store_true')
    copy = commands.add_parser('file-copy', help='copy a file to another board through this host')
    copy.add_argument('--remote', required=True, help='absolute source path')
    copy.add_argument('--to-device', required=True)
    copy.add_argument('--to-path', required=True, help='absolute destination path')
    copy.add_argument('--overwrite', action='store_true')
    copy.add_argument('--background', action='store_true', help='return a durable collector task without waiting')
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        client = Client(args.url, args.http_timeout, args.token)
        client.actor = args.actor
        client.ssh_user, client.ssh_password = args.ssh_user, args.ssh_password
        if args.action in ('devices', 'health', 'capabilities', 'diagnostics'):
            result = client.request('/api/' + args.action)
        elif args.action in ('tasks', 'task', 'task-cancel'):
            if args.device:
                client.select(args.device)
            if args.action == 'tasks':
                query = dict(limit=args.limit, offset=args.offset)
                if args.device:
                    query['target_serial'] = client.serial
                result = client.get('/api/tasks', **query)
            elif args.action == 'task':
                result = client.get('/api/task', id=args.id)
            else:
                result = client.post('/api/task-cancel', id=args.id)
        else:
            if not args.device:
                raise ClientError('--device is required; run devices to find the full key')
            client.select(args.device)
            if args.action == 'task-start':
                result = client.post('/api/task-start', kind=args.kind, connection_id=client.connection_id)
            elif args.action == 'read':
                result, data = client.read(args.sid, args.off)
                dropped = max(0, len(data) - args.tail) if args.tail else 0
                data = data[dropped:]
                result.update(data=data.decode('utf-8', 'replace'),
                              data_b64=base64.b64encode(data).decode('ascii'),
                              truncated_bytes=dropped, start_off=result['off'] - len(data))
            elif args.action == 'rename':
                result = client.post('/api/label', label=args.label)
            elif args.action == 'operation-cancel':
                result = client.post('/api/operation-cancel', id=args.id)
            elif args.action == 'input-claim':
                result = client.post('/api/input-claim')
            elif args.action == 'input-release':
                result = client.post('/api/input-release', id=args.id)
            elif args.action == 'boot-log':
                boot_id = args.id
                if args.latest:
                    boots = client.get('/api/logs').get('boots', [])
                    if not boots:
                        raise ClientError('No detected boot is archived; collection may have started after boot')
                    boot_id = boots[0]['id']
                result = client.get('/api/boot-log', id=boot_id)
            elif args.action == 'boot-search':
                result = client.get('/api/boot-search', q=args.query, offset=args.offset)
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
            elif args.action == 'file-put':
                result = file_put(client, args.local, args.remote, args.overwrite)
            elif args.action == 'file-get':
                result = file_get(client, args.remote, args.local, args.overwrite)
            elif args.action == 'file-copy':
                destination = Client(args.url, args.http_timeout, args.token)
                destination.actor = args.actor
                destination.select(args.to_device)
                if args.background:
                    result = client.post('/api/task-start', kind='file-copy',
                        connection_id=client.connection_id, to_key=destination.key,
                        to_serial=destination.serial, to_connection_id=destination.connection_id,
                        path=args.remote, to_path=args.to_path, overwrite=args.overwrite)
                else:
                    result = file_copy(client, destination, args.remote, args.to_path, args.overwrite)
            elif args.action == 'boot-compare':
                baseline = client
                if args.baseline_device:
                    baseline = Client(args.url, args.http_timeout, args.token)
                    baseline.select(args.baseline_device)
                result = client.request('/api/boot-compare', query={
                    'left_key': baseline.key, 'left_serial': baseline.serial,
                    'left_id': args.baseline_id, 'right_key': client.key,
                    'right_serial': client.serial, 'right_id': args.target_id})
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
