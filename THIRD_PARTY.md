# Third-party components

DeckLab's Apache-2.0 license applies to application code and original branding.
It does not replace the licenses of the following components.

| Component | Files / distribution | License and attribution |
| --- | --- | --- |
| xterm.js and fit addon | `vendor/xterm.js`, `vendor/xterm.css`, `vendor/addon-fit.js` | MIT; xterm.js authors, Christopher Jeffrey. Full notice retained in `vendor/xterm.css`. The browser viewport skips queued scroll callbacks after disposal. Upstream: https://github.com/xtermjs/xterm.js |
| xterm headless 5.5.0 and serialize addon 0.13.0 | `vendor/terminal/` | MIT; exact upstream notices and package integrity in the adjacent LICENSE/source files. Same xterm version as the browser. |
| Ace 1.44.0 | `vendor/ace/` | BSD-3-Clause; unmodified no-conflict browser build with JSON and YAML modes. Full license and pinned npm archive and file digests are adjacent. Upstream: https://github.com/ajaxorg/ace-builds |
| noVNC 1.7.0 Display and Keyboard | `vendor/novnc/` | MPL-2.0; unmodified entry points and their complete relative ES-module dependency closure. Original notices: `LICENSE.txt`, `docs/LICENSE.MPL-2.0`, `AUTHORS`. Fixed upstream commit, archive and individual file hashes: `vendor/novnc/source.json`. Upstream: https://github.com/novnc/noVNC |
| wsproto 1.3.2 | `vendor/python/wsproto-1.3.2-py3-none-any.whl` | MIT; the official wheel is unchanged. Original notice is retained in the wheel and copied to `vendor/python/wsproto-LICENSE`. Upstream: https://github.com/python-hyper/wsproto |
| h11 0.16.0 | `vendor/python/h11-0.16.0-py3-none-any.whl` | MIT; the official wheel is unchanged. Original notice is retained in the wheel and copied to `vendor/python/h11-LICENSE`. Upstream: https://github.com/python-hyper/h11 |
| Node.js | System package on Ubuntu; `_internal/runtime/` in Windows bundles | Windows Node 22.23.3 archive pinned by SHA-256; original LICENSE includes upstream dependency notices. Source and executable/license digests are in `runtime/source.json`. https://nodejs.org/ |
| Tabler Icons | Selected SVG symbols in `static/icons.svg` | MIT; see `static/icons-LICENSE.txt`. Upstream: https://github.com/tabler/tabler-icons |
| Inno Setup Chinese translation | `packaging/inno-languages/` | Upstream license and translator credits retained alongside the resource. |
| Inno Setup | Windows installer build tool | https://github.com/jrsoftware/issrc; used to generate installers, not part of the Python service. |
| PyInstaller bootloader | Windows executable | GPL with bootloader exception; https://pyinstaller.org/en/stable/license.html |
| pySerial | Windows executable dependency | BSD-3-Clause; https://github.com/pyserial/pyserial |
| Paramiko | Windows executable dependency | LGPL-2.1; https://github.com/paramiko/paramiko |
| FreeRDP 3.33.1 and WinPR | Private `desktop-runtime/freerdp/` libraries | Apache-2.0; unmodified protocol core. Upstream: https://github.com/FreeRDP/FreeRDP |
| OpenSSL 3.5.9 | Statically linked into the private FreeRDP runtime | Apache-2.0; https://github.com/openssl/openssl |
| OpenH264 2.6.0 | Statically linked into the private FreeRDP runtime | BSD-2-Clause; original license and notices retained. https://github.com/cisco/openh264 |

Windows bundles include `third-party/inventory.json`, exact installed dependency
versions, copied license/notice files and their SHA-256 digests. Collection runs
in the packaging interpreter and includes all installed distributions, including
build-only tools; it is a conservative build-environment inventory, not an exact
list of frozen modules. Python's license is included separately. Installer builds
verify the copied notice hashes and reject missing or incomplete inventories.

Some distributions omit license files. The version-specific pySerial 3.5 notice
is retained in `packaging/third-party/pyserial-3.5/`, copied unchanged from
https://github.com/pyserial/pyserial/blob/v3.5/LICENSE.txt. Missing notices for
other versions/components stop the build; the collector never fetches arbitrary
licenses during packaging.

Frozen OpenSSL providers are inspected during Windows packaging. Their observed
versions select pinned upstream licenses, and the inventory records provider and
license hashes. The reviewed CPython 3.12.10 source baseline also contributes
Expat, zlib 1.3.1 and XZ 5.2.5 source notices under `third-party/runtime-source/`.
These supplemental source notices are explicitly separate from binary provider
identification; they do not identify every component in an arbitrary Python build.
The native source catalog also retains modeled Rust dependency notices, CFFI's
libffi source headers and observed Rust standard-library release declarations.
Windows packages include Paramiko's corresponding source archive; the
[rebuild guide](docs/development/windows-rebuild.md) explains replacing that library before
freezing and preserving the supplied source in the resulting package.

