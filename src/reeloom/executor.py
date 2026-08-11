"""Forward-only execution.

Every move is decided by looking at the filesystem right now, so running the
same plan twice is safe: whatever already happened is recognised and skipped.
That property is what replaces V1's journal, rollback and recovery machinery —
a crashed run is finished by simply executing it again.

Four outcomes, and they are exhaustive:

===========================  ==================================
source present, dest absent  rename it                  MOVED
source absent, dest present  a previous pass did it     ALREADY_DONE
source present, dest present the library wins; source   DUPLICATE
                             goes to the fail bucket
neither present              nothing to do              MISSING
===========================  ==================================
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import replace
from pathlib import Path, PurePosixPath

from reeloom.models import (
    ExecutedMove,
    Move,
    MoveKind,
    MoveOutcome,
    ReeloomError,
    Root,
    Run,
    RunResult,
    WatchConfig,
)
from reeloom.rename import RenameFailure, classify, rename_noreplace
from reeloom.scanner import (
    ACQUIRED_DIR,
    ARCHIVE_BUCKET,
    FAIL_BUCKET,
    SUBTITLE_EXTENSIONS,
    safe_relative,
)

_LOGGER = logging.getLogger(__name__)


class ExecutionError(ReeloomError):
    pass


class FilesystemExecutor:
    """Implements the worker's ``Executor`` protocol."""

    def __init__(self, database) -> None:
        self._db = database

    async def execute(self, run: Run, config: WatchConfig) -> RunResult:
        if run.plan is None:
            raise ExecutionError("missing_plan")
        roots = _Roots.of(config)

        moved = 0
        subtitles_moved = 0
        duplicates: list[str] = []
        missing: list[str] = []

        for move in run.plan.moves:
            executed = await asyncio.to_thread(self._apply, move, roots, run)
            await self._db.append_executed(run.id, executed)
            name = PurePosixPath(move.source_path).name
            if executed.move.kind is MoveKind.FAIL:
                # The move was diverted: the library already held this episode.
                duplicates.append(name)
            elif executed.outcome is MoveOutcome.MOVED:
                if PurePosixPath(name).suffix.lower() in SUBTITLE_EXTENSIONS:
                    subtitles_moved += 1
                else:
                    moved += 1
            elif executed.outcome is MoveOutcome.MISSING:
                missing.append(name)

        archived = await self._sweep(run, roots)
        return RunResult(
            moved=moved,
            duplicates=tuple(duplicates),
            missing=tuple(missing),
            archived=archived,
            subtitles_moved=subtitles_moved,
        )

    async def revert(self, run: Run, config: WatchConfig) -> None:
        """Put everything this run moved back where it came from.

        Only moves recorded as MOVED are undone, in reverse order, using the
        same idempotent rules — so an interrupted revert is finished by
        running it again.
        """

        roots = _Roots.of(config)
        for executed in reversed(run.executed_moves):
            if executed.outcome is not MoveOutcome.MOVED:
                continue
            await asyncio.to_thread(self._apply, executed.move.reversed(), roots, run)
        await asyncio.to_thread(self._prune_buckets, run, roots)

    async def apply_move(
        self, move: Move, config: WatchConfig, run: Run
    ) -> ExecutedMove:
        """Apply one move through the same never-overwrite path.

        Used by subtitle publication so an acquired subtitle cannot take a
        different route into the library than a mapped file does.
        """

        executed = await asyncio.to_thread(self._apply, move, _Roots.of(config), run)
        await self._db.append_executed(run.id, executed)
        return executed

    async def discard(self, run: Run, config: WatchConfig) -> int:
        """Give up on a folder: park all of it, as downloaded, in the fail bucket.

        The original download is never deleted, so a discard can be looked at
        afterwards and, if it was a mistake, moved back by hand. Subtitles
        reeloom downloaded itself (the ``.acquired`` staging) are the one
        deliberate exception: they are deleted rather than parked, so the fail
        bucket holds exactly what came in — and they can be downloaded again.
        """

        roots = _Roots.of(config)
        await asyncio.to_thread(_delete_acquired_staging, roots.inbound, run)
        moved = await self._park_tree(run, roots, FAIL_BUCKET, MoveKind.FAIL)
        await asyncio.to_thread(
            _remove_empty_tree, roots.inbound / ARCHIVE_BUCKET / run.folder_name
        )
        return moved

    # ---- one move -----------------------------------------------------

    def _apply(self, move: Move, roots: _Roots, run: Run) -> ExecutedMove:
        source = roots.resolve(move.source_root, move.source_path)
        destination = roots.resolve(move.dest_root, move.dest_path)

        if move.kind is MoveKind.FOLDER_RENAME:
            return ExecutedMove(move, self._rename_folder(source, destination))

        outcome = self._move_file(source, destination, roots.base(move.dest_root))
        if outcome is not MoveOutcome.DUPLICATE:
            return ExecutedMove(move, outcome)

        # The library already has this episode. Keep what is there and put the
        # new copy in the fail bucket instead of deciding which one is better.
        redirect = self._fail_move(move, run)
        fail_destination = roots.resolve(redirect.dest_root, redirect.dest_path)
        _LOGGER.info("duplicate, diverting to fail: %s", move.source_path)
        return ExecutedMove(
            redirect,
            self._move_file(source, fail_destination, roots.base(redirect.dest_root)),
        )

    def _move_file(self, source: Path, destination: Path, base: Path) -> MoveOutcome:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _check_within(base, destination)
        try:
            rename_noreplace(source, destination)
        except FileExistsError:
            if _exists(source):
                return MoveOutcome.DUPLICATE
            return MoveOutcome.ALREADY_DONE
        except OSError as error:
            failure = classify(error)
            if failure is RenameFailure.MISSING_SOURCE:
                return (
                    MoveOutcome.ALREADY_DONE
                    if _exists(destination)
                    else MoveOutcome.MISSING
                )
            if failure is RenameFailure.CROSS_FILESYSTEM:
                raise ExecutionError(
                    "cross_filesystem_move",
                    source=str(source),
                    destination=str(destination),
                ) from error
            raise ExecutionError(
                "rename_failed", reason=failure.value, source=str(source)
            ) from error
        return MoveOutcome.MOVED

    def _rename_folder(self, source: Path, destination: Path) -> MoveOutcome:
        """Backfill a ``{tmdb-id}`` on an older folder.

        If the canonical folder turned up in the meantime, leave both alone:
        episodes still land in the canonical one, and merging two folders is
        not something to do silently.
        """

        if not _exists(source):
            return MoveOutcome.ALREADY_DONE if _exists(destination) else MoveOutcome.MISSING
        if _exists(destination):
            _LOGGER.info("canonical folder already exists, not merging: %s", destination)
            return MoveOutcome.DUPLICATE
        try:
            rename_noreplace(source, destination)
        except FileExistsError:
            return MoveOutcome.DUPLICATE
        except OSError as error:
            raise ExecutionError(
                "folder_rename_failed", reason=classify(error).value
            ) from error
        return MoveOutcome.MOVED

    def _fail_move(self, move: Move, run: Run) -> Move:
        relative = PurePosixPath(move.source_path)
        if relative.parts and relative.parts[0] == run.folder_name:
            relative = PurePosixPath(*relative.parts[1:])
        return replace(
            move,
            kind=MoveKind.FAIL,
            dest_root=Root.INBOUND,
            dest_path=f"{FAIL_BUCKET}/{run.folder_name}/{relative.as_posix()}",
        )

    # ---- residue ------------------------------------------------------

    async def _sweep(self, run: Run, roots: _Roots) -> int:
        """Move whatever is left in the intake folder to the archive bucket.

        Unmapped files are never deleted; they are parked where they can be
        looked at, and the emptied folder is removed so the scanner stops
        seeing it.
        """

        return await self._park_tree(run, roots, ARCHIVE_BUCKET, MoveKind.ARCHIVE)

    async def _park_tree(
        self, run: Run, roots: _Roots, bucket: str, kind: MoveKind
    ) -> int:
        """Move the folder's remaining content into ``bucket``. Returns files.

        The inbound root may be a network mount where every rename is a
        request, so movement is as coarse as the bucket allows: the whole
        folder when the bucket does not hold its name yet, then whole
        subfolders, then single files. Each executed unit lands in the ledger,
        so revert and crash replay see directory moves like any other.
        """

        moved = 0
        units = await asyncio.to_thread(_park_units, roots.inbound, run, bucket)
        for relative, files in units:
            suffix = f"/{relative}" if relative else ""
            move = Move(
                kind=kind,
                source_root=Root.INBOUND,
                source_path=f"{run.folder_name}{suffix}",
                dest_root=Root.INBOUND,
                dest_path=f"{bucket}/{run.folder_name}{suffix}",
            )
            executed = await asyncio.to_thread(self._apply, move, roots, run)
            await self._db.append_executed(run.id, executed)
            if executed.outcome is MoveOutcome.MOVED:
                moved += files
        await asyncio.to_thread(
            _remove_empty_tree, roots.inbound / run.folder_name
        )
        return moved

    def _prune_buckets(self, run: Run, roots: _Roots) -> None:
        for bucket in (ARCHIVE_BUCKET, FAIL_BUCKET):
            _remove_empty_tree(roots.inbound / bucket / run.folder_name)


