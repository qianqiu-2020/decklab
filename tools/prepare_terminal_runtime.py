"""Bundle a hash-pinned Windows Node runtime; no developer installation needed."""
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

VERSION = '22.23.3'
ARCHIVE_SHA256 = '2b0ff57b049cda1bbcea2240eec20467018713c1efe1f7360c2681859b90ed71'
URL = 'https://nodejs.org/dist/v%s/node-v%s-win-x64.zip' % (VERSION, VERSION)


def prepare(root, download=None):
    root = Path(root)
    target = root / 'build/terminal-runtime'
    cache = root / 'build/terminal-runtime.zip'
    target.mkdir(parents=True, exist_ok=True)
    if cache.is_file() and hashlib.sha256(cache.read_bytes()).hexdigest() == ARCHIVE_SHA256:
        data = cache.read_bytes()
    else:
        data = download(URL) if download else urllib.request.urlopen(URL, timeout=60).read()
        if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
            raise ValueError('Windows Node runtime archive hash mismatch')
        cache.write_bytes(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in ('node.exe', 'LICENSE'):
            (target / name).write_bytes(archive.read('node-v%s-win-x64/%s' % (VERSION, name)))
    (target / 'source.json').write_text(json.dumps({'component': 'Node.js', 'version': VERSION,
        'url': URL, 'archive_sha256': ARCHIVE_SHA256,
        'executable_sha256': hashlib.sha256((target / 'node.exe').read_bytes()).hexdigest(),
        'license_sha256': hashlib.sha256((target / 'LICENSE').read_bytes()).hexdigest()}, indent=2) + '\n', encoding='utf-8')
    return target


if __name__ == '__main__':
    print(prepare(Path(__file__).resolve().parents[1]))
