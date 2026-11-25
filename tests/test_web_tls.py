import http.client
from http.server import BaseHTTPRequestHandler
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

from decklab_core.httpd import BoundedThreadingTCPServer
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
