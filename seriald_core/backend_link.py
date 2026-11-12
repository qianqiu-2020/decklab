"""Authenticated TLS link from one Web node to its hardware-owning backend."""
import http.client
import ssl
from urllib.parse import urlsplit


class BackendConfigurationError(ValueError):
    pass


def validate_token(value):
    if (not isinstance(value, str) or not 32 <= len(value) <= 4096
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise BackendConfigurationError('backend token must contain 32 to 4096 printable ASCII characters')
    return value


class _Connection(http.client.HTTPSConnection):
    def __init__(self, host, port, token, context, timeout):
        super().__init__(host, port, timeout=timeout, context=context)
        self._backend_token = token

    def request(self, method, url, body=None, headers=None, *, encode_chunked=False):
        # The public browser/Agent credential never crosses the node boundary.
        headers = {name: value for name, value in (headers or {}).items()
                   if name.lower() not in ('authorization', 'cookie', 'proxy-authorization')}
        headers['Authorization'] = 'Bearer ' + self._backend_token
        return super().request(method, url, body, headers, encode_chunked=encode_chunked)


class BackendLink:
    """A single pinned origin; no redirects, fallback node, or write retries."""
    def __init__(self, url, token, ca=''):
        try:
            parsed = urlsplit(url)
            port = parsed.port or 443
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.path not in ('', '/')
                    or parsed.query or parsed.fragment or not 1 <= port <= 65535
                    or any(char.isspace() for char in url)):
                raise ValueError()
        except (ValueError, AttributeError, TypeError):
            raise BackendConfigurationError('backend URL must be an HTTPS origin without credentials or a path') from None
        self.host, self.port = parsed.hostname, port
        self.token = validate_token(token)
        try:
            self.context = ssl.create_default_context(cafile=ca or None)
            self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        except (OSError, ssl.SSLError):
            raise BackendConfigurationError('backend CA certificate cannot be loaded') from None

    def connection(self, timeout=30):
        return _Connection(self.host, self.port, self.token, self.context, timeout)


def server_context(certificate, key):
    if not certificate or not key:
        raise BackendConfigurationError('backend TLS certificate and private key are required')
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certificate, key)
        return context
    except (OSError, ssl.SSLError):
        raise BackendConfigurationError('backend TLS certificate or private key cannot be loaded') from None