This inventory does not replace review of native libraries embedded in Python
or dependency wheels (for example crypto libraries), PyInstaller's frozen output,
or bundled frontend assets. Such binary attribution remains a release gate;
this table and Python metadata alone are not a complete native-library SBOM.
The [native dependency review](docs/development/windows-native-notices.md) records an initial
Windows-wheel observation with exact hashes and component-specific unresolved
work. It does not replace review of the actual frozen Windows output.

Windows distributions also include `bundle-manifest.json`: relative paths,
sizes and SHA-256 for every distribution file, with EXE/DLL/PYD files marked
`native` to aid frozen-output review. This is the actual file inventory, separate
from the installed-Python distribution inventory. It does not identify embedded
libraries, determine licenses or authenticate the publisher; those still need
review and trusted release checksums/signatures.

The Python distribution inventory excludes the separate Node executable.
The complete bundle manifest covers that executable, its license and provenance.
The runtime does not open network or serial connections; it parses streams supplied by the
collector. `tools/prepare_terminal_runtime.py` verifies the official archive before
extracting only node.exe and LICENSE. `tools/prepare_terminal_assets.py` reproduces
the two headless assets from integrity-pinned npm packages using esbuild 0.25.11,
lowering syntax to Ubuntu 22.04's Node 12. It does not alter terminal behavior.
Upstream asset hashes, transformed hashes and compiler options are recorded;
ordinary package builds use the committed assets without running npm.

## Continuous desktop assets and protocol IO

The noVNC selection contains Display, Keyboard and their required source modules;
it does not include the noVNC application, RFB client or a browser RDP client.
VNC/RDP negotiation, authentication and decoding remain in the server-side
vncdotool/aardwolf adapters or the optional FreeRDP native engine. noVNC's own license and original source headers
remain applicable; DeckLab does not relabel these files as Apache-2.0.

The FreeRDP engine uses a small DeckLab C ABI inside the existing isolated
desktop worker. FreeRDP performs its own graphics-pipeline, RDSTLS and server
redirection handling; its protocol sources are not patched. Reviewed upstream
archive versions, URLs and SHA-256 digests are in
`packaging/third-party/freerdp-sources.json`. Each prepared native runtime binds
those inputs and the exact DeckLab bridge sources to an architecture-specific
`runtime-manifest.json`, complete file hashes and copied component notices.
Package builders verify it offline before freezing. Linux uses the distribution's
standard C/C++ runtime dependencies; Windows includes the required private native
closure and its toolchain notices. No GUI toolkit or control-machine clipboard is
used by this engine.

The noVNC 1.7.0 archive is fixed to commit
`63107bd06d9e1f6136ff21aeda8cd62cbf0d433e`, downloaded from
https://codeload.github.com/novnc/noVNC/tar.gz/63107bd06d9e1f6136ff21aeda8cd62cbf0d433e,
with SHA-256
`06dac239be3973ed79a00e357a68aab996e6ed2fd496ee2cc6e6a34020344ac9`.
The selected files are source code and are shipped without transformation.

The WebSocket IO layer loads the following original pure-Python wheels directly
with Python's ZIP importer. It needs neither pip nor network access on the
installed control/collector host, and it does not use the separate MCP worker
to load these dependencies.

| Official PyPI wheel | SHA-256 |
| --- | --- |
| https://pypi.org/project/wsproto/1.3.2/#files | `61eea322cdf56e8cc904bd3ad7573359a242ba65688716b0710a5eb12beab584` |
| https://pypi.org/project/h11/0.16.0/#files | `63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86` |

Exact wheel download URLs, versions and license locations are recorded in
`vendor/python/websocket-sources.json`; the loader verifies the same fixed
wheel hashes before import. These are vendored application resources, separate
from the MCP native-dependency inventory and from the Windows packaging
interpreter's installed-distribution inventory. The existing Linux vendor copy
and Windows data inclusion ship the source manifests, notices and runtime files
unchanged. Linux places them under `/usr/lib/decklab/vendor/`; Windows bundles
place them under `_internal/vendor/`. Windows `bundle-manifest.json` inventories
all these actual resource files and rejects later changes.

## Binary flashing component

The goflash flashing executor is a separate, non-open-source component. This
release includes its executable under separate redistribution authorization; no
goflash source is included. DeckLab's Apache-2.0 license does not apply to that
component or grant additional rights to its source or binaries. Its component
notices and dependency licenses remain in the distribution.

The K1 and K3 flash and RAM startup loader directories contain their build records,
the U-Boot GPL-2.0 license in `flash-loaders/COPYING.txt`, and written offers in
`SOURCE-OFFER.txt` for the exact corresponding U-Boot and SPL sources. Any third
party may request those sources free of charge through DeckLab Issues for at least
three years after distribution of their copy. OpenSBI and the USB driver retain
their own license and attribution.
