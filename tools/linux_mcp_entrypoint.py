"""Frozen Linux MCP worker entry point; never imports the serial collector."""
import sys

from seriald_mcp import main


if __name__ == '__main__':
    sys.exit(main())
