"""Reject legacy RDP with the frozen worker, using loopback and dummy credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import tempfile


def read_packet(connection):
    data = b''
    while len(data) < 4:
        part = connection.recv(4 - len(data))
        if not part:
            return data
        data += part
    if data[:2] != b'\x03\0':
        return data
    length = int.from_bytes(data[2:4], 'big')
    if not 4 <= length <= 65535:
        raise ValueError('invalid test-peer packet length')
    while len(data) < length:
        part = connection.recv(length - len(data))
        if not part:
            break
        data += part
    return data


def probe(executable, pinned, negotiation):
    # X.224 Connection Confirm, either selecting standard RDP (0), or omitting
    # negotiation data as old servers do. No MCS reply or certificate is sent.
    reply = bytes.fromhex('030000130ed000001234000200080000000000' if negotiation
                          else '0300000b06d00000123400')
    finished = threading.Event()
    connections, errors = [], []
    with tempfile.TemporaryDirectory(prefix='decklab-rdp-security-') as directory, socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(3)
        listener.settimeout(.2)
        port = listener.getsockname()[1]
        def serve():
            try:
                while not finished.is_set():
                    try:
                        connection, _ = listener.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        connection.settimeout(2)
                        first = read_packet(connection)
                        connection.sendall(reply)
                        try:
                            following = read_packet(connection)
                        except (socket.timeout, ConnectionResetError):
                            following = b''
                        connections.append({'request_bytes': len(first),
                            'next_packet_bytes': len(following),
                            'next_packet_prefix': following[:16].hex(),
                            'legacy_mcs': following[7:9] == b'\x7f\x65'})
            except Exception as error:
                errors.append(type(error).__name__)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        config = {'provider': 'rdp', 'engine': 'freerdp', 'width': 1280, 'height': 800,
                  'secret_url': 'rdp+ntlm-password://probe-user:dummy-only@127.0.0.1:' + str(port)}
        if pinned:
            config['certificate_sha256'] = '00' * 32
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('SERIALD_') and key not in ('PYTHONPATH', 'PYTHONHOME')}
        environment['SERIALD_BASE'] = directory
        command = [str(executable)] + (['mcp'] if sys.platform == 'win32' else [])
        process = subprocess.Popen(command + ['--desktop-worker'], env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, _ = process.communicate((json.dumps({'id': 1, 'config': config}) + '\n').encode(), timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
            finished.set()
            thread.join(3)
        assert not thread.is_alive() and not errors, errors
        assert process.returncode == 0, 'worker failed instead of rejecting the peer'
        response = json.loads(output.decode('utf-8').strip())
        assert response.get('id') == 1 and response.get('ok') is False and response.get('error'), response
        assert connections, 'no native connection occurred; missing SDK must not pass this test'
        assert all(row['request_bytes'] >= 11 for row in connections), connections
        assert not any(row['legacy_mcs'] for row in connections), 'legacy RDP bypassed certificate policy: ' + json.dumps(connections)
        return {'pinned': pinned, 'negotiation_data': negotiation,
                'connections': connections, 'legacy_rejected': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    executable = args.executable.resolve()
    record = {'scope': 'frozen desktop worker; loopback dummy peer, no target or graphical input',
              'executable_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
              'cases': [probe(executable, pinned, negotiation)
                        for pinned in (True, False) for negotiation in (True, False)]}
    if args.receipt:
        args.receipt.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record))


if __name__ == '__main__':
    main()
