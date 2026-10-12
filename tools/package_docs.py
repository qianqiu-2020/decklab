"""Publish selected release documentation with links matching installed layout."""
import argparse
import os
from pathlib import Path
import re
import shutil
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
GUIDES = ('BRAND', 'OPEN_SOURCE_PLAN', 'RELEASE_ACCEPTANCE', 'RELEASING', 'AGENT_ACCESS', 'API', 'ARCHITECTURE', 'INSTALL',
          'HARDWARE_ID', 'DEVICE_IDENTITY_MATRIX', 'CONFIGURATION', 'DEMO',
          'CONSOLE_TRANSACTIONS', 'STORAGE_UPGRADES', 'LOG_OPERATIONS', 'TASKS',
          'TRANSFER_ARTIFACTS', 'HARDWARE_EXTENSIONS', 'REFACTOR_PLAN', 'WINDOWS',
          'TRANSFER_PERFORMANCE', 'WINDOWS_NATIVE_NOTICES', 'WINDOWS_REBUILD')
LINK = re.compile(r'(!?\[[^\]\n]*\]\()([^\s)]+)(\))')


def document_map(platform):
    if platform not in ('linux', 'windows'):
        raise ValueError('unsupported documentation layout')
    pairs = {'README.md': 'README.md', 'README.zh-CN.md': 'README.zh-CN.md',
             'LICENSE': 'copyright' if platform == 'linux' else 'LICENSE',
             'NOTICE': 'NOTICE', 'THIRD_PARTY.md': 'THIRD_PARTY.md',
             'CONTRIBUTING.md': 'CONTRIBUTING.md', 'SECURITY.md': 'SECURITY.md',
             'CHANGELOG.md': 'CHANGELOG.md'}
    prefix = '' if platform == 'linux' else 'docs/'
    pairs.update({'docs/' + name + '.md': prefix + name + '.md' for name in GUIDES})
    for name in ('seriald-architecture.png', 'seriald-architecture.svg',
                 'seriald-architecture-en.png', 'seriald-architecture-en.svg',
                 'decklab-demo.png'):
        pairs['docs/' + name] = prefix + name
    pairs['docs/windows-wheel-observation.json'] = prefix + 'windows-wheel-observation.json'
    for path in ('examples/profiles.json', 'examples/hardware.json', 'skills/seriald-board/SKILL.md'):
        pairs[path] = path
    if platform == 'windows':
        pairs['README.md'] = 'README-project.md'
    return pairs


def render(source, target, mapping):
    text = source.read_text(encoding='utf-8')
    def replace(match):
        url = urlsplit(match[2])
        if url.scheme or url.netloc or not url.path or url.path.startswith('/'):
            return match[0]
        original = (source.parent / unquote(url.path)).resolve()
        destination = mapping.get(original)
        if destination is None:
            raise ValueError('unpackaged document link in %s: %s' % (source.name, match[2]))
        relative = quote(Path(os.path.relpath(destination, target.parent)).as_posix(), safe='/._-')
        if url.query:
            relative += '?' + url.query
        if url.fragment:
            relative += '#' + url.fragment
        return match[1] + relative + match[3]
    return LINK.sub(replace, text)


def publish(destination, platform):
    destination = Path(destination)
    if (destination / 'data').exists():
        raise ValueError('refusing to publish documentation into a runtime installation')
    pairs = document_map(platform)
    mapping = {(ROOT / source).resolve(): destination / target for source, target in pairs.items()}
    outputs = [(ROOT / source, destination / target) for source, target in pairs.items()]
    if platform == 'windows':
        outputs.append((ROOT / 'docs/WINDOWS.md', destination / 'README.md'))
    if any(not source.is_file() for source, _ in outputs):
        raise ValueError('release documentation source is missing')
    # Validate all links before changing any documentation files.
    prepared = [(source, target, render(source, target, mapping) if source.suffix == '.md' else None)
                for source, target in outputs]
    for source, target, text in prepared:
        target.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            shutil.copyfile(source, target)
        else:
            target.write_text(text, encoding='utf-8')
        target.chmod(0o644)
    return [target for _, target in outputs]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--platform', required=True, choices=('linux', 'windows'))
    args = parser.parse_args()
    publish(args.destination, args.platform)
