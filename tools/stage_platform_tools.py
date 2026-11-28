"""Stage the redistributable ADB and Fastboot components from reviewed SDK archives."""
import hashlib
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from urllib.request import urlopen
from zipfile import ZipFile


VERSION = '37.0.3'
INPUTS = {
    'linux': ('214548146cadb2ca3508143d8a1d4be4c59de5bb837253e3ede89ed74cf450d1',
              ('adb', 'fastboot')),
    'win': ('89d108360a3bf2924d3bea837239edaccc22fd6ff5befd3787c4348a9fe4f09d',
            ('adb.exe', 'fastboot.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll')),
}


def stage(platform, destination):
    digest, binaries = INPUTS[platform]
    filename = f'platform-tools_r{VERSION}-{platform}.zip'
    cache = Path(__file__).resolve().parents[1] / 'build' / 'platform-tools'
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / filename
    if not archive.is_file():
        url = 'https://dl.google.com/android/repository/' + filename
        with urlopen(url, timeout=60) as source:
            data = source.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Platform Tools archive digest mismatch: ' + filename)
        with NamedTemporaryFile(dir=cache, delete=False) as temporary:
            temporary.write(data)
        Path(temporary.name).replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise ValueError('Platform Tools archive digest mismatch: ' + filename)
    destination.mkdir(parents=True, exist_ok=True)
    with ZipFile(archive) as package:
        for name in (*binaries, 'NOTICE.txt', 'source.properties'):
            path = destination / name
            path.write_bytes(package.read('platform-tools/' + name))
            path.chmod(0o755 if name in binaries else 0o644)


if __name__ == '__main__':
    stage(sys.argv[1], Path(sys.argv[2]))
