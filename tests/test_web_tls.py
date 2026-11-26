import http.client
from http.server import BaseHTTPRequestHandler
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from decklab_core.httpd import BoundedThreadingTCPServer, WebTCPServer
from decklab_core.web_tls import https_listener


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        value = self.server.receipt
        self.send_response(200)
        self.send_header('Content-Length', str(len(value)))
        self.end_headers()
        self.wfile.write(value)

    def log_message(self, *args):
        pass


class WebTLSTests(unittest.TestCase):
    @unittest.skipUnless(socket.has_dualstack_ipv6(), 'dual-stack sockets unavailable')
    def test_lan_http_and_https_serve_both_address_families(self):
        with tempfile.TemporaryDirectory() as folder, WebTCPServer(('0.0.0.0', 0), Handler) as server:
            server.receipt = b'one listener'
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                with socket.socket() as free:
                    free.bind(('127.0.0.1', 0))
                    port = free.getsockname()[1]
                with patch.dict(os.environ, DECKLAB_HTTPS_PORT=str(port), DECKLAB_HTTPS_CERT='', DECKLAB_HTTPS_KEY=''):
                    with https_listener(server, folder):
                        trust = ssl.create_default_context(cadata=server.https_ca)
                        hosts = ['127.0.0.1', '::1']
                        if sys.platform.startswith('linux'):
                            # A new local destination can obtain a leaf from the already trusted CA.
                            hosts.append('127.0.0.2')
                        for host in hosts:
                            for conn in (http.client.HTTPConnection(host, server.server_address[1], timeout=3),
                                         http.client.HTTPSConnection(host, port, context=trust, timeout=3)):
                                try:
                                    conn.request('GET', '/')
                                    self.assertEqual(conn.getresponse().read(), server.receipt)
                                finally:
                                    conn.close()
            finally:
                server.shutdown()
                worker.join()

    def test_lan_falls_back_to_ipv4_if_ipv6_is_unavailable(self):
        with patch('socket.has_dualstack_ipv6', return_value=False):
            with WebTCPServer(('0.0.0.0', 0), Handler) as server:
                self.assertEqual(server.server_address[0], '0.0.0.0')
                self.assertFalse(server.dual_stack)

    def test_both_ports_reach_same_control_and_trust_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder, BoundedThreadingTCPServer(('127.0.0.1', 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever)
            server.receipt = os.urandom(16)
            worker.start()
            self.addCleanup(worker.join)
            self.addCleanup(server.shutdown)
            with socket.socket() as free:
                free.bind(('127.0.0.1', 0))
                port = free.getsockname()[1]
            with patch.dict(os.environ, DECKLAB_HTTPS_PORT=str(port), DECKLAB_HTTPS_CERT='', DECKLAB_HTTPS_KEY=''):
                with https_listener(server, folder):
                    self.assertEqual(server.https_status, {'state': 'active', 'port': port, 'certificate': 'decklab-local'})
                    trust = server.https_ca
                    context = ssl.create_default_context(cadata=trust)
                    context.verify_flags |= ssl.VERIFY_X509_STRICT
                    for conn in (http.client.HTTPConnection(*server.server_address, timeout=3),
                                 http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=3)):
                        try:
                            conn.request('GET', '/')
                            self.assertEqual(conn.getresponse().read(), server.receipt)
                        finally:
                            conn.close()
                with https_listener(server, folder):
                    self.assertEqual(server.https_ca, trust)
                self.assertTrue((Path(folder) / 'web-tls/server.pem').is_file())

    def test_https_bind_failure_reports_the_port_and_stage(self):
        with tempfile.TemporaryDirectory() as folder, WebTCPServer(('127.0.0.1', 0), Handler) as server:
            with socket.socket() as occupied:
                occupied.bind(('127.0.0.1', 0))
                occupied.listen()
                port = occupied.getsockname()[1]
                with patch.dict(os.environ, DECKLAB_HTTPS_PORT=str(port)):
                    with https_listener(server, folder):
                        self.assertEqual(server.https_status, {'state': 'failed', 'phase': 'bind', 'port': port})
