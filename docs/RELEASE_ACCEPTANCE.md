# Release acceptance audit

Audit candidate: source version 1.5.0, reviewed 2026-10-12. This separates
implemented behavior from platform/release evidence. Local tests use temporary
storage, simulated boards and fake transports; they do not prove real COM or
installed Windows behavior. Earlier checkpoint counts are historical.
The tagged checkpoint below builds 1.5.0; older platform checkpoints built
1.4.50. Outputs remain draft review artifacts while native attribution work
continues.

## Requirement evidence

| Requirement | Implementation and focused evidence | Current assessment |
| --- | --- | --- |
| Public project | LICENSE, NOTICE, THIRD_PARTY.md, bilingual README, CONTRIBUTING.md, SECURITY.md and issue/PR templates; package tests | Present; native binary attribution incomplete |
| Releases | CI, Windows package and tagged release workflows; version-tag validator; Debian extraction fixtures | Tagged Linux/Windows/publish jobs pass; downloaded draft packages/checksums/notes verified |
| Configurable hosts/boards | profiles.py, transport.py, windows_manager.py; test_profiles.py validates framing, profiles, manual mode and credentials | Local contracts verified |
| Hardware-free demo | demo.py, seriald_demo.py; test_demo.py and demo_smoke.js exercise normal HTTP/CLI/browser paths | Linux demo/browser checks and frozen Windows API/archive smoke pass |
| Board/connection/session separation | models.py, devices.py, session_base.py; model, registry and session-contract tests | Local reconnect and migration contracts verified |
| Console coordination | transactions.py, console_protocol.py; transaction, protocol, handoff and input-ownership tests | Local timeout/cancel/generation contracts verified |
| Platform adapters | sessions.py, sessions_windows.py, windows_manager.py; shared session and fake Windows tests | Linux fixtures and native Windows selected suites pass |
| Application services/API | actions.py, queries.py, results.py, API.md; action/query/HTTP/CLI tests | Implemented; 54-route source/contract content review complete locally |
| Frontend ownership | devices.js, terminal-view.js, archives.js, terminal-navigation.js; frontend.test.js | Isolation checks verified; desktop/mobile browser fixture passes five consecutive runs after synchronization fix |
| Schema upgrades | schema.py registry migrations through version 5; test_schema.py checks WAL backups, rollback, concurrent openers and future versions | Linux and native Windows upgrade/backup fixtures pass |
| Collaboration | transactions.py, agent_grants.py, control_server.py; input-ownership, grants and sharing tests | Local reservation/revocation/takeover verified |
| Log operations | maintenance.py; maintenance/archive/identity tests cover active writers, cross-day boots, disk warnings and allowlisted diagnostics | Linux and native Windows maintenance fixtures pass |
| Long operations | tasks.py, task_actions.py, transfer_tasks.py, transfer_artifacts.py; task/file/CLI tests and demo browser upload/download/copy/reload | Local task and artifact workflows verified |
| Hardware extensions | hardware.py, process_windows.py, exclusive_serial handoff; helper and handoff tests | Linux/fake contracts and native Windows Job/helper/handoff suites pass |
| Distributed documentation | package_docs.py; test_package_docs.py checks both layouts, examples, assets and UTF-8 links | Content/layout/native install/release reviewed; binary attribution remains outstanding |

## Tagged 1.5.0 checkpoint

