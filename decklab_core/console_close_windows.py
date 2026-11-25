"""Release COM handles when the Windows console window is closed.

Closing the console sends CTRL_CLOSE_EVENT; Python does not handle it, so the
process is ended with an overlapped COM read still pending. Some USB-UART
drivers never finish cancelling that read, leaving the port "in use" until
the adapter is unplugged. Release ports the way pySerial's close() does
(CancelIoEx, then CloseHandle) before Windows terminates the process.
Ctrl+C keeps Python's KeyboardInterrupt path and its complete shutdown.
"""
import ctypes
import os
import threading

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6
# Windows allows about five seconds after CTRL_CLOSE_EVENT; stay well below it.
RELEASE_BUDGET = 3.0

_installed = []  # keep the ctypes callback alive for the process lifetime


def release_on(event, release, after=None):
    """Return True when this handler consumed the console event."""
    if event == CTRL_C_EVENT:
        return False
    if event not in (CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
        return False
    try:
        release(RELEASE_BUDGET)
    finally:
        if after is not None:
            threading.Thread(target=after, daemon=True).start()
    return True


def install(release, after=None, kernel32=None):
    """Register the handler; a missing console (no window) is not an error."""
    if os.name != 'nt' and kernel32 is None:
        return False
    kernel32 = kernel32 if kernel32 is not None else ctypes.WinDLL('kernel32', use_last_error=True)
    prototype = getattr(ctypes, 'WINFUNCTYPE', ctypes.CFUNCTYPE)(ctypes.c_int, ctypes.c_uint32)

    def handler(event):
        return 1 if release_on(int(event), release, after) else 0

    callback = prototype(handler)
    if not kernel32.SetConsoleCtrlHandler(callback, 1):
        return False
    _installed.append(callback)
    return True
