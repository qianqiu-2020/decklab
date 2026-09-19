"""Bounded HTTP servers for TCP control traffic and the local Unix API."""
import os
import socketserver
import threading


class _BoundedMixIn:
    daemon_threads = True
    max_workers = 64
    request_timeout = 20.0

    def _init_workers(self):
        self._workers = threading.BoundedSemaphore(self.max_workers)

    def process_request(self, request, client_address):
        if not self._workers.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(self.request_timeout)
            super().process_request(request, client_address)
        except BaseException:
            self._workers.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._workers.release()


class BoundedThreadingTCPServer(_BoundedMixIn, socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        self._init_workers()
        super().__init__(*args, **kwargs)


class BoundedThreadingUnixServer(_BoundedMixIn, socketserver.ThreadingUnixStreamServer):
    def __init__(self, path, handler):
        self._init_workers()
        super().__init__(path, handler)
        os.chmod(path, 0o660)
