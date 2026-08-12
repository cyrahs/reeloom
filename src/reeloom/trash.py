"""The trash area for version replacement.

A replaced or duplicate file is never deleted in the execution path: it is
renamed into ``<inbound>/.reeloom-trash/<run-id>/<origin>/…`` under the watch
root — never under the library, because media servers scan the library and a
dot-directory there does not make the displaced version disappear from them —
and recorded in the run's ledger like any other move, so a revert can bring
it back for as long as it exists. The ``origin`` segment names where the file
came from (``library``, ``inbound``, ``extra-1`` …) so displaced files from
different roots cannot collide. The library and any extra directories must
therefore share a filesystem with the watch root; a cross-filesystem trash
move fails loudly instead of copying.

The periodic purge in the worker is the only place trash is actually deleted,
once the run is settled and the retention window has passed.
``purge_run_trash`` is the single hard-delete entry point in the codebase.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from reeloom.models import ReeloomError

_LOGGER = logging.getLogger(__name__)

TRASH_DIR = ".reeloom-trash"
"""Hidden holding area under the watch root; dot-prefixed so the scanner
never picks trashed files back up as new arrivals."""


class TrashError(ReeloomError):
    pass


def trash_relative(run_id: str, origin: str, relative: str) -> str:
    """Inbound-relative trash destination for a file this run displaces.

    ``origin`` names the root the file came from so equal relative paths
    from different roots land side by side instead of colliding.
    """

    _check_component(run_id)
    _check_component(origin)
    return f"{TRASH_DIR}/{run_id}/{origin}/{PurePosixPath(relative).as_posix()}"


@dataclass(frozen=True, slots=True)
class TrashEntry:
    """One run's worth of trash under one root."""

    run_id: str
    path: Path
    mtime: float
    """When the entry last changed — effectively when the run trashed into it."""
    files: int
    bytes: int


def list_trash_entries(root: Path) -> list[TrashEntry]:
    trash_root = root / TRASH_DIR
    if trash_root.is_symlink() or not trash_root.is_dir():
        return []

    entries: list[TrashEntry] = []
    with os.scandir(trash_root) as scanned:
        for entry in sorted(scanned, key=lambda item: item.name):
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            files, size = _measure(Path(entry.path))
            entries.append(
                TrashEntry(
                    run_id=entry.name,
                    path=Path(entry.path),
                    mtime=entry.stat(follow_symlinks=False).st_mtime,
                    files=files,
                    bytes=size,
                )
            )
    return entries


def purge_run_trash(root: Path, run_id: str) -> tuple[int, int]:
    """Delete one run's trash under ``root``. Returns ``(files, bytes)``.

    This is the only place reeloom deletes media files, so it is deliberately
    narrow: the target must be exactly ``<root>/.reeloom-trash/<run-id>``,
    resolve to a real directory still inside the trash root, and involve no
    symlink at any level of the addressed path.
    """

    _check_component(run_id)
    trash_root = root / TRASH_DIR
    if trash_root.is_symlink() or not trash_root.is_dir():
        return (0, 0)
    target = trash_root / run_id
    if target.is_symlink():
        raise TrashError("trash_entry_is_symlink", path=str(target))
    if not target.is_dir():
        return (0, 0)
    if target.resolve().parent != trash_root.resolve():
        raise TrashError("trash_entry_escapes_root", path=str(target))

    files, size = _measure(target)
    _LOGGER.info("purging trash %s (%d files, %d bytes)", target, files, size)
    shutil.rmtree(target)
    _try_rmdir(trash_root)
    return (files, size)


def prune_trash(root: Path) -> None:
    """Remove empty directories under the trash area, and the area itself.

    Reverts and replayed moves leave empty directory skeletons behind; this
    keeps the watch root clean without ever touching a file — only ``rmdir``,
    which fails harmlessly on anything non-empty.
    """

    trash_root = root / TRASH_DIR
    if trash_root.is_symlink() or not trash_root.is_dir():
        return
    for current, directories, _ in os.walk(
        trash_root, topdown=False, followlinks=False
    ):
        for name in directories:
            _try_rmdir(Path(current) / name)
    _try_rmdir(trash_root)


def _check_component(name: str) -> None:
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or name.startswith(".")
    ):
        raise TrashError("invalid_trash_component", component=name)


def _measure(folder: Path) -> tuple[int, int]:
    files = 0
    size = 0
    for current, _, names in os.walk(folder, followlinks=False):
        for name in names:
            files += 1
            try:
                size += (Path(current) / name).lstat().st_size
            except OSError:
                pass
    return (files, size)


def _try_rmdir(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass
