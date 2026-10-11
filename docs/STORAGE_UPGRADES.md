# Database upgrades

DeckLab versions both SQLite databases with `PRAGMA user_version` and identifies
their purpose with `PRAGMA application_id`. The registry currently uses schema
version 4; the agent-grant database uses version 1. These versions are independent
of the application release number.

## Automatic upgrade

When the collector opens `registry.sqlite3`, or the control service opens
`agent-grants.sqlite3`, it checks the schema before using the data. Existing
unversioned DeckLab databases are adopted. Older registries gain missing session
completion fields, command history and identity-conflict tables; existing rows,
labels, serials and grant hashes remain intact. Registry schema 3 adds durable
task history and historical connection targets. Schema 4 adds host-side transfer
artifact metadata and task ownership. New empty databases need no backup.

Before modifying an existing database, DeckLab creates a snapshot under:

```text
<data-directory>/backups/
  registry.sqlite3.v3-to-v4.<timestamp>.<random-id>.sqlite3
  agent-grants.sqlite3.v0-to-v1.<timestamp>.<random-id>.sqlite3
```

SQLite's backup API includes committed WAL contents. A completed backup passes
an integrity check and is published from a temporary file before migration
starts. Snapshot files are created with owner-only permissions on Unix; Windows
access follows directory ACLs. They contain operational metadata and token hashes,
so treat them like the original databases. Backups are retained until explicitly
removed; normal restarts at the current schema do not make extra copies.

Migrations and version changes share one transaction. A failure rolls back the
database and stops startup with an error that names the completed backup, if one
exists. Backup failure also stops the upgrade. Concurrent startup attempts serialize
and recheck the version, avoiding duplicate migrations. A database from a newer
schema or another application is rejected without a downgrade attempt.

## Recovery

A failed migration normally leaves the original database usable by its previous
application version. Keep the reported backup for diagnosis. To restore a snapshot:

1. Stop every process using the affected database: both Linux services, or the
   Windows host. Do not restore while the collector or control service is running.
2. Preserve the current database and its `-wal`/`-shm` files together in a separate
   recovery directory before replacing anything.
3. Copy the matching backup to the original database filename, without carrying
   over the displaced database's WAL/SHM files. Preserve service ownership and access.
4. Start a compatible application version and verify boards, labels and grants.

Snapshots cover their database only, not serial/SSH log files or the other database.
Restoring an older snapshot loses metadata changes made after that snapshot.
Application rollback must therefore consider both database versions; installing
an older executable does not automatically downgrade or restore data.

## Adding a migration

Append a migration to the appropriate tuple in `seriald_core/schema.py`; never
change an already released migration. Use `execute()` inside the supplied
transaction, not `executescript()` or an explicit commit. Update the expected
column contract, migration fixtures and this guide. Tests must cover preservation,
failure rollback, backup contents and rejection of future versions.
