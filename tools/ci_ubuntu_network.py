"""Diagnose Ubuntu DNS; --repair is restricted to ephemeral GitHub-hosted CI."""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import struct
import subprocess
import time


HOSTS = ('archive.ubuntu.com', 'security.ubuntu.com', 'ports.ubuntu.com', 'azure.archive.ubuntu.com')
RESOLVERS = ('1.1.1.1', '8.8.8.8')


def command(arguments):
    try:
        return subprocess.run(arguments, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        print(arguments[0] + ': unavailable or timed out')
        return None


def resolves(hosts, prefix=()):
    results = {}
    for host in hosts:
        result = command([*prefix, 'getent', 'ahostsv4', host])
        ok = result is not None and result.returncode == 0 and bool(result.stdout.strip())
        print(('_apt' if '_apt' in prefix else 'root' if prefix else 'normal') +
              ' DNS ' + host + ': ' + ('ok' if ok else 'failed'))
        results[host] = ok
    return results


def read_name(data, offset):
    labels, seen, end = [], set(), None
    for _ in range(128):
        if offset >= len(data) or offset in seen:
            break
        seen.add(offset)
        size = data[offset]
        offset += 1
        if not size:
            return tuple(labels), end if end is not None else offset
        if size & 0xc0 == 0xc0:
            if offset >= len(data) or ((size & 63) << 8 | data[offset]) >= len(data):
                break
            if end is None:
                end = offset + 1
            offset = (size & 63) << 8 | data[offset]
            continue
        if size > 63 or offset + size > len(data):
            break
        labels.append(data[offset:offset + size].lower())
        if sum(map(len, labels)) + len(labels) > 254:
            break
        offset += size
    raise ValueError('invalid DNS name')


def valid_answer(data, query):
    if len(data) < 12 or len(query) < 12:
        return False
    ident, flags, questions, answers, _, _ = struct.unpack('!6H', data[:12])
    if (ident != int.from_bytes(query[:2], 'big') or flags & 0x8000 == 0
            or flags & 0x7a0f or questions != 1 or not answers or data[12:len(query)] != query[12:]):
        return False
    offset, addresses, aliases = len(query), {}, {}
    try:
        question, end = read_name(query, 12)
        if end + 4 != len(query) or query[end:] != b'\0\1\0\1':
            return False
        for _ in range(answers):
            owner, offset = read_name(data, offset)
            kind, cls, _, size = struct.unpack('!HHIH', data[offset:offset + 10])
            offset += 10
            if offset + size > len(data):
                return False
            if kind == 1 and cls == 1:
                if size != 4:
                    return False
                address = ipaddress.IPv4Address(data[offset:offset + size])
                if not address.is_global or address.is_multicast:
                    return False
                addresses[owner] = True
            elif kind == 5 and cls == 1:
                target, end = read_name(data, offset)
                if end != offset + size or owner in aliases and aliases[owner] != target:
                    return False
                aliases[owner] = target
            offset += size
        reachable = set()
        while question in aliases:
            if question in reachable:
                return False
            reachable.add(question)
            question = aliases[question]
        reachable.add(question)
    except (ValueError, struct.error):
        return False
    return question in addresses and set(addresses) <= reachable and set(aliases) <= reachable


def public_dns(resolver, host):
    question = b''.join(bytes([len(part)]) + part.encode('ascii') for part in host.split('.')) + b'\0\0\1\0\1'
    query = secrets.token_bytes(2) + struct.pack('!5H', 0x0100, 1, 0, 0, 0) + question
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.settimeout(2)
            connection.sendto(query, (resolver, 53))
            data, source = connection.recvfrom(4096)
        return source == (resolver, 53) and valid_answer(data, query)
    except OSError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repair', action='store_true')
    parser.add_argument('--hosts', nargs='+', choices=HOSTS, default=HOSTS)
    args = parser.parse_args(argv)
    if args.repair and (os.environ.get('GITHUB_ACTIONS') != 'true'
                        or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'):
        parser.error('--repair requires GITHUB_ACTIONS=true and RUNNER_ENVIRONMENT=github-hosted')
    try:
        state = Path('/etc/resolv.conf').stat()
        print('resolv.conf uid=%d gid=%d mode=%o' % (state.st_uid, state.st_gid, state.st_mode & 0o777))
        for line in Path('/etc/resolv.conf').read_text().splitlines():
            if re.match(r'^\s*(nameserver|search|options)\s', line):
                print(line.split('#', 1)[0])
    except OSError:
        print('resolv.conf: unavailable')
    service = command(['systemctl', 'is-active', 'systemd-resolved'])
    print('systemd-resolved: ' + ('active' if service is not None and service.returncode == 0 else 'unavailable'))
    if not all(shutil.which(name) for name in ('getent', 'sudo')):
        print('Required getent/sudo command unavailable; no DNS change')
        return 1
    normal = resolves(args.hosts)
    apt = resolves(args.hosts, ('sudo', '-n', '-u', '_apt'))
    if all(normal.values()) and all(apt.values()):
        return 0
    root = resolves(args.hosts, ('sudo', '-n'))
    if any(normal[host] != apt[host] or normal[host] != root[host] for host in args.hosts):
        print('Resolver results differ by user; diagnose APT/NSS permissions, no global DNS change')
        return 1
    resolver = next((server for server in RESOLVERS if all(public_dns(server, host) for host in args.hosts)), None)
    if not resolver:
        print('No public DNS resolver verified; no DNS change')
        return 1
    print('Verified public DNS resolver: ' + resolver)
    if not args.repair:
        print('Read-only diagnosis complete; default resolver failed')
        return 1
    if (not all(shutil.which(name) for name in ('resolvectl', 'ip', 'systemctl'))
            or service is None or service.returncode != 0):
        print('Required resolver commands/service unavailable; no DNS change')
        return 1
    route = command(['ip', '-4', 'route', 'show', 'default'])
    match = re.search(r'\bdev ([A-Za-z0-9_.:][A-Za-z0-9_.:-]{0,63})(?:\s|$)', route.stdout) if route is not None and route.returncode == 0 else None
    if not match:
        print('Default network interface unavailable; no DNS change')
        return 1
    for arguments in (['dns', match[1], resolver], ['domain', match[1], '~.'], ['flush-caches']):
        result = command(['sudo', '-n', 'resolvectl', *arguments])
        if result is None or result.returncode != 0:
            print('CI DNS repair failed at resolvectl ' + arguments[0])
            return 1
    for attempt in range(3):
        normal = resolves(args.hosts)
        apt = resolves(args.hosts, ('sudo', '-n', '-u', '_apt'))
        if all(normal.values()) and all(apt.values()):
            print('CI DNS lookups restored; original APT sources and signature checks remain unchanged')
            return 0
        if attempt < 2:
            time.sleep(1)
    print('CI DNS repair did not restore all requested lookups')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
