# M11 threat model

M0-M10 root, plan, approval, journal, secret, and browser controls remain in
force.

| Threat | Control |
| --- | --- |
| One run sees sibling content | scanner creates an independent snapshot whose candidate paths all retain one top-folder prefix |
| Partial upload is planned | complete tree identity must remain unchanged for the configured settle interval |
| Symlink or directory swap | no-follow dirfd traversal, bound device/inode, atomic no-replace rename, post-rename full-tree verification |
| Secret file is archived | any `.env*` component blocks before content or metadata is read; blocked folders have no disposition |
| Old plan executes after drift | active generation is invalidated before media effect; apply requires the current active observation |
| Residual target is overwritten | target suffix is fixed in the canonical plan; execution uses atomic no-replace rename |
| Crash creates an ambiguous move | journal precedes rename; recovery accepts only exact source/destination inode combinations |
| FUSE rejects no-replace flags | report `degraded` and use the explicitly accepted checked-rename fallback; retain journal, identity reconciliation and recovery, while documenting the remaining external-writer race |
| Read or SSE retry repeats a move | settlement resolution is read-only; only the explicit recovery mutation can resume a claimed transaction |
| Capability probe touches media | probe uses random, identity-bound empty directories on the single-slot I/O lane and removes only those empty directories |
| Late file is silently absorbed | post-media drift settles again and creates a new immutable disposition hash |
| Temporary provider failure immediately moves input to fail | non-deterministic Agent or Provider failures retire the current generation and preserve the source for at most three retries; only the fourth failed generation may produce the bounded `agent_retry_exhausted` fail plan |
| A failed run remains attached after its source disappears | a successful watch scan starts a missing settle window; sustained absence becomes the exact `blocked/source_folder_missing` terminal projection only when no media or folder effect is claimed |
| An immutable historical plan permanently forces `.1`, `.2`, ... | name lookup ignores detached, superseded, blocked, and settled plan history; plan insertion serializes and rechecks live reservations before accepting the target |

Residual risk: a same-filesystem directory rename is atomic, but storage
durability still depends on the mounted filesystem honoring directory fsync.
CloudDrive WebDAV/gRPC is not enabled as a move backend until its live
conformance demonstrates the same no-overwrite and recovery properties.
