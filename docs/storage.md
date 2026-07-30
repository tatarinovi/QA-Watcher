# Event storage

State schema version 3 is persisted through the `EventStore` protocol.
Deployments use durable `<project_id>:<deployment_id>` keys and environment
cursors use `<project_id>:<canonical_environment>` keys. Schema-v2 files are
migrated atomically on load; project identity is taken only from each event's
stored `ProjectSnapshot`, and migration fails closed when that evidence is
missing or ambiguous. Existing report paths are retained and remain readable. The
current `JsonEventStore` implementation serializes every read and write with one
portable `filelock` sidecar lock. Writes use a temporary file in the state
file's directory, flush and `fsync` the file, atomically `os.replace` the target,
then `fsync` the containing directory on platforms that support directory
handles. Invalid JSON and unsupported schema versions fail closed; they are
never treated as empty state.

The JSON adapter is deliberately a **single-replica** store. A shared lock file
coordinates processes that see the same local filesystem, but it is not a
distributed consensus mechanism and must not be used by multiple containers or
hosts with independent filesystems. A future PostgreSQL adapter can implement
the same optimistic `load`/`save(expected_revision=...)` protocol for
multi-replica operation.
