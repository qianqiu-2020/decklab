# Windows native dependency attribution

This is an incomplete release review, not a license clearance or native SBOM.
The [wheel observation](windows-wheel-observation.json) records exact downloaded
wheel hashes, native-member hashes and available notice hashes from 2026-10-12.
These are source artifacts, not an executed or frozen Windows build.

## Evidence and limitations

The audit downloaded compatible Windows x64 wheels on Linux with:

```sh
python3 -m pip download --dest /tmp/decklab-windows-license-audit \
  --platform win_amd64 --python-version 3.12 --implementation cp --abi cp312 \
  --only-binary=:all: -r requirements-windows.txt
```

Cross-platform downloading selects compatible wheel tags but does not establish
Windows environment-marker resolution. The resulting set must not be used as a
complete Windows dependency lock. The JSON records only native-bearing wheels;
build-only executables are included as observations, without claiming they ship.
Dependency ranges can resolve different versions on the next build.

| Observed wheel | Native members | Available notice evidence / unresolved work |
| --- | --- | --- |
| cryptography 50.0.2 | `cryptography/hazmat/bindings/_rust.pyd` | Package LICENSE, Apache and BSD texts; no separate OpenSSL notice found. Determine the embedded provider/version and compiled dependencies, then include their matching notices. |
| PyNaCl 1.6.2 | `nacl/_sodium.pyd` | Package LICENSE and `LICENSE.libsodium.txt` present; confirm actual frozen member and embedded library version. |
| cffi 2.1.1 | `_cffi_backend.cp312-win_amd64.pyd` | Package LICENSE present; establish any embedded libffi attribution from this wheel's build sources. |
| bcrypt 5.0.0 | `bcrypt/_bcrypt.pyd` | Package LICENSE present; inspect compiled dependency attribution rather than treating Python metadata as complete. |
| PyInstaller 6.22.3 | Four Windows bootloader EXEs | COPYING.txt present; final application EXE modifies/embeds the bootloader and will not equal its source hash. Review bootloader and embedded dependency notices. |
| setuptools 84.0.0 | Eight CLI/GUI launcher EXEs | Package and vendored notices present; build-environment presence alone does not mean launchers ship. |

Cryptography documents that its Windows wheels are statically linked; finding
only package-level license texts does not inventory every embedded component.
See the [upstream installation documentation](https://cryptography.io/en/latest/installation/).
PyInstaller's terms include a bootloader exception; review the exact build
version's [COPYING.txt](https://github.com/pyinstaller/pyinstaller/blob/v6.22.3/COPYING.txt).
No assumption about an embedded library's exact version is derived from these
general documents.

## Finish against an actual Windows bundle

1. Build in the dedicated Windows environment and retain its exact distribution
   inventory, build log, source wheel filenames/hashes and final bundle manifest.
2. Match unchanged PYD/DLL members against their source wheels by content hash.
   Record transformed executables and unmatched binaries separately; a matching
   basename is insufficient attribution evidence.
3. Review Python runtime DLLs, its own crypto libraries, Windows runtimes and
   bootloader dependencies from their actual providers/build records. The wheel
   observation does not cover the installed Python runtime.
4. Determine embedded dependency versions and corresponding upstream notices.
   Keep exact source/version provenance with copied notices; do not select an
   unrelated current license merely because its filename matches.
5. Review the resulting Setup/ZIP files and verify all notices survive packaging.
   Mark unresolved components explicitly and update the release acceptance audit.

The existing notice collector remains offline and validates copied notice hashes.
The complete bundle manifest checks distributed file integrity. Neither check
proves embedded dependency attribution. See [release acceptance](RELEASE_ACCEPTANCE.md)
and the [third-party summary](../THIRD_PARTY.md).
