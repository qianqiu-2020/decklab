# Windows native dependency attribution

This is an incomplete release review, not a license clearance or native SBOM.
The [wheel observation](windows-wheel-observation.json) records exact downloaded
wheel hashes, native-member hashes and available notice hashes from 2026-10-12.
The source wheel observations are separate from the actual frozen checkpoint below.

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

## Actual frozen checkpoint

The [successful Windows run](https://github.com/qianqiu-2020/decklab/actions/runs/38195810414)
built source `c45fb8484db7b64d304e3a263a922472dc03efc4` on CPython 3.12.10.
Its downloaded ZIP verifies all 205 manifest entries and all 16 distribution
notice sets; the Setup EXE matches its published SHA-256. ZIP member separators
were normalized for Linux inspection without changing file bytes.

Four unchanged frozen extensions match the reviewed wheel members by SHA-256:
bcrypt 5.0.0, cffi 2.1.1, cryptography 50.0.2 and PyNaCl 1.6.2. Exact matches and
provider observations are recorded under `frozen_observation` in the linked JSON.
The Python runtime `libcrypto-3.dll` contains an OpenSSL 3.0.16 version string;
cryptography's `_rust.pyd` contains OpenSSL 4.0.3. These are distinct providers
and require corresponding attribution; host Linux OpenSSL is irrelevant.
Python's collected license includes Microsoft redistributable terms and libffi
and bzip2 notices. This does not close embedded OpenSSL, Rust dependencies or
all other runtime-library attribution gaps. No native license clearance is
claimed from an integrity check or a version string.

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

The offline notice collector now inspects the frozen `libcrypto-3.dll` and
cryptography `_rust.pyd` before publication. It requires one unambiguous OpenSSL
version per provider and currently supports the observed 3.0.16 and 4.0.3.
Matching upstream tag licenses are pinned under `packaging/third-party/` and
checked against their SHA-256 before copying. Missing, ambiguous or newly
unreviewed versions fail packaging with the provider path; review the new
upstream version and extend the catalog rather than bypassing the check.
`inventory.json` links each provider's binary hash to its copied license and
upstream URL. Installer verification checks both binary and license hashes.
Tests cover atomic failure, changed providers/notices and a shared version using
one license with two provider records. This addresses those OpenSSL license
copies; it does not establish all native components' source/notice requirements.

The collector also copies hash-pinned Expat, zlib and XZ source notices from the
reviewed CPython 3.12.10 baseline. Its upstream
[Windows dependency configuration](https://github.com/python/cpython/blob/v3.12.10/PCbuild/python.props)
selects zlib 1.3.1 and XZ 5.2.5; Expat's notice comes from that CPython source tag.
`runtime_source_notices` identifies this baseline explicitly and does not infer
the same versions for other interpreters. Missing or modified reference texts
fail collection atomically; installer checks verify the copied hashes. The XZ
notice distinguishes liblzma from its separately licensed tools and scripts;
copying it does not claim those tools ship. Python's existing license retains
the runtime's libffi, bzip2 and Microsoft terms. Remaining binary/source matching
and compiled Rust dependency review still require evidence.

## Reviewed Windows source graph

The corresponding bcrypt 5.0.0 and cryptography 50.0.2 source archives were
verified against PyPI's published SHA-256. `cargo metadata --locked` with
`--filter-platform x86_64-pc-windows-msvc` resolved 37 and 31 registry packages,
67 unique package/version pairs. Every modeled Windows node has copied notice
texts: 132 unchanged files. Two source-lock crates without notice files,
`r-efi` and `wasip2`, are absent from both Windows resolve graphs. The model
includes build-time dependencies and does not attest upstream wheel feature flags.

Eight leading comment headers from CFFI 2.1.1's bundled x86/x64 libffi C/header
files are also retained unchanged, including their original copyright and
permission terms. Archive/member/header hashes preserve extraction provenance.
The offline catalog under `packaging/third-party/native-source-notices/` records
crate archive checksums, original member paths, metadata license expressions,
source parents, notice hashes and the three reviewed frozen provider hashes.
Those providers must match before the collector copies supplements into
`third-party/native-source/`; a different wheel binary requires review and a
catalog update. No crate build scripts were run during this source inspection.

The compiler commits embedded in bcrypt and cryptography match official Rust
1.90.0 and 1.98.1 channel metadata respectively. Official Windows compiler
archives were checked against their channel SHA-256; their MIT/Apache notices,
copyright statement and generated `COPYRIGHT-library.html` are retained.
The library-only summaries cover source contributors and dependency notices for
the observed standard-library releases; full compiler binaries are not shipped.
The catalog records commit/provider associations, archive URLs/checksums and
each original notice member/hash.

Paramiko's unmodified 4.0.0 source archive is included with its hash and license.
All 44 Python modules match the reviewed wheel byte-for-byte. The builder can
install a supplied source archive before freezing and retain that same archive
in the bundle; see [replacement/rebuild guide](WINDOWS_REBUILD.md). CI exercises
this path with the reviewed upstream archive. Modified sources retain their own
hash/version and are not labeled unmodified upstream unless bytes match exactly.

Native packaging and final release inspection still need to verify these newest
additions. The already published 1.5.0 draft predates them and must not be
described as containing them. Attribution remains a reviewed source coverage
superset, not an assertion of exactly activated upstream wheel build features.

The existing notice collector remains offline and validates copied notice hashes.
The complete bundle manifest checks distributed file integrity. Neither check
proves embedded dependency attribution. See [release acceptance](RELEASE_ACCEPTANCE.md)
and the [third-party summary](../THIRD_PARTY.md).