class _Roots:
    def __init__(self, inbound: Path, library: Path) -> None:
        self.inbound = inbound
        self.library = library

    @classmethod
    def of(cls, config: WatchConfig) -> _Roots:
        inbound = Path(config.inbound_root)
        library = Path(config.library_root)
        if not inbound.is_absolute() or not library.is_absolute():
            raise ExecutionError(
                "roots_must_be_absolute",
                inbound=config.inbound_root,
                library=config.library_root,
            )
        return cls(inbound, library)

    def base(self, root: Root) -> Path:
        return self.inbound if root is Root.INBOUND else self.library

    def resolve(self, root: Root, relative: str) -> Path:
        return self.base(root) / safe_relative(relative).as_posix()


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _check_within(base: Path, destination: Path) -> None:
    """Verify the real destination parent is still inside its root.

    ``safe_relative`` already rules out ``..``; this catches the other way
    out, a symlinked directory somewhere in the parent chain. Checked after
    the parents are created and immediately before the rename.
    """

    resolved = destination.parent.resolve()
    if resolved != base.resolve() and base.resolve() not in resolved.parents:
        raise ExecutionError(
            "destination_escapes_root", path=str(resolved), root=str(base)
        )


def _delete_acquired_staging(inbound: Path, run: Run) -> None:
    """Delete subtitles reeloom downloaded for this run.

    The one place anything is ever deleted. These files did not come with the
    release — parking them in ``fail/`` would make the discarded folder differ
    from the original download, and they are always re-downloadable.
    """

    staging = inbound / ARCHIVE_BUCKET / run.folder_name / ACQUIRED_DIR
    if staging.is_dir() and not staging.is_symlink():
        _LOGGER.info("deleting acquired subtitle staging: %s", staging)
        shutil.rmtree(staging)


