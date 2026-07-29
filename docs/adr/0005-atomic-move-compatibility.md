# ADR 0005: Atomic move compatibility

## Status

Accepted.

## Decision

Reeloom continues to require a native no-replace move primitive. It never
falls back to ordinary `rename`, preflight-then-rename, copy/unlink, or a
probabilistic reservation.

Native move failures are classified into bounded codes. After every syscall
result Reeloom re-observes the exact source and destination identity:

- exact source plus absent destination is a proven no-effect result;
- absent source plus exact destination is a completed move;
- every other combination requires recovery.

An unsupported primitive leaves the claimed transaction non-terminal. Exact
recovery reuses the same plan, approval, transaction, and journal. Read-only
REST reconciliation never invokes recovery.

The Admin configuration page exposes an explicit capability probe. It uses
only owned empty directories, verifies collision preservation and parent
`fsync`, and shares the bounded single-slot directory I/O lane.

CloudDrive FUSE is not given an ordinary-rename exception. A future
CloudDrive WebDAV backend remains reserved but disabled until live
conformance proves exact-target no-overwrite, timeout convergence, identity
stability, rollback behavior, and restart safety.

## Compatibility

Existing media and folder plan bytes, hashes, approvals, transaction IDs, and
journal schemas do not change. The nullable folder transaction failure code
and move backend are read-model metadata only.
