"""Serial board service: hardware, persistence, and HTTP boundaries."""

from .compat import adopt_environment
from .version import __version__

adopt_environment()

__all__ = ['__version__']
