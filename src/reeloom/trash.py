"""The trash area for version replacement.

A replaced or duplicate file is never deleted in the execution path: it is
renamed into ``<root>/.reeloom-trash/<run-id>/…`` under the same root it came
from — same filesystem, hidden from the scanner and from media servers — and
recorded in the run's ledger like any other move, so a revert can bring it
back for as long as it exists. The periodic purge in the worker is the only
place trash is actually deleted, once the run is settled and the retention
window has passed.

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
"""Hidden per-root holding area; dot-prefixed so nothing else ever scans it."""


class TrashError(ReeloomError):
    pass


def trash_relative(run_id: str, relative: str) -> str:
    """Root-relative trash destination for a file this run displaces."""

    _check_run_id(run_id)
    return f"{TRASH_DIR}/{run_id}/{PurePosixPath(relative).as_posix()}"


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

    _check_run_id(run_id)
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


def _check_run_id(run_id: str) -> None:
    if (
        not run_id
        or run_id in (".", "..")
        or "/" in run_id
        or "\\" in run_id
        or run_id.startswith(".")
    ):
        raise TrashError("invalid_run_id", run_id=run_id)


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
