"""Run the packaging regression modules in one Python interpreter."""
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
    sys.exit(not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful())
