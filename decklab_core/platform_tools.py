"""Locate private bundled USB tools, with PATH support for source installs."""
import os
from pathlib import Path
import shutil
import sys


def adb_executable():
    if sys.platform == 'win32':
        root = Path(sys.executable).resolve().parent
        name = 'adb.exe'
    else:
        root = Path(__file__).resolve().parents[1]
        name = 'adb'
    bundled = root / 'platform-tools' / name
    return str(bundled) if bundled.is_file() and os.access(bundled, os.X_OK) else shutil.which('adb')
