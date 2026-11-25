"""Credential-redacted connection diagnostics shared by desktop IPC and HTTP."""
import errno
import json
import re
import socket
import ssl
import traceback
from urllib.parse import quote, unquote, urlsplit


class DesktopConnectionError(RuntimeError):
    def __init__(self, details):
        self.details = details
        self.code = details.get('code', 'desktop-connection-failed')
        super().__init__('远程桌面连接失败 [' + self.code + '] · '
            + ' / '.join(str(details.get(key, '')) for key in ('provider', 'engine', 'stage'))
            + ' · ' + details.get('endpoint', '') + ' · ' + details.get('reason', ''))


def redact(value, config):
    config = config if isinstance(config, dict) else {}
    value = str(value)
    secret = config.get('secret_url', '')
    secrets = [secret] if secret else []
    try:
        url = urlsplit(secret)
        if url.password:
            secrets.append(url.password)
        password = unquote(url.password or (url.username if config.get('provider') == 'vnc' else '') or '')
        if password:
            secrets.extend((password, quote(password, safe=''), json.dumps(password)[1:-1]))
    except ValueError:
        pass
    for key in ('password', 'token', 'authorization'):
        if isinstance(config.get(key), str) and config[key]:
            secrets.append(config[key])
    argv = config.get('argv') or []
    if not isinstance(argv, (list, tuple)):
        argv = []
    for index, argument in enumerate(argv):
        if index and argv[index - 1] in ('--password', '--token', '--secret', '--authorization') and isinstance(argument, str):
            secrets.append(argument)
    for secret in sorted(set(secrets), key=len, reverse=True):
        value = value.replace(secret, '[REDACTED]')
    value = re.sub(r'(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+@', r'\1[REDACTED]@', value)
    value = re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+', r'\1[REDACTED]', value)
    value = re.sub(r'''(?i)((?:password|secret_url|token|authorization)["']?\s*[:=]\s*)["']?[^\s,}\n]+''', r'\1[REDACTED]', value)
    return value[:24000]


def connection_error(config, stage, error, native=None):
    config = config if isinstance(config, dict) else {}
    if isinstance(error, DesktopConnectionError):
        return DesktopConnectionError({key: redact(value, config) if isinstance(value, str)
                                       and key not in ('code', 'provider', 'engine', 'stage', 'exception', 'native_code') else value
                                       for key, value in error.details.items()})
    provider = config.get('provider', 'unknown')
    details = {'provider': provider, 'engine': config.get('engine', 'aardwolf' if provider == 'rdp' else provider), 'stage': stage,
               'exception': type(error).__name__, 'reason': redact(str(error), config)[:2048]}
    try:
        url = urlsplit(config.get('secret_url', ''))
        details['endpoint'] = (url.hostname or '') + ':' + str(url.port or (3389 if provider == 'rdp' else 5900))
    except ValueError:
        details['endpoint'] = '(invalid endpoint)'
    code = 'desktop-connection-failed'
    root = error
    for _ in range(8):
        if not isinstance(root.__cause__, Exception):
            break
        root = root.__cause__
    if type(getattr(root, 'errno', None)) is int:
        details['os_errno'] = root.errno
    if stage in ('runtime', 'configuration') or isinstance(root, ImportError):
        if isinstance(root, ImportError):
            details['stage'] = stage = 'runtime'
        code = 'desktop-' + stage + '-failed'
    elif isinstance(root, ssl.SSLCertVerificationError):
        code, details['stage'] = 'desktop-certificate-rejected', 'certificate'
    elif isinstance(root, socket.gaierror):
        code, details['stage'] = 'desktop-dns-failed', 'dns'
    elif isinstance(error, (TimeoutError, socket.timeout)) or isinstance(root, (TimeoutError, socket.timeout)):
        code = 'desktop-connection-timeout'
    elif isinstance(root, ConnectionRefusedError) or getattr(root, 'errno', None) == errno.ECONNREFUSED:
        code, details['stage'] = 'desktop-connection-refused', 'tcp'
    if native:
        details['native_code'] = '0x%08X' % native['code']
        details['native_reason'] = redact(native['reason'], config)[:1024]
        details['reason'] = details['native_reason'] + ' · ' + details['reason']
    reason = details['reason'].lower()
    if 'certificate:' in reason:
        code = 'desktop-certificate-rejected'
        details['stage'] = 'certificate'
    elif any(word in reason for word in ('errconnect_authentication_failed', 'errconnect_logon_failure',
            'errconnect_wrong_password', 'errconnect_account_', 'status_logon_failure', 'authentication failed')):
        code = 'desktop-authentication-failed'
        details['stage'] = 'authentication'
    elif 'errconnect_dns_' in reason:
        code, details['stage'] = 'desktop-dns-failed', 'dns'
    elif 'errconnect_tls_connect_failed' in reason:
        code, details['stage'] = 'desktop-tls-failed', 'tls'
    elif 'errconnect_security_nego_connect_failed' in reason:
        code, details['stage'] = 'desktop-protocol-failed', 'negotiation'
    elif 'errconnect_connect_failed' in reason or 'errconnect_connect_transport_failed' in reason:
        code, details['stage'] = 'desktop-network-failed', 'transport'
    details['code'] = code
    # Keep the actual chain and source locations, not local variable values.
    trace = redact(''.join(traceback.format_exception(type(error), error, error.__traceback__)), config)
    details['traceback'], details['traceback_truncated'] = trace[:8000], len(trace) > 8000
    return DesktopConnectionError(details)
