"""One hardware owner per data directory, across local and network launch modes."""
import os
from pathlib import Path


class CollectorLock:
    def __init__(self, base):
        self.path = Path(base) / '.collector-owner.lock'
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.file.write(b'0'); self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close(); self.file = None
            raise RuntimeError('another collector already owns this runtime data directory') from None
        return self

    def __exit__(self, *_):
        if self.file:
            self.file.close(); self.file = None
