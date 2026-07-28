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
| Late file is silently absorbed | post-media drift settles again and creates a new immutable disposition hash |
| Temporary provider failure moves input to fail | only allowlisted deterministic failures produce a fail plan |

Residual risk: a same-filesystem directory rename is atomic, but storage
durability still depends on the mounted filesystem honoring directory fsync.
