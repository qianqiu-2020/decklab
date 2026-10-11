# Release acceptance audit

Audit baseline: source version 1.4.50, reviewed 2026-10-12. This separates
implemented behavior from platform/release evidence. Local tests use temporary
storage, simulated boards and fake transports; they do not prove real COM or
installed Windows behavior. Earlier checkpoint counts are historical.

## Requirement evidence

| Requirement | Implementation and focused evidence | Current assessment |
| --- | --- | --- |
| Public project | LICENSE, NOTICE, THIRD_PARTY.md, bilingual README, CONTRIBUTING.md, SECURITY.md and issue/PR templates; package tests | Present; native binary attribution incomplete |
| Releases | CI, Windows package and tagged release workflows; version-tag validator; Debian extraction fixtures | Local tooling checked; hosted jobs and Windows outputs unverified |
| Configurable hosts/boards | profiles.py, transport.py, windows_manager.py; test_profiles.py validates framing, profiles, manual mode and credentials | Local contracts verified |
| Hardware-free demo | demo.py, seriald_demo.py; test_demo.py and demo_smoke.js exercise normal HTTP/CLI/browser paths | Verified on Linux; frozen Windows execution unverified |
| Board/connection/session separation | models.py, devices.py, session_base.py; model, registry and session-contract tests | Local reconnect and migration contracts verified |
| Console coordination | transactions.py, console_protocol.py; transaction, protocol, handoff and input-ownership tests | Local timeout/cancel/generation contracts verified |
| Platform adapters | sessions.py, sessions_windows.py, windows_manager.py; shared session and fake Windows tests | Local fixtures verified; native Windows tests pending |
| Application services/API | actions.py, queries.py, results.py, API.md; action/query/HTTP/CLI tests | Implemented; 54-route source/contract content review complete locally |
| Frontend ownership | devices.js, terminal-view.js, archives.js, terminal-navigation.js; frontend.test.js | Isolation checks verified; desktop/mobile browser fixture passes five consecutive runs after synchronization fix |
| Schema upgrades | schema.py registry migrations through version 5; test_schema.py checks WAL backups, rollback, concurrent openers and future versions | Local SQLite fixtures verified; Windows upgrade pending |
| Collaboration | transactions.py, agent_grants.py, control_server.py; input-ownership, grants and sharing tests | Local reservation/revocation/takeover verified |
| Log operations | maintenance.py; maintenance/archive/identity tests cover active writers, cross-day boots, disk warnings and allowlisted diagnostics | Local fixtures verified; native Windows file semantics pending |
| Long operations | tasks.py, task_actions.py, transfer_tasks.py, transfer_artifacts.py; task/file/CLI tests and demo browser upload/download/copy/reload | Local task and artifact workflows verified |
| Hardware extensions | hardware.py, process_windows.py, exclusive_serial handoff; helper and handoff tests | Local/fake contracts verified; native Windows process-tree gate pending |
| Distributed documentation | package_docs.py; test_package_docs.py checks both layouts, examples, assets and UTF-8 links | API/config source review and layouts verified; native install/release and binary notices outstanding |

## Evidence from this audit

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

## Remaining gates

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
releases require the 24.04 check. Hosted execution remains unverified. Container
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

1. Verify supported-platform installation, upgrade and recovery behavior.
   API/configuration content has a local source/fixture review above; native
   installation evidence is still required.
2. Review the actual frozen Windows files and embedded native-library notices.
   The installed-Python distribution inventory is not a complete binary SBOM.
3. Execute native Windows tests, frozen demo/host checks and installer upgrades
   in a clean Windows account. Retain job output and generated package hashes.
4. Execute the tagged release pipeline on an authorized GitHub repository and
   review Setup, ZIP, Debian packages, checksums and release notes. Workflow
   definitions alone do not prove a usable release.

No native Windows or hosted GitHub results were available at this audit. The
overall optimization goal remains incomplete until these gates have evidence.
