# ADR 0004: Folder-scoped intake lifecycle

Status: accepted

Each direct child directory of a watch root is one intake generation. Reeloom
stabilizes its complete no-follow inventory, exposes only that folder's media
candidates to one run, and keeps the existing media plan families unchanged.

After a durable media settlement, a separate immutable
`FolderDispositionPlan v1` moves residual content to `archive/<source>` or
removes a verified empty directory through a recoverable tombstone. A bounded
deterministic planning failure may instead authorize `fail/<source>`. These
effects use a separate approval, transaction, journal, and recovery path.

`archive`, `fail`, hidden top-level directories, loose watch-root files, and
top-level symlinks never become intake generations. An `.env*` entry anywhere
in a folder blocks the generation without reading or moving it.

This preserves all M0-M10 canonical media bytes and hashes. Reapply continues
to operate only on the durable media-library layout and never reads source-side
residual buckets.
