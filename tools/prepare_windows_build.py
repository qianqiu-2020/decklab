"""Create a clean packaging interpreter without touching development environments."""
from pathlib import Path
import sys
import venv


def prepare(root):
    root = Path(root).resolve()
    build = root / 'build'
    environment = build / 'windows-venv'
    # Do not follow a user-created junction/symlink when clearing the build env.
    for path in (build, environment):
        if path.exists() and path.resolve() != path.absolute():
            raise ValueError('Build environment must not be a symlink or junction: %s' % path)
        if path.is_symlink():
            raise ValueError('Build environment must not be a symlink: %s' % path)
    venv.EnvBuilder(clear=True, with_pip=True, system_site_packages=False).create(environment)
    return environment


if __name__ == '__main__':
    prepare(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1])
