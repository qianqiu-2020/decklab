#!/usr/bin/env python3
"""Prepare a temporary RISC-V boot payload from a stock SpacemiT OS archive."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from seriald_core.ramboot_prepare import main

if __name__ == '__main__':
    sys.exit(main())
