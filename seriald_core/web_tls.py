"""A second public listener for the same Web handler and control-plane state."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading

from .httpd import BoundedThreadingTCPServer


def local_certificate(folder, hosts):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    now = datetime.now(timezone.utc)
    pem = serialization.Encoding.PEM

    def issue(common_name, issuer=None, issuer_key=None):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        ca = issuer is None
        builder = (x509.CertificateBuilder().subject_name(subject)
                   .issuer_name(subject if ca else issuer.subject).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=10))
                   .not_valid_after(now + timedelta(days=3650 if ca else 365))
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                       key.public_key() if ca else issuer.public_key()), critical=False)
                   .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True)
                   .add_extension(x509.KeyUsage(not ca, False, False, False, False, ca, ca, None, None), critical=True))
        if not ca:
            builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False).add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        cert = builder.sign(key if ca else issuer_key, hashes.SHA256())
        return cert.public_bytes(pem) + key.private_bytes(pem, serialization.PrivateFormat.PKCS8,
                                                          serialization.NoEncryption())

    names = []
    for host in sorted(set(hosts)):
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host.encode('idna').decode('ascii')))

    def write_pem(path, value, *, create=False):
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as out:
            tmp = Path(out.name)
        try:
            with tmp.open('wb') as dest:
                dest.write(value)
                dest.flush()
                os.fsync(dest.fileno())
            if create:
                try:
                    os.link(tmp, path)  # Publish one complete identity across simultaneous first starts.
                except FileExistsError:
                    pass
            else:
                os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    authority = folder / 'issuer.pem'
    if not authority.exists():
        write_pem(authority, issue('DeckLab Local CA'), create=True)
    issuer_pem = authority.read_bytes()
    issuer = x509.load_pem_x509_certificate(issuer_pem)
    leaf = folder / 'server.pem'
    current = x509.load_pem_x509_certificate(leaf.read_bytes()) if leaf.exists() else None
    expires = (current.not_valid_after_utc if hasattr(current, 'not_valid_after_utc')
               else current.not_valid_after.replace(tzinfo=timezone.utc)) if current else now
    if (not current or set(current.extensions.get_extension_for_class(x509.SubjectAlternativeName).value) != set(names)
            or expires <= now + timedelta(days=30)):
        key = serialization.load_pem_private_key(issuer_pem[issuer_pem.index(b'-----BEGIN PRIVATE KEY'):], None)
        write_pem(leaf, issue('DeckLab Web', issuer, key))
    return leaf, issuer.public_bytes(serialization.Encoding.DER)


class WebTLSListener(BoundedThreadingTCPServer):
    def finish_request(self, request, client_address):
        # Run the TLS handshake inside the bounded worker, not the accept loop.
        try:
            with self.context.wrap_socket(request, server_side=True) as secured:
                self.RequestHandlerClass(secured, client_address, self.control)
        except (ssl.SSLError, ConnectionError, TimeoutError):
            pass


@contextmanager
def https_listener(control, base):
    bind = control.server_address[0]
    listener = None
    try:
        port = int(os.environ.get('SERIALD_HTTPS_PORT', '8443'))
        if port:
            if not 1 <= port <= 65535 or port == control.server_address[1]:
                raise ValueError('HTTPS requires a separate port in 1..65535')
            listener = WebTLSListener((bind, port), control.RequestHandlerClass)
            listener.control = control
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            cert = os.environ.get('SERIALD_HTTPS_CERT')
            key = os.environ.get('SERIALD_HTTPS_KEY')
            ca = None
            if bool(cert) != bool(key):
                raise ValueError('set both SERIALD_HTTPS_CERT and SERIALD_HTTPS_KEY')
            if not cert:
                from .control_server import lan_hosts
                hosts = ['localhost', '127.0.0.1', socket.gethostname()]
                hosts.extend(lan_hosts() if bind in ('', '0.0.0.0') else [bind])
                cert, ca = local_certificate(Path(base) / 'web-tls', hosts)
            ctx.load_cert_chain(cert, key or None)
            listener.context = ctx
            control.https_port, control.https_ca = port, ca
            worker = threading.Thread(target=listener.serve_forever, name='decklab-https', daemon=True)
            worker.start()
            print('seriald control on https://%s:%d/' % (bind, port), flush=True)
    except (OSError, ValueError, ImportError) as error:
        if listener:
            listener.server_close()
            listener = None
        print('seriald: native HTTPS unavailable (%s); HTTP remains active' % error, file=sys.stderr, flush=True)
    try:
        yield
    finally:
        if listener:
            listener.shutdown()
            worker.join()
            listener.server_close()
