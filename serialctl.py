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
        body_limit = 2 * 1024 ** 2 if route == '/api/artifact-append' else 65536
        if data is not None and len(data) > body_limit:
            raise ClientError('Request exceeds the service limit of %d bytes' % body_limit)
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


def stage_upload_task(client, local, remote, overwrite=False):
    """Send a bounded host artifact, then let the collector own UART transfer."""
    path = Path(local).expanduser()
    if not path.is_file():
        raise ClientError('Local source must be a regular file')
    artifact = client.post('/api/artifact-create', name=path.name, size=path.stat().st_size)['artifact']
    # A lost task-start response must never trigger deletion or automatic replay:
    # the collector may already have claimed the artifact.
    submitted = False
    try:
        offset, digest = 0, hashlib.sha256()
        with path.open('rb') as source:
            while True:
                data = source.read(1024 * 1024)
                if not data:
                    break
                result = client.post('/api/artifact-append', id=artifact['id'], offset=offset,
                                     data_b64=base64.b64encode(data).decode('ascii'))['artifact']
                offset += len(data)
                digest.update(data)
                if result.get('received') != offset:
                    raise ClientError('Host staging offset mismatch')
        sealed = client.post('/api/artifact-seal', id=artifact['id'])['artifact']
        if sealed.get('size') != offset or sealed.get('sha256') != digest.hexdigest():
            raise ClientError('Host staging SHA-256 mismatch')
        submitted = True
        return client.post('/api/task-start', kind='file-upload', artifact_id=artifact['id'],
                           path=remote, overwrite=overwrite, connection_id=client.connection_id)
    finally:
        if not submitted:
            try:
                client.post('/api/artifact-delete', id=artifact['id'])
            except ClientError:
                pass  # Idle artifact expiry handles unreachable staging services.


def wait_task(client, response):
    task = response.get('task')
    if not isinstance(task, dict) or not isinstance(task.get('id'), str) or not task['id']:
        raise ClientError('Invalid task submission; inspect task history before retrying')
    task_id = task['id']
    try:
        while True:
            if not isinstance(task, dict) or task.get('id') != task_id:
                raise ClientError('Task response belongs to a different operation')
            state = task.get('state')
            if state == 'completed':
                if not isinstance(task.get('result'), dict):
                    raise ClientError('Invalid task result')
                return dict(task['result'], task_id=task_id)
            if state in ('failed', 'cancelled', 'interrupted'):
                raise ClientError(task.get('error') or state)
            if state not in ('queued', 'running'):
                raise ClientError('Invalid task state')
            time.sleep(0.5)
            task = client.get('/api/task', id=task_id).get('task')
    except KeyboardInterrupt:
        try:
            client.post('/api/task-cancel', id=task_id)
            detail = 'cancellation requested; check final state'
        except ClientError:
            detail = 'cancellation unconfirmed; check final state'
        raise ClientError('Interrupted task %s: %s' % (task_id, detail)) from None
    except ClientError as error:
        raise ClientError('Task %s: %s' % (task_id, error), status=error.status, code=error.code) from error


def file_put(client, local, remote, overwrite=False):
    return wait_task(client, stage_upload_task(client, local, remote, overwrite))


def fetch_task_file(client, task_id, local, overwrite=False):
    """Retrieve a completed download without reading UART or buffering the file."""
    target = Path(local).expanduser().absolute()
    if not target.parent.is_dir():
        raise ClientError('Local destination directory does not exist')
    if not overwrite and os.path.lexists(target):
        raise ClientError('Local destination already exists; pass --overwrite to replace it')
    task = client.get('/api/task', id=task_id).get('task')
    if (not isinstance(task, dict) or task.get('id') != task_id
            or task.get('kind') != 'file-download' or task.get('state') != 'completed'):
        raise ClientError('Task is not a completed file download')
    result = task.get('result') or {}
    if not isinstance(result, dict):
        raise ClientError('Invalid download task metadata')
    size, sha, artifact_id = result.get('size'), result.get('sha256'), result.get('artifact_id')
    if (type(size) is not int or size < 0 or not isinstance(sha, str)
            or not re.fullmatch('[a-f0-9]{64}', sha) or not isinstance(artifact_id, str)
            or not re.fullmatch('[a-f0-9]{24}', artifact_id)):
        raise ClientError('Invalid download task metadata')
    query = {'id': artifact_id}
    if client.key:
        query['key'] = client.key
    if client.serial:
        query['serial'] = client.serial
    headers = {'Authorization': 'Bearer ' + client.token} if client.token else {}
    request = Request(client.url + '/api/artifact-download?' + urlencode(query), headers=headers)
    temp = None
    try:
        with urlopen(request, timeout=client.timeout) as incoming:
            if (incoming.headers.get('Content-Length') != str(size)
                    or incoming.headers.get('X-Checksum-SHA256') != sha):
                raise ClientError('Download headers do not match task metadata')
            digest, received = hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.seriald-', delete=False) as sink:
                temp = sink.name
                while True:
                    data = incoming.read(65536)
                    if not data:
                        break
                    received += len(data)
                    if received > size:
                        raise ClientError('Downloaded file exceeds expected size')
                    sink.write(data)
                    digest.update(data)
                sink.flush()
                os.fsync(sink.fileno())
            if received != size or digest.hexdigest() != sha:
                raise ClientError('Downloaded file size or SHA-256 mismatch')
        if overwrite:
            os.replace(temp, target)
        else:
            os.link(temp, target)
            os.unlink(temp)
        temp = None
        return dict(ok=True, task_id=task_id, local=str(target), size=size, sha256=sha)
    except HTTPError as error:
        error.close()
        raise ClientError('HTTP %s while retrieving task file' % error.code, status=error.code) from error
    except (URLError, OSError, http.client.HTTPException) as error:
        raise ClientError(str(error)) from error
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)


