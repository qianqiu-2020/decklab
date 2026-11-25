"""Stage current Web resources and cache the frozen Windows Python runtime."""
import argparse
import hashlib
from pathlib import Path
import shutil
import sys

from bundle_manifest import create, verify


ROOT = Path(__file__).resolve().parents[1]
STATIC_FILES = {
    name: name for name in (
        'index.html', 'static', 'vendor', 'seriald_core/terminal_worker.js',
        'seriald_core/terminal_snapshot.js', 'seriald_core/desktop_gnome_target.py',
        'seriald_core/onie_target.py', 'seriald_core/desktop_lxqt_target.py',
        'serialctl.py', 'seriald_mcp.py', 'requirements-mcp.txt', 'pyproject.toml',
        'skills/seriald-board/SKILL.md', 'skills/seriald-board/SKILL.zh-CN.md',
    )
}
STATIC_FILES['build/terminal-runtime'] = 'runtime'


def runtime_key(dependencies, desktop):
    inputs = sorted(ROOT.glob('seriald*.py')) + [ROOT / 'serialctl.py']
    inputs += sorted((ROOT / 'seriald_core').rglob('*.py'))
    inputs += [ROOT / 'packaging/decklab.ico', ROOT / 'tools/build_windows.ps1', Path(__file__)]
    if desktop:
        inputs.append(desktop / 'runtime-manifest.json')
    key = hashlib.sha256(dependencies.encode())
    for path in inputs:
        key.update(str(path).encode())
        key.update(hashlib.sha256(path.read_bytes()).digest())
    return key.hexdigest()


def restore(cache, bundle, key):
    if not cache.exists():
        return False
    try:
        if (cache / 'inputs.sha256').read_text().strip() != key:
            return False
        verify(cache / 'seriald')
    except (OSError, ValueError):
        return False
    if bundle.exists():
        shutil.rmtree(bundle)
    shutil.copytree(cache / 'seriald', bundle, ignore=shutil.ignore_patterns('bundle-manifest.json'))
    print('Reusing verified frozen Windows runtime.')
    return True


def save(cache, bundle, key):
    temporary = cache.with_name(cache.name + '.new')
    if temporary.exists():
        shutil.rmtree(temporary)
    shutil.copytree(bundle, temporary / 'seriald')
    create(temporary / 'seriald')
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