Tag `v1.5.0` points to `c62e4763a88c6efe319c00253e76a4935bd51dd5`.
The [tagged workflow](https://github.com/qianqiu-2020/decklab/actions/runs/38197327006)
completed Linux, native Windows and draft publication jobs successfully. The
draft has six assets: Debian package, Setup EXE, portable ZIP, combined checksums,
Linux checksums and the installer checksum. Its release notes exactly match
CHANGELOG's 1.5.0 section. Downloaded assets pass `tools/verify_release.py`:
all three package checksums, Debian package/version/architecture, portable
version, complete 210-file manifest, 16 distribution notice sets, both native
OpenSSL provider/license hashes and three supplemental runtime source notices.
Executables were inspected without running them on the Linux audit host.

| Downloaded artifact | SHA-256 |
| --- | --- |
| `seriald_1.5.0_all.deb` | `503eb923309e0b9de166d556044bce609a2e0bd83ebad3b402a39517e276c1a0` |
| `seriald-1.5.0-windows-x64-setup.exe` | `2d946b7377b00ac6371420f3c68a7680ab1ca500c6866399b111d53cc67287d0` |
| `seriald-windows-portable.zip` | `3109a00a417782da770fc9022a76ee2b7d14b78e0e37409b850556f10eb41c57` |

Repeat on Ubuntu with authenticated access to the draft:

```sh
gh release download v1.5.0 --dir /tmp/decklab-release-review
python3 tools/verify_release.py /tmp/decklab-release-review --version 1.5.0
```

The verifier rejects changed checksum sets, duplicate/path-traversal entries,
Windows case/separator collisions, runtime data in ZIPs, changed manifests or
notices and mismatched package versions. Five synthetic regression fixtures
exercise these boundaries. It also runs before draft publication in subsequent
tagged workflows; this tag was independently inspected after publication.

## Earlier hosted platform checkpoint

Source `c45fb8484db7b64d304e3a263a922472dc03efc4` passed both hosted jobs:
[Linux regression](https://github.com/qianqiu-2020/decklab/actions/runs/38195810408)
and [Windows packages](https://github.com/qianqiu-2020/decklab/actions/runs/38195810414).
Linux ran Python 3.10/3.12/3.13 regression jobs and actual isolated Ubuntu
22.04/24.04 package installations. Windows used CPython 3.12.10 and built both
Setup EXE and portable ZIP. Portable health/device API and frozen demo checks
passed; the demo exposed three boards and two indexed boots. Installer checks
passed portable migration, remembered upgrade directory, modified runtime data
preservation, all three launcher targets/icons and uninstall behavior.

Native Windows suites include CLI UTF-8 output, schema/WAL backup, tasks,
artifacts, file/copy tasks, models, maintenance, grants, profiles, sessions,
console protocol, ownership, Job containment, hardware helpers and reader
handoff. Linux shell-specific fixtures are skipped there; fake serial transports
do not establish physical COM reliability. Job tests actually run on Windows.
The hosted checks uncovered and fixed default-codepage reads, test cleanup
ordering, read-only SQLite backup fsync and CLI pipe encoding. Shortcut inspection
copies identical bytes to an ASCII temporary filename before WScript inspection;
the installed Chinese shortcut names remain unchanged.

These results clear the hosted platform gate for the exercised scopes. They do
not prove tagged release publication, every native dependency's attribution,
physical UART/USB behavior or unattended installation on arbitrary PCs.

The downloaded Windows artifact at that source also passes its complete
205-file bundle manifest, all 16 distribution notice-set hashes and Setup EXE
checksum. Four frozen extensions match the reviewed wheels byte-for-byte.
Distinct embedded OpenSSL providers were observed; the exact evidence and
remaining attribution gaps are in [native review](WINDOWS_NATIVE_NOTICES.md).
Integrity verification alone does not close that release gate.

## Historical local evidence

The preceding code checkpoint passed 511 backend tests (three Windows-native
tests skipped), 74 frontend isolation checks, sharing checks and PowerShell
syntax parsing. The current real demo browser check passes input/SSE, archives,
independent-client takeover, UI copy/upload/download, page reload and a verified
download link. Desktop/mobile fixture smoke initially reproduced a scrollbar-click
failure. Its setup directly changed DOM scroll position, which could conflict
with asynchronous xterm viewport synchronization. The fixture now resets through xterm, waits for rendering,
asserts overflow and native track hit-testing, then requires actual movement within
a bounded wait. Five consecutive complete fixture runs pass; no application
scrollbar behavior was changed. Five package/documentation tests also pass.

## Historical platform and source audits

Actual systemd lifecycle now passes in disposable Ubuntu 22.04 and 24.04 KVM
guests through tools/test_deb_vm.py. Official cloud-image hashes were checked;
private overlays and temporary SSH keys/loopback forwarding shared no physical
devices or host directories. Real installation started both units and served
the API/CLI. Same-fingerprint reinstall reported kept-running and preserved the
collector PID. A temporary comment-only collector change with a recomputed
fingerprint and higher fixture package version reported restarted and changed
the PID. Reinstalling that fixture then preserved the new PID. Modified conffiles,
runtime data and registry survived; purge stopped both units and kept data.
Restart markers were removed after configuration. Both guests exited and their
overlays/keys were removed. The VM launcher rejects an incorrect image checksum
before boot; its final launch/check path passed on 22.04, and the equivalent
systemd checks passed on 24.04 before adding explicit hash/SSH-config arguments.
Image hashes: 22.04 012d81fade7e8ff4428fc35dfeee711d66d1f47d5ea9c5c8ced96a85b959835c;
24.04 6a81c37564db9b1ee84e141922625e1d7c5b389b99bb3c572e0243607d5bb4d2.
Physical UART continuity and privileged USB recovery are not established by an
empty-port VM. Three packaged-document tests and the documentation inventory
check pass. Native Windows and hosted GitHub checks subsequently passed at the checkpoint above.

Ubuntu package acceptance now includes actual isolated installations on 22.04
and 24.04, using tools/test_deb_container.py. Both completed successfully with
dpkg installation and same-version reinstall, modified conffile/data preservation,
root-owned non-group-writable recovery helper, valid sudoers, runtime ownership,
service-account collector/control API/CLI startup, independent control restart
preserving collector PID, and purge preserving the registry/runtime sentinel.
The disposable containers had no host mounts/devices/ports or privileged mode;
each was removed after verification. This does not exercise systemd activation,
collector-restart fingerprint decisions in a running systemd installation, actual
UART or USB recovery. CI now defines both Ubuntu baselines and tagged Linux
releases require the 24.04 check. Hosted execution subsequently passed for both images at the checkpoint above. Container
image IDs observed: 22.04 e8df6c74f650ad385efe2e20103eca9b0ad50690fb3c63d49fa00762412bbf55;
24.04 69f919497cada9a8611f3de61df902d9f94bf20f84552ef5bd93125c38608ef9.
Install documentation now distinguishes control and collector config reloads
and uses the current profile-controlled root password semantics.

Native dependency review now has a concrete source-artifact checkpoint:
Windows x64-compatible wheels were downloaded on Linux for Python 3.12, without
installing or executing their binaries. WINDOWS_NATIVE_NOTICES.md records six
native-bearing wheels and component-specific attribution gaps; the adjacent
JSON records exact wheel/native/notice hashes, all rechecked against downloaded
archives. Cryptography 50.0.2 contains its package license texts but no separate
OpenSSL notice in the inspected wheel; PyNaCl 1.6.2 includes a libsodium notice.
This is not proof of the exact Windows dependency graph: cross-platform pip
resolution does not establish Windows environment-marker behavior, and source
wheel presence does not establish frozen inclusion. Python runtime binaries and
embedded dependencies still need actual-provider evidence. Both documents are
included in package layouts; the three document-layout/link tests pass. The
actual frozen-output attribution gate remains open.

The local API content review now includes task and artifact contracts, completing
the six groups covering all 54 public routes in API.md. TASKS.md and
TRANSFER_ARTIFACTS.md describe start/query response wrappers, opaque IDs,
worker capacity and HTTP 429, actor visibility, history pagination, cancellation
races, exact append-retry rules, retained deleted metadata, unavailable-file
responses and task history outliving artifacts. Artifact timestamps refresh on
stream open, not metadata reads or identical append retries; expiry runs on a
maintenance pass. Quota settings include their integer bounds and zero reserve.
Review sources were task_actions.py, tasks.py, artifact_actions.py,
transfer_artifacts.py, transfer_tasks.py and collector maintenance. The 39
task/file/copy tests, ten task-action tests, 23 artifact tests and three packaged
document tests pass, as does the 34-variable/54-route inventory check. This is
source and local contract evidence, not proof of native Windows behavior or
hosted release execution. Earlier review notes below are checkpoint evidence.

API content review now covers captured-output cursors, archive/history limits,
SSH session actor permissions, low-level transfer payloads and control sharing
payloads against web.py, queries.py, actions.py, file_transfer.py and control_server.py.
API.md documents HTTP-200 operation failures, streaming failure handling, immediate
cursor-zero snapshots, long-poll behavior, command/search limits, distinct transfer
and task IDs, transfer idle expiry, grant creation/list limits and revocation cleanup
confirmation. This review found health aborting on unavailable disks and iterating
mutable SSH registries without locking; health now returns structured unavailable
storage and snapshots sessions under their registry lock. HTTP fixtures verify
both behaviors; all 552 backend tests pass (three native Windows tests skipped),
and package-document fixtures and route/config name checks pass. Final task and
artifact content review and native installation/release gates remain outstanding.

The configuration content review groups all 34 runtime environment names into
host/auth (11), board/serial (13), retention (3), artifacts (6) and helpers (1).
CONFIGURATION.md and its linked operational guides were checked against config,
entrypoints, package environment/launchers and policy constructors for defaults,
validation, reload ownership and platform differences. This found profile
selection rereading environment defaults; ProfileBook now snapshots defaults and
copies its input document at construction. Fixtures verify subsequent invalid
environment values and caller document edits cannot change active profiles;
creating a replacement book applies new settings. Native Windows installation
semantics and the complete API-route review remain separate requirements. Full
backend regression passes 551 tests (three native Windows tests skipped); all
three package-document layout/link fixtures and the 34-variable/54-route
documentation inventory check also pass.

SSH provisioning now writes the selected profile's root password, rather than
hardcoding the development default. Automatic authentication-denial recovery
requires an enabled root profile and credentials matching that profile; different
per-request credentials do not change the board. Explicit provisioning rejects
non-root or empty-password profiles before console interaction. Hardware-free
tests verify both password prompts, no password in generated config commands or
status messages, configured-password retry and override isolation. All 548
backend tests pass (three native Windows tests skipped). Configuration and agent
documentation describe these conditions; real-board policy remains profile-owned.

Collector API imports no longer instantiate a Manager or read board profile and
hardware configuration. Linux, Windows and demo entrypoints inject their manager
into the HTTP server; requests use that instance. Real loopback HTTP fixtures
verify two collectors have separate directories and mutations, a module-level
test fallback cannot override an injected manager, and an unconfigured server
returns 503. A subprocess import with invalid/missing configuration creates no
runtime directory. The full backend suite passes 545 tests (three native Windows
tests skipped); the simulated-host browser verifies input/SSE, archives, takeover,
copy/upload/download and results after reload. Native Windows remains unverified.

Serial archive writing is now a separate `SerialLog` component owning its
decoder, partial lines, file handle/path, write-error state and reentrant lock.
It accepts a directory callback, without a connection, registry or serial reader.
Device retains raw byte fan-out; migration and retention continue to use the
writer's same lock. Standalone fixtures verify split UTF-8, local status isolation,
partial-line close and disk-error recovery. Existing capture, identity, registry,
maintenance, session and demo contracts pass in the complete 542-test backend
suite (three native Windows tests skipped), with 74 frontend checks and sharing
checks. A spectator fixture now uses temporary storage and explicitly closes its
writer, fixing its stale method mock and unclosed-log warning. Windows build
syntax parses; native execution remains unverified.

Windows packaging now creates `bundle-manifest.json` after all executable,
library, UI, launcher, documentation and notice files are ready. ZIP creation
and standalone installer compilation verify the complete file set, sizes and
SHA-256 values. Local fixtures reject same-size changes, missing/extra files,
runtime data, redirected paths and invalid manifests; subprocess tests verify
the actual CLI returns failure for a modified DLL. Full regression passes 539
backend tests (three native Windows tests skipped), 74 frontend checks and
sharing checks; all seven manifest tests pass, including the subsequently added
CLI check. Both builder scripts parse on local PowerShell. This inventory gives
native-binary review a file list, but does not identify embedded libraries or
resolve their notices; actual frozen Windows files still require review.

Public request parsing is shared by control and collector. Contract checks
verify HTTP 400 before device access for duplicate fields, non-finite numbers,
lone surrogates and excessive nesting, while normal Unicode remains intact.
Agent proxy checks fail before contacting the collector; direct task/artifact
services reject non-object payloads. The checkpoint passes 533 backend tests
(three native Windows tests skipped), 74 frontend checks, sharing checks and
the real simulated-host browser input/SSE/upload/download/copy/reload workflow.
The Windows builder includes parser tests and passes local PowerShell parsing.

Linux hardware cleanup now waits for the root within a bounded deadline and
checks remaining owned process/thread states before exclusive UART resume.
Local tests verify root-before-child exit, unaffected unrelated groups, zombie
leaders with live threads, unreadable/invisible metadata and cleanup timeouts.
The complete suite passes 522 backend tests (three native Windows tests skipped),
74 frontend isolation checks and sharing checks. Existing handoff tests verify
cleanup failures leave the UART paused. The adapter requires foreground helpers
that preserve group/namespace/credential ownership; it does not contain detached
descendants. Native Windows process ownership remains unverified.

The API/configuration review now includes all 34 host variable names and 54
public route names found in the runtime sources. `check_public_docs.py` checks
that inventory offline without importing hardware modules and is required by
Linux CI/releases and Windows builds. It does not verify defaults, parameters or
authorization semantics. This review also corrected boot-comparison selectors,
documented web-issued grant restrictions and profile-controlled automatic SSH
configuration, and specified Linux/Windows host/authentication differences.
Full content review remains a separate requirement.
Verification for this checkpoint: 514 backend tests pass (three native Windows
tests skipped), 74 frontend isolation checks and sharing checks pass. The Windows
build script parses on PowerShell; native execution remains unverified.

## Current remaining gate

Verify the newest native-package attribution/source additions in actual Windows
outputs and the next tagged release. Rust compiler commits embedded in the two
extensions match official 1.90.0/1.98.1 channel metadata; verified official
compiler archives supply original MIT/Apache/COPYRIGHT and generated library-only
copyright summaries. Paramiko's bundled upstream source has 44 Python modules
matching its reviewed wheel. The builder now supports an explicit replacement
source archive, installs a private snapshot before freezing and preserves that
snapshot/version/hash in its output. Native CI exercises this source-install
path with the reviewed archive; its new run is still required. The modeled Windows Rust registry graph now has
67 package/version pairs and 132 pinned original notice files; eight CFFI/libffi
source headers are also collected. All three frozen provider hashes match before
copying this catalog. Twenty notice tests and an actual frozen-provider copy
check pass; these supplements still require native packaging verification and
are absent from the previously published 1.5.0 draft. OpenSSL provider notices and supplemental
CPython source-baseline notices are now shipped, but are not a complete native
SBOM. The installed-distribution inventory is a conservative superset. Source
archives for bcrypt 5.0.0, cryptography 50.0.2, CFFI 2.1.1 and Paramiko 4.0.0
have been downloaded from PyPI and checked against its published SHA-256, without
executing build scripts. bcrypt and cryptography contain Cargo locks with 43
and 40 package records respectively; those are source graphs, not proof of all
activated Windows wheel features. Complete the review before claiming the full
optimization goal achieved. Physical UART/USB behavior remains limited to the
documented fixture/support scope; no installed service or real board was changed.
