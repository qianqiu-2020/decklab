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

This inventory does not replace review of native libraries embedded in Python
or dependency wheels (for example crypto libraries), PyInstaller's frozen output,
or bundled frontend assets. Such binary attribution remains a release gate;
this table and Python metadata alone are not a complete native-library SBOM.

Windows distributions also include `bundle-manifest.json`: relative paths,
sizes and SHA-256 for every distribution file, with EXE/DLL/PYD files marked
`native` to aid frozen-output review. This is the actual file inventory, separate
from the installed-Python distribution inventory. It does not identify embedded
libraries, determine licenses or authenticate the publisher; those still need
review and trusted release checksums/signatures.
