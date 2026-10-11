# Security and deployment scope

DeckLab controls development machines and intentionally supports trusted-LAN
operation without authentication. Choose local-only or authenticated LAN mode
when clients are not all trusted. Keep the service off the public Internet;
use a TLS reverse proxy for access over an untrusted network.

Agent grants are revocable and can be restricted to a stable board serial.
Their scopes do not restrict anonymous clients in open mode. Human share links
provide collaboration access; an actor label is not a verified person identity.

Board logs and diagnostic files can contain secrets. Do not attach them to a
public issue without review. SSH host keys can change after flashing; the
current development workflow does not offer persistent host-key verification.
Root SSH provisioning changes the board's configuration and is intended for
explicitly configured development boards.

For vulnerabilities, use GitHub's private vulnerability reporting if enabled on
the repository. Do not disclose credentials or an exploitable deployment in a
public issue. If private reporting is unavailable, ask the maintainers for a
private reporting channel without including exploit details.
