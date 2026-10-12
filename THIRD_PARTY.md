# Third-party components

DeckLab's Apache-2.0 license applies to application code and original branding.
It does not replace the licenses of the following components.

| Component | Files / distribution | License and attribution |
| --- | --- | --- |
| xterm.js and fit addon | `vendor/xterm.js`, `vendor/xterm.css`, `vendor/addon-fit.js` | MIT; xterm.js authors, Christopher Jeffrey. Full notice retained in `vendor/xterm.css`. Upstream: https://github.com/xtermjs/xterm.js |
| Tabler Icons | Selected SVG symbols in `static/icons.svg` | MIT; see `static/icons-LICENSE.txt`. Upstream: https://github.com/tabler/tabler-icons |
| Inno Setup Chinese translation | `packaging/inno-languages/` | Upstream license and translator credits retained alongside the resource. |
| Inno Setup | Windows installer build tool | https://github.com/jrsoftware/issrc; used to generate installers, not part of the Python service. |
| PyInstaller bootloader | Windows executable | GPL with bootloader exception; https://pyinstaller.org/en/stable/license.html |
| pySerial | Windows executable dependency | BSD-3-Clause; https://github.com/pyserial/pyserial |
| Paramiko | Windows executable dependency | LGPL-2.1; https://github.com/paramiko/paramiko |

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
[rebuild guide](docs/WINDOWS_REBUILD.md) explains replacing that library before
freezing and preserving the supplied source in the resulting package.

This inventory does not replace review of native libraries embedded in Python
or dependency wheels (for example crypto libraries), PyInstaller's frozen output,
or bundled frontend assets. Such binary attribution remains a release gate;
this table and Python metadata alone are not a complete native-library SBOM.
The [native dependency review](docs/WINDOWS_NATIVE_NOTICES.md) records an initial
Windows-wheel observation with exact hashes and component-specific unresolved
work. It does not replace review of the actual frozen Windows output.

Windows distributions also include `bundle-manifest.json`: relative paths,
sizes and SHA-256 for every distribution file, with EXE/DLL/PYD files marked
`native` to aid frozen-output review. This is the actual file inventory, separate
from the installed-Python distribution inventory. It does not identify embedded
libraries, determine licenses or authenticate the publisher; those still need
review and trusted release checksums/signatures.
