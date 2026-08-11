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
from reeloom.scanner import ARCHIVE_BUCKET, FAIL_BUCKET, safe_relative

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
                moved += 1
            elif executed.outcome is MoveOutcome.MISSING:
                missing.append(name)

        archived = await self._sweep(run, roots)
        return RunResult(
            moved=moved,
            duplicates=tuple(duplicates),
            missing=tuple(missing),
            archived=archived,
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
        """Give up on a folder: park all of it in the fail bucket.

        Nothing is deleted, so a discard can be looked at afterwards and, if
        it was a mistake, moved back by hand.
        """

        roots = _Roots.of(config)
        moved = 0
        for relative in await asyncio.to_thread(_remaining_files, roots.inbound, run):
            move = Move(
                kind=MoveKind.FAIL,
                source_root=Root.INBOUND,
                source_path=f"{run.folder_name}/{relative}",
                dest_root=Root.INBOUND,
                dest_path=f"{FAIL_BUCKET}/{run.folder_name}/{relative}",
            )
            executed = await self.apply_move(move, config, run)
            if executed.outcome is MoveOutcome.MOVED:
                moved += 1
        await asyncio.to_thread(_remove_empty_tree, roots.inbound / run.folder_name)
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

        archived = 0
        for relative in await asyncio.to_thread(_remaining_files, roots.inbound, run):
            move = Move(
                kind=MoveKind.ARCHIVE,
                source_root=Root.INBOUND,
                source_path=f"{run.folder_name}/{relative}",
                dest_root=Root.INBOUND,
                dest_path=f"{ARCHIVE_BUCKET}/{run.folder_name}/{relative}",
            )
            executed = await asyncio.to_thread(self._apply, move, roots, run)
            await self._db.append_executed(run.id, executed)
            if executed.outcome is MoveOutcome.MOVED:
                archived += 1
        await asyncio.to_thread(
            _remove_empty_tree, roots.inbound / run.folder_name
        )
        return archived

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


def _remaining_files(inbound: Path, run: Run) -> list[str]:
    folder = inbound / run.folder_name
    if not folder.is_dir():
        return []
    found: list[str] = []
    for current, _, filenames in os.walk(folder, followlinks=False):
        for filename in filenames:
            path = Path(current) / filename
            found.append(path.relative_to(folder).as_posix())
    return sorted(found)


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
