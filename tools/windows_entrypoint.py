"""Launch the application bundled beside the frozen Windows interpreter."""
import sys


sys.dont_write_bytecode = True

import decklab_windows


if __name__ == '__main__':
    raise SystemExit(decklab_windows.main())
