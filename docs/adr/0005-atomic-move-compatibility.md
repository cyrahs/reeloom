# ADR 0005: Atomic move compatibility

> M14 update (2026-08-07): accepted ADR 0008 supersedes this ADR for v2
> execution. The exact-identity rollback/recovery rules below remain descriptive
> only for the v1 write path until M14.4 removes it.

## Status

Accepted.

## Decision

Reeloom prefers a native no-replace move primitive. When that primitive
returns an unsupported error and both bound parent directories are on Linux
FUSE, it may fall back to an immediately rechecked ordinary `rename`.
Non-FUSE filesystems and every other error remain fail closed.

Native move failures are classified into bounded codes. After every syscall
result Reeloom re-observes the exact source and destination identity:

- exact source plus absent destination is a proven no-effect result;
- absent source plus exact destination is a completed move;
- every other combination requires recovery.

`EACCES`, `EPERM`, and `EROFS` are reported as `permission_denied`. A
zero-move permission failure keeps the claimed approval and exact transaction
recoverable. Folder generations are recreated only for proven source drift;
permission, unknown executor, provider, and mount failures remain in place
for Admin review instead of starting a new Agent run.

An unsupported primitive leaves the claimed transaction non-terminal. Exact
recovery reuses the same plan, approval, transaction, and journal. Read-only
REST reconciliation never invokes recovery.

The Admin configuration page exposes an explicit capability probe. It uses
only owned empty directories, verifies collision preservation and parent
`fsync`, and shares the bounded single-slot directory I/O lane.

The FUSE fallback is explicitly reported as `fuse_checked_rename` with
`degraded` capability status. It preserves preflight, dirfd/no-follow
binding, journal-before-effect, post-move identity convergence, rollback and
recovery. It cannot eliminate the race between the final absence check and
ordinary rename, so an external concurrent writer can still be overwritten.
This is an accepted deployment residual risk for FUSE only.

## Compatibility

Existing media and folder plan bytes, hashes, approvals, transaction IDs, and
journal schemas do not change. The nullable folder transaction failure code
and move backend are read-model metadata only.
