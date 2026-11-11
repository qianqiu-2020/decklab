# Linux MCP worker

The Debian installer carries a private MCP (Model Context Protocol) worker with
its Python interpreter, SDK and native extensions. Installing or starting the
package does not run pip or require a client-side Python installation. The
collector and HTTP control service continue to use the distribution's standard
library Python. The worker only accesses the authenticated HTTP API; its entry
point never imports the serial collector.

Build a prepared runtime on an amd64 or arm64 Docker host:

```sh
python3 tools/build_linux_mcp.py --arch amd64 --output build/linux-mcp/amd64/runtime
python3 tools/build_linux_mcp.py --arch arm64 --output build/linux-mcp/arm64/runtime
```

Build each architecture on its native runner. The default command creates an
Ubuntu 22.04 container, downloads hash-locked build inputs and freezes a
`seriald-mcp-worker` directory. Cross-architecture Docker execution additionally
needs host emulation; it is not required by the native CI jobs. Container network
access is needed during the build, and is not needed by package installation.
The output directory must not already exist.

For a prepared native Ubuntu 22.04 build runner:

```sh
sudo apt-get install python3.10-venv python3.10-dev binutils ca-certificates
/usr/bin/python3.10 -m venv build/mcp-env
build/mcp-env/bin/python -m pip download --only-binary=:all: --require-hashes \
  -r packaging/linux-mcp-requirements.lock -d build/mcp-wheels
build/mcp-env/bin/python -m pip install --no-index --find-links build/mcp-wheels \
  --only-binary=:all: --require-hashes -r packaging/linux-mcp-requirements.lock
build/mcp-env/bin/python tools/build_linux_mcp.py --local --arch amd64 \
  --wheelhouse build/mcp-wheels --output build/linux-mcp/amd64/runtime
```

`--local` requires Ubuntu 22.04, CPython 3.10 and glibc 2.35. PyInstaller does
not bundle glibc, so freezing on a newer distribution would invalidate the
Ubuntu 22.04 compatibility baseline. See the
[PyInstaller Linux compatibility guidance](https://pyinstaller.org/en/stable/usage.html#making-gnu-linux-apps-forward-compatible).

`runtime-manifest.json` records the architecture, ELF machine, baseline,
Python/SDK versions, adapter source hashes, requirements hash and every shipped
file's size and SHA-256. The manifest excludes itself. Internal library symlinks
are materialized before hashing. Verify an existing output offline with:

```sh
python3 tools/build_linux_mcp.py --verify --arch amd64 \
  --output build/linux-mcp/amd64/runtime
```

`third-party/inventory.json` records Python distribution notices and actual
Linux native providers. The build wheelhouse and the source artifact catalog
connect native extensions to pinned upstream wheel/source hashes. System
libraries are attributed using their build distribution's package copyright
files. Rust source notices are a conservative source dependency set, rather
than a claim that every source package is present in the executable. Missing
licenses, source graph entries or unmatched native binaries fail the build.

The reviewed prebuilt runtime architectures are amd64 and arm64. A RISC-V
source installation remains a separate supported development path. The builder
reserves native `--local --arch riscv64` for extending the reviewed inputs and
validating a native PyInstaller bootloader; it is not a claim that a prebuilt
RISC-V worker has been delivered. Adding an architecture requires its wheel
hashes, source notices and clean installation verification before release.