def file_get(client, remote, local, overwrite=False):
    target = Path(local).expanduser().absolute()
    if not target.parent.is_dir():
        raise ClientError('Local destination directory does not exist')
    if not overwrite and os.path.lexists(target):
        raise ClientError('Local destination already exists; pass --overwrite to replace it')
    completed = wait_task(client, client.post('/api/task-start', kind='file-download',
        path=remote, connection_id=client.connection_id))
    return fetch_task_file(client, completed['task_id'], target, overwrite)


def start_copy_task(source, destination, remote, to_path, overwrite=False):
    if source.connection_id == destination.connection_id and source.key == destination.key:
        raise ClientError('Source and destination must be different devices')
    return source.post('/api/task-start', kind='file-copy', connection_id=source.connection_id,
        to_key=destination.key, to_serial=destination.serial or '',
        to_connection_id=destination.connection_id, path=remote, to_path=to_path, overwrite=overwrite)


def file_copy(source, destination, remote, to_path, overwrite=False):
    return wait_task(source, start_copy_task(source, destination, remote, to_path, overwrite))


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
    commands.add_parser('hardware', help='list configured board hardware actions without executing them')
    tasks = commands.add_parser('tasks', help='list durable tasks; optionally select a device')
    tasks.add_argument('--limit', type=nonnegative, default=20)
    tasks.add_argument('--offset', type=nonnegative, default=0)
    task = commands.add_parser('task', help='read a durable task by ID')
    task.add_argument('id')
    task_cancel = commands.add_parser('task-cancel', help='request cancellation of a durable task')
    task_cancel.add_argument('id')
    fetch_file = commands.add_parser('file-fetch', help='save a completed download task with size/SHA-256 verification')
    fetch_file.add_argument('id', help='completed file-download task ID')
    fetch_file.add_argument('--local', required=True)
    fetch_file.add_argument('--overwrite', action='store_true')
    task_start = commands.add_parser('task-start', help='start a background device operation')
    task_start.add_argument('kind', choices=('identify', 'ssh-enable', 'ssh-connect',
        'hardware-power-on', 'hardware-power-off', 'hardware-reset', 'hardware-flash'))
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
    put.add_argument('--background', action='store_true', help='stage locally, then return a durable collector task')
    get = commands.add_parser('file-get', help='download a board file through the serial console')
    get.add_argument('--remote', required=True, help='absolute path on the board')
    get.add_argument('--local', help='local output path; required unless --background')
    get.add_argument('--overwrite', action='store_true')
    get.add_argument('--background', action='store_true', help='return a durable task; retrieve it with file-fetch <task-id> --local <path>')
    copy = commands.add_parser('file-copy', help='copy a file to another board through this host')
    copy.add_argument('--remote', required=True, help='absolute source path')
    copy.add_argument('--to-device', required=True)
    copy.add_argument('--to-path', required=True, help='absolute destination path')
    copy.add_argument('--overwrite', action='store_true')
    copy.add_argument('--background', action='store_true', help='return a durable collector task without waiting')
    return p


def main(argv=None):
    # Agents consume JSON through pipes, where Windows otherwise uses the
    # legacy locale encoding even though API/archive text is UTF-8.
    for stream in (sys.stdout, sys.stderr):
        configure = getattr(stream, 'reconfigure', None)
        if callable(configure):
            configure(encoding='utf-8')
    p = parser()
    args = p.parse_args(argv)
    try:
        client = Client(args.url, args.http_timeout, args.token)
        client.actor = args.actor
        client.ssh_user, client.ssh_password = args.ssh_user, args.ssh_password
        if args.action in ('devices', 'health', 'capabilities', 'diagnostics'):
            result = client.request('/api/' + args.action)
        elif args.action in ('tasks', 'task', 'task-cancel', 'file-fetch'):
            if args.device:
                client.select(args.device)
            if args.action == 'file-fetch':
                result = fetch_task_file(client, args.id, args.local, args.overwrite)
            elif args.action == 'tasks':
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
                result = (stage_upload_task if args.background else file_put)(
                    client, args.local, args.remote, args.overwrite)
            elif args.action == 'file-get':
                if args.background:
                    if args.local:
                        raise ClientError('--background saves on the host; omit --local and retrieve with file-fetch')
                    result = client.post('/api/task-start', kind='file-download', path=args.remote,
                                         connection_id=client.connection_id)
                else:
                    if not args.local:
                        raise ClientError('--local is required unless --background')
                    result = file_get(client, args.remote, args.local, args.overwrite)
            elif args.action == 'file-copy':
                destination = Client(args.url, args.http_timeout, args.token)
                destination.actor = args.actor
                destination.select(args.to_device)
                if args.background:
                    result = start_copy_task(client, destination, args.remote, args.to_path, args.overwrite)
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
