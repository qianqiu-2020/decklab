"""Stage current Web resources and cache the frozen Windows Python runtime."""
import argparse
import ast
import hashlib
from pathlib import Path
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
APPLICATION_FILES = ('decklab_windows.py', 'decklab_demo.py', 'decklab_mcp.py', 'decklabctl.py')


def application_sources():
    return sorted(ROOT / name for name in APPLICATION_FILES) + sorted((ROOT / 'decklab_core').rglob('*.py'))


def import_signature(path):
    """Reanalyze dependencies when any project module's imports change."""
    tree = ast.parse(path.read_bytes(), filename=str(path))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(ast.dump(node))
        if isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id in ('__import__', 'import_module'))
            or (isinstance(node.func, ast.Attribute) and node.func.attr == 'import_module')
        ):
            if not node.args or not isinstance(node.args[0], ast.Constant):
                return path.read_bytes()
            imports.append(ast.dump(node))
    return '\n'.join(sorted(imports)).encode()


def runtime_key(dependencies, desktop):
    inputs = [ROOT / 'packaging/decklab.ico', ROOT / 'tools/build_windows.ps1',
              ROOT / 'tools/windows_entrypoint.py', ROOT / 'tools/windows_hooks/hook-decklab_windows.py',
              Path(__file__)]
    key = hashlib.sha256(dependencies.encode())
    if desktop:
        key.update(hashlib.sha256((desktop / 'runtime-manifest.json').read_bytes()).digest())
    for path, data in ([(path, path.read_bytes()) for path in inputs] +
                       [(path, import_signature(path)) for path in application_sources()]):
        relative = path.relative_to(ROOT).as_posix()
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
    resources = dict(STATIC_FILES)
    for path in application_sources():
        name = path.relative_to(ROOT).as_posix()
        resources[name] = name
    for source, target in resources.items():
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
