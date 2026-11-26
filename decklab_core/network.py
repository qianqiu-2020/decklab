"""Addresses available for shared browser links and local HTTPS."""
import ipaddress
import json
import os
import socket
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from time import monotonic


def ip_address(host):
    address = ipaddress.ip_address(host)
    if address.version == 6 and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def url_host(host):
    return f'[{host}]' if ':' in host else host


@lru_cache(maxsize=1)
def _interface_addresses(interval):
    if sys.platform.startswith('linux'):
        result = subprocess.check_output(['ip', '-j', '-d', 'address', 'show', 'up'], timeout=2)
        return {
            info['local'] for link in json.loads(result)
            if link['ifname'] == 'tailscale0' or (
                'LOOPBACK' not in link.get('flags', ()) and
                not link['ifname'].startswith(('docker', 'br-')) and
                link.get('linkinfo', {}).get('info_kind') not in ('tun', 'veth'))
            for info in link.get('addr_info', ()) if info.get('scope') == 'global'
        }
    if sys.platform == 'win32':
        powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        command = (
            '[Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() | '
            "Where-Object { $_.OperationalStatus -eq 'Up' } | ForEach-Object { "
            '$_.GetIPProperties().UnicastAddresses | ForEach-Object { $_.Address.ToString() } }'
        )
        result = subprocess.check_output(
            [str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
            timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
        return set(result.decode('ascii').splitlines())
    return set()


def lan_hosts():
    """Suggest usable unicast addresses from the current interfaces."""
    try:
        hosts = set(_interface_addresses(int(monotonic() // 5)))
    except (OSError, subprocess.SubprocessError, ValueError):
        hosts = set()
    if not hosts:
        try:
            hosts.update(item[4][0] for item in socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_UNSPEC, socket.SOCK_STREAM))
        except OSError:
            pass
    addresses = set()
    for host in hosts:
        try:
            address = ip_address(host)
        except ValueError:
            continue
        if not (address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified):
            addresses.add(address)
    return [str(address) for address in sorted(addresses, key=lambda item: (item.version, int(item)))]
