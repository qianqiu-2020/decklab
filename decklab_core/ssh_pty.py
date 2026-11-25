"""Exec an SSH wrapper with the allocated PTY as its controlling terminal.

Run after Popen(start_new_session=True), rather than using an unsafe Python
preexec_fn inside the multithreaded collector. No reader or shell is created.
"""
import fcntl
import os
import sys
import termios


def main(argv):
    if not argv:
        raise SystemExit('SSH PTY helper requires an executable')
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvp(argv[0], argv)


if __name__ == '__main__':
    main(sys.argv[1:])
