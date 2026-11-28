"""Application version, injected into the Windows installer at packaging time."""
from pathlib import Path
import re
import sys


__version__ = '1.19.50'

if getattr(sys, 'frozen', False) and sys.platform == 'win32':
    packaged_version = (Path(sys.executable).parent / 'package-version.txt').read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', packaged_version):
        raise RuntimeError('Packaged DeckLab version is invalid')
    __version__ = packaged_version
