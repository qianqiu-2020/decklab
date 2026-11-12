#!/usr/bin/env python3
"""Ubuntu 22.04 two-node acceptance with separate network/storage namespaces.

Uses the host Ubuntu 22.04 Python stdlib read-only; no package installation or
board access. A third container is a tiny LAN image repository fixture.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def main():
    suffix = uuid.uuid4().hex[:10]
    network = 'decklab-split-' + suffix
    names = [network + '-' + role for role in ('backend', 'web', 'images')]
    created = []
    with tempfile.TemporaryDirectory(prefix='decklab-split-containers-') as directory:
        base = Path(directory)
        for role in ('backend', 'web', 'images', 'tls'):
            (base / role).mkdir()
        payload = b'single-partition-URL-fixture' * 100
        (base / 'images/bootfs.ext4').write_bytes(payload)
        cert, key = base / 'tls/cert.pem', base / 'tls/key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=backend', '-addext', 'subjectAltName=DNS:backend', '-keyout', str(key), '-out', str(cert)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        token = 'isolated-node-test-' + 'a' * 48
        mounts = ['-v', str(ROOT) + ':/app:ro', '-v', str(base / 'tls') + ':/tls:ro',
            '-v', '/usr/bin/python3.10:/usr/bin/python3.10:ro',
            '-v', '/usr/lib/python3.10:/usr/lib/python3.10:ro',
            '-v', '/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu:ro']
        docker('network', 'create', '--opt', 'com.docker.network.driver.mtu=1200', network)
        try:
            def start(role, index, environment, arguments, extra=()):
                name = names[index]
                command = ['run', '-d', '--user', str(os.getuid()) + ':' + str(os.getgid()), '--name', name, '--network', network, '--network-alias', role,
                    '-w', '/app', *mounts, '-v', str(base / role) + ':/state', *extra]
                values = dict(SERIALD_BASE='/state', SERIALD_STATIC='/app', SERIALD_MCP_ENABLED='0',
                    SERIALD_AUTH_TOKEN='container-web-owner', SERIALD_BACKEND_TOKEN=token, **environment)
                for key, value in values.items():
                    command += ['-e', key + '=' + value]
                command += ['ubuntu:22.04', '/usr/bin/python3.10', *arguments]
                docker(*command)
                created.append(name)
                return name
            backend = start('backend', 0, {'SERIALD_BACKEND_CERT': '/tls/cert.pem', 'SERIALD_BACKEND_KEY': '/tls/key.pem'},
                ['-m', 'seriald_core.backend_server', '--bind', '0.0.0.0', '--demo'])
            web = start('web', 1, {'SERIALD_BACKEND_URL': 'https://backend:8899', 'SERIALD_BACKEND_CA': '/tls/cert.pem'},
                ['-m', 'seriald_core.control_server', '--bind', '0.0.0.0', '--no-browser', '--require-backend'],
                ['-p', '127.0.0.1::8898'])
            images = start('images', 2, {}, ['-m', 'http.server', '8080', '--directory', '/state'])
            docker('exec', images, '/usr/bin/python3.10', '-c',
                'import time,urllib.request\n'
                'for attempt in range(100):\n'
                ' try:\n'
                '  urllib.request.urlopen("http://127.0.0.1:8080/bootfs.ext4",timeout=1).close();break\n'
                ' except OSError:time.sleep(.05)\n'
                'else:raise RuntimeError("image repository fixture not ready")')
            address = 'http://' + docker('port', web, '8898/tcp')
            def request(route, body=None):
                data = json.dumps(body).encode() if body is not None else None
                req = Request(address + route, data=data, headers={'Content-Type': 'application/json',
                    'Authorization': 'Bearer container-web-owner'})
                with urlopen(req, timeout=3) as incoming:
                    return json.load(incoming)
            def ready():
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        return request('/api/devices')['devices']
                    except OSError:
                        time.sleep(.1)
                raise RuntimeError('nodes not ready: ' + docker('logs', web) + docker('logs', backend))
            boards = ready()
            board = next(row for row in boards if row['serial'] == 'DEMO-001')
            scope = {'key': board['key'], 'serial': board['serial']}
            task = request('/api/image-download', dict(scope, url='http://images:8080/bootfs.ext4',
                request_id='network-image-import', sha256=hashlib.sha256(payload).hexdigest()))['task']
            for _ in range(100):
                task = request('/api/task?id=' + task['id'])['task']
                if task['state'] not in ('queued', 'running'):
                    break
                time.sleep(.1)
            assert task['state'] == 'completed', task
            assert task['result']['artifact']['sha256'] == hashlib.sha256(payload).hexdigest()
            assert (base / 'backend/registry.sqlite3').is_file()
            assert not (base / 'web/registry.sqlite3').exists()
            assert not (base / 'web/transfers').exists()
            assert list((base / 'backend/transfers').glob('*.bin'))
            started = docker('inspect', '--format', '{{.State.StartedAt}}', backend)
            docker('restart', web)
            # Docker may allocate a new published host port for an ephemeral
            # mapping when restarting the container. The service still binds 8898.
            address = 'http://' + docker('port', web, '8898/tcp')
            after = next(row for row in ready() if row['serial'] == board['serial'])
            assert after['connection_id'] == board['connection_id']
            assert docker('inspect', '--format', '{{.State.StartedAt}}', backend) == started
            print('PASS: Ubuntu 22.04 Web/backend in separate containers; verified TLS/DNS, backend-only URL download/cache/registry, Web restart preserves backend connection')
            print('NOT COVERED: native Windows, hardware, systemd or package installation')
        finally:
            for name in reversed(created):
                docker('rm', '-f', name)
            docker('network', 'rm', network)


if __name__ == '__main__':
    main()
