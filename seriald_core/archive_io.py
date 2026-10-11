"""Regular-file-only archive access across Linux and Windows."""
import os
import stat


def open_archive(path):
    """Reject links and nonregular files, including replacement races on Windows."""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise OSError('archive is not a regular file')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise OSError('archive changed while opening')
        source = os.fdopen(fd, 'rb')
    except Exception:
        os.close(fd)
        raise
    return source

