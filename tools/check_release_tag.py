#!/usr/bin/env python3
"""Reject a release tag which does not describe the checked-out package."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from seriald_core.version import __version__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tag')
    args = parser.parse_args()
    expected = 'v' + __version__
    if args.tag != expected:
        parser.error('tag must match package version: ' + expected)
    print('Validated release ' + args.tag)


if __name__ == '__main__':
    main()
