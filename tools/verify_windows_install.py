"""Verify files extracted by the real Windows installer and record their checksums."""
import argparse
import hashlib
import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.bundle_manifest import NAME, verify
from tools.verify_release import inspect_complete_install, DESKTOP_RUNTIME, inspect_desktop_runtime, verify_notices
from decklab_core.version import __version__


def inspect(installation, installer, staging):
    manifest = verify(installation, installed=True)
    if (installation / NAME).read_bytes() != (staging / NAME).read_bytes():
        raise ValueError('Installed file inventory differs from the package build')
    if (installation / 'package-version.txt').read_text(encoding='ascii').strip() != __version__:
        raise ValueError('Installed application version differs from the checked-out version')
    inspect_complete_install(installation, 'windows-amd64')
    inspect_desktop_runtime(installation / DESKTOP_RUNTIME, 'windows-amd64')
    notices = verify_notices(installation / 'third-party')
    if (len(notices.get('native_crypto_providers', [])) != 2
            or len(notices.get('runtime_source_notices', [])) != 3
            or not notices.get('native_source_notices')
            or len(notices.get('corresponding_sources', [])) != 1):
        raise ValueError('Windows third-party source and notice inventory is incomplete')
    return {'schema': 1, 'version': __version__, 'sha256': hashlib.sha256(installer.read_bytes()).hexdigest(),
            'bundle_files': len(manifest['files']), 'distribution_notice_sets': len(notices['distributions'])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('installation', type=Path)
    parser.add_argument('installer', type=Path)
    parser.add_argument('staging', type=Path)
    parser.add_argument('result', type=Path)
    args = parser.parse_args()
    result = inspect(args.installation, args.installer, args.staging)
    args.result.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
