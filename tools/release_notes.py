"""Extract the checked-out version's release notes from CHANGELOG.md."""
import argparse
from datetime import date
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from decklab_core.version import __version__


RELEASE_HEADING = re.compile(r'^\[([^\]\r\n]+)\] - (\d{4}-\d{2}-\d{2})$')
REFERENCE_LINK = re.compile(r'^\[[^\]\n]+\]:[ \t]+https?://\S+[ \t]*$', re.MULTILINE)


def extract(text, version):
    matches = list(re.finditer(r'^## (.+)$', text, re.MULTILINE))
    selected = []
    for index, heading in enumerate(matches):
        release = RELEASE_HEADING.fullmatch(heading[1])
        if release and release[1] == version:
            date.fromisoformat(release[2])
            selected.append(index)
    if len(selected) != 1:
        raise ValueError('changelog must contain exactly one section for ' + version)
    index = selected[0]
    end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
    body = REFERENCE_LINK.split(text[matches[index].end():end], maxsplit=1)[0].strip()
    if not body:
        raise ValueError('release notes are empty for ' + version)
    return body + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    try:
        notes = extract((ROOT / 'CHANGELOG.md').read_text(encoding='utf-8'), __version__)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + '\n')
    args.output.write_text(notes, encoding='utf-8')


if __name__ == '__main__':
    main()
