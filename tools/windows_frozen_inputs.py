"""Stage current Web resources and cache the frozen Windows Python runtime."""
import argparse
import hashlib
from pathlib import Path
import re
import shutil
import sys

from bundle_manifest import create, verify


ROOT = Path(__file__).resolve().parents[1]
STATIC_FILES = {
    name: name for name in (
        'index.html', 'static', 'vendor', 'decklab_core/terminal_worker.js',
        'decklab_core/terminal_snapshot.js', 'decklab_core/desktop_gnome_target.py',
        'decklab_core/onie_target.py', 'decklab_core/desktop_lxqt_target.py',
        'decklabctl.py', 'decklab_mcp.py', 'requirements-mcp.txt', 'pyproject.toml',
        'skills/decklab-board/SKILL.md', 'skills/decklab-board/SKILL.zh-CN.md',
    )
}
STATIC_FILES['build/terminal-runtime'] = 'runtime'


def runtime_key(dependencies, desktop):
    inputs = sorted(ROOT.glob('decklab*.py')) + [ROOT / 'decklabctl.py']
    inputs += sorted((ROOT / 'decklab_core').rglob('*.py'))
    inputs += [ROOT / 'packaging/decklab.ico', ROOT / 'tools/build_windows.ps1', Path(__file__)]
    key = hashlib.sha256(dependencies.encode())
    if desktop:
        key.update(hashlib.sha256((desktop / 'runtime-manifest.json').read_bytes()).digest())
    for path in inputs:
        relative = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        if relative == 'decklab_core/version.py':
            # The installed host reads this release's package-version.txt.
            data = re.sub(rb"(?m)^__version__ = '[0-9]+\.[0-9]+\.[0-9]+'$", b"__version__ = '0.0.0'", data)
        key.update(relative.encode() + b"\0")
        key.update(hashlib.sha256(data).digest())
    return key.hexdigest()


def restore(cache, bundle, key):
    if not cache.exists():
        return False
    try:
        if (cache / 'inputs.sha256').read_text().strip() != key:
            return False
        verify(cache / 'decklab')
    except (OSError, ValueError):
        return False
    if bundle.exists():
        shutil.rmtree(bundle)
    shutil.copytree(cache / 'decklab', bundle, ignore=shutil.ignore_patterns('bundle-manifest.json'))
    print('Reusing verified frozen Windows runtime.')
    return True


def save(cache, bundle, key):
    temporary = cache.with_name(cache.name + '.new')
    if temporary.exists():
        shutil.rmtree(temporary)
    shutil.copytree(bundle, temporary / 'decklab')
    create(temporary / 'decklab')
    (temporary / 'inputs.sha256').write_text(key + '\n')
    if cache.exists():
        shutil.rmtree(cache)
    temporary.rename(cache)


def stage(bundle):
    for source, target in STATIC_FILES.items():
        source, target = ROOT / source, bundle / '_internal' / target
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('key', 'restore', 'save', 'stage'))
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--key')
    parser.add_argument('--desktop', type=Path)
    args = parser.parse_args()
    cache = ROOT / 'build/windows-frozen-runtime'
    if args.action == 'key':
        print(runtime_key(args.key, args.desktop))
    elif args.action == 'restore':
        sys.exit(0 if restore(cache, args.bundle, args.key) else 2)
    elif args.action == 'save':
        save(cache, args.bundle, args.key)
    else:
        stage(args.bundle)


if __name__ == '__main__':
    main()
