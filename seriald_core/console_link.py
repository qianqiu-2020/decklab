"""Pair a USB flashing attachment with the UART console of the same board.

The host sends one read-only ``getvar:decklab-link-<nonce>`` over USB. SpacemiT
K1/K3 BootROM prints ``fastboot_handle_command: <name>`` and U-Boot prints
``WARNING: unknown variable: <name>`` on the board UART, so the console that
echoes the nonce is wired to that USB attachment. No firmware change, no UART
write and no timing heuristic is involved: a nonce is fresh, single-use and
only counts when exactly one console echoes it.
"""
import secrets
import threading
import time

LINK_PREFIX = 'decklab-link-'


class ConsoleLinks:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}          # nonce -> consoles that echoed it

    def start(self):
        token = secrets.token_hex(8)
        with self._lock:
            self._pending[token] = []
        return token

    def observe(self, device, tail):
        """Called by every console reader with its recent output."""
        if not self._pending:
            return
        with self._lock:
            for token, hits in self._pending.items():
                if LINK_PREFIX + token in tail and not any(hit is device for hit in hits):
                    hits.append(device)

    def finish(self, token, wait=1.5, settle=0.3):
        """Return consoles that echoed ``token``; the nonce is retired afterwards."""
        deadline = time.monotonic() + wait
        try:
            while True:
                with self._lock:
                    hits = list(self._pending.get(token, ()))
                if hits or time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            if hits:
                # A second console echoing the same nonce means ambiguous wiring.
                time.sleep(settle)
                with self._lock:
                    hits = list(self._pending.get(token, ()))
            return hits
        finally:
            self.cancel(token)

    def cancel(self, token):
        with self._lock:
            self._pending.pop(token, None)
