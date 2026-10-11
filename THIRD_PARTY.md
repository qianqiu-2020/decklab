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

Windows build dependencies include transitive packages. The release build must
collect their license files and version inventory, rather than treating this
short table as a complete binary-distribution inventory.
