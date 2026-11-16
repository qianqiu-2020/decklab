# Offline WebSocket protocol dependencies

The original pure-Python wheels in this directory are loaded directly with
Python's standard zip importer. No installation or network access is needed on
the control or collector host. The applications do not modify upstream code.

`websocket-sources.json` fixes the official PyPI download URLs, versions and
SHA-256 values. `seriald_core/websocket_io.py` verifies those same immutable
inputs before importing them. An already loaded, different system copy is not
silently substituted. Each wheel also contains its original MIT license; exact
license copies are retained here for offline delivery.

The existing Linux package `vendor/` copy and Windows `vendor` data inclusion
carry these inputs unchanged. This directory must not be exposed by the public
static file allowlist.