def _park_units(inbound: Path, run: Run, bucket: str) -> list[tuple[str, int]]:
    """Units of remaining content to move into ``bucket``.

    Each unit is ``(relative_path, file_count)``; ``""`` stands for the run
    folder itself. A directory is one unit only while its bucket twin does not
    exist — an existing twin (a replayed run, or an older run of the same
    name) forces a descent so nothing is merged blindly. Directories holding
    no files are left for the rmdir pass.
    """

    folder = inbound / run.folder_name
    if folder.is_symlink() or not folder.is_dir():
        return []

    def file_count(path: Path) -> int:
        total = 0
        for _, _, filenames in os.walk(path, followlinks=False):
            total += len(filenames)
        return total

    units: list[tuple[str, int]] = []

    def visit(source: Path, taken: Path, prefix: str) -> None:
        with os.scandir(source) as scanned:
            entries = sorted(scanned, key=lambda entry: entry.name)
        for entry in entries:
            relative = f"{prefix}{entry.name}"
            twin = taken / entry.name
            if entry.is_dir(follow_symlinks=False):
                count = file_count(Path(entry.path))
                if count == 0:
                    continue
                if twin.exists() or twin.is_symlink():
                    visit(Path(entry.path), twin, f"{relative}/")
                else:
                    units.append((relative, count))
            else:
                units.append((relative, 1))

    destination = inbound / bucket / run.folder_name
    total = file_count(folder)
    if total == 0:
        return []
    if destination.exists() or destination.is_symlink():
        visit(folder, destination, "")
    else:
        units.append(("", total))
    return units


def _remove_empty_tree(folder: Path) -> None:
    """rmdir bottom-up. Never removes anything that still holds a file."""

    if not folder.is_dir() or folder.is_symlink():
        return
    for current, directories, _ in os.walk(folder, topdown=False, followlinks=False):
        for name in directories:
            _try_rmdir(Path(current) / name)
    _try_rmdir(folder)


def _try_rmdir(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass
