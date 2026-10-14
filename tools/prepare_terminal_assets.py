"""Reproduce pinned headless xterm assets for Ubuntu's Node 12 runtime.

Run with Node 22/npm when updating these vendored dependencies. Packaging uses
the committed assets and never invokes npm or downloads runtime JavaScript.
"""
import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ESBUILD = '0.25.11'
PACKAGES = (
    ('@xterm/headless', '5.5.0', 'headless', 'lib-headless/xterm-headless.js',
     'sha512-5xXB7kdQlFBP82ViMJTwwEc3gKCLGKR/eoxQm4zge7GPBl86tCdI0IdPJjoKd8mUSFXz5V7i/25sfsEkP4j46g=='),
    ('@xterm/addon-serialize', '0.13.0', 'addon-serialize', 'lib/addon-serialize.js',
     'sha512-kGs8o6LWAmN1l2NpMp01/YkpxbmO4UrfWybeGu79Khw5K9+Krp7XhXbBTOTc3GJRRhd6EmILjpR8k5+odY39YQ=='))


def main():
    target = ROOT / 'vendor/terminal'
    with tempfile.TemporaryDirectory(prefix='decklab-terminal-assets-') as directory:
        for package, version, name, member, integrity in PACKAGES:
            url = 'https://registry.npmjs.org/%s/-/%s-%s.tgz' % (package, name, version)
            with urllib.request.urlopen(url, timeout=30) as response:
                archive = response.read()
            if 'sha512-' + base64.b64encode(hashlib.sha512(archive).digest()).decode() != integrity:
                raise ValueError('upstream package integrity mismatch: ' + package)
            with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as source:
                data = source.extractfile('package/' + member).read()
            input_file = Path(directory) / (name + '.js')
            input_file.write_bytes(data)
            output = target / Path(member).name
            subprocess.run(['npx', '--yes', '--registry=https://registry.npmjs.org',
                '--package=esbuild@' + ESBUILD, 'esbuild', str(input_file), '--platform=node',
                '--target=node12', '--format=cjs', '--minify', '--log-level=error',
                '--outfile=' + str(output)], check=True)
            manifest = target / (name + '-source.json')
            metadata = json.loads(manifest.read_text(encoding='utf-8'))
            metadata.update(url=url, upstream_js_sha256=hashlib.sha256(data).hexdigest(),
                js_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                transform={'tool': 'esbuild', 'version': ESBUILD, 'target': 'node12',
                           'options': ['--platform=node', '--format=cjs', '--minify']})
            manifest.write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
            print(output)


if __name__ == '__main__':
    main()
