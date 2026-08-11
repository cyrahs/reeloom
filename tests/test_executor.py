from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from reeloom.executor import ExecutionError, FilesystemExecutor
from reeloom.models import (
    ExecutedMove,
    FileKind,
    MediaIdentity,
    MediaType,
    Move,
    MoveKind,
    MoveOutcome,
    Plan,
    Root,
    Run,
    RunState,
    SnapshotFile,
    WatchConfig,
)
from tests.conftest import make_files
from tests.fakes import FakeDatabase

IDENTITY = MediaIdentity(MediaType.ANIME, 123, "Show", 2024)
FOLDER = "Drop"


def media_move(source: str, destination: str, candidate: str = "V1") -> Move:
    return Move(
        kind=MoveKind.MEDIA,
        source_root=Root.INBOUND,
        source_path=f"{FOLDER}/{source}",
        dest_root=Root.LIBRARY,
        dest_path=destination,
        candidate_id=candidate,
    )


def build_run(*moves: Move, snapshot=(), executed=()) -> Run:
    return Run(
        id="run-1",
        config_id="config-1",
        folder_name=FOLDER,
        state=RunState.EXECUTING,
        snapshot=tuple(snapshot),
        plan=Plan(identity=IDENTITY, moves=tuple(moves)),
        executed_moves=tuple(executed),
    )


@pytest.fixture
def executor() -> tuple[FilesystemExecutor, FakeDatabase]:
    database = FakeDatabase()
    return FilesystemExecutor(database), database


async def test_plain_move_lands_in_the_library(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv")
    engine, database = executor
    run = build_run(media_move("ep01.mkv", "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"))
    database.runs[run.id] = run

    result = await engine.execute(run, config)

    assert (library / "Show (2024) {tmdb-123}/S01/Show S01E01.mkv").is_file()
    assert not (inbound / FOLDER).exists()
    assert result.moved == 1


async def test_existing_destination_is_never_overwritten(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", size=10)
    destination = library / "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"original")
    engine, database = executor
    run = build_run(media_move("ep01.mkv", destination.relative_to(library).as_posix()))
    database.runs[run.id] = run

    result = await engine.execute(run, config)

    assert destination.read_bytes() == b"original"
    assert result.duplicates == ("ep01.mkv",)
    assert result.moved == 0
    assert (inbound / "fail" / FOLDER / "ep01.mkv").is_file()


async def test_unmapped_residue_is_archived_not_deleted(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", "extras/trailer.mkv", "readme.txt")
    engine, database = executor
    run = build_run(media_move("ep01.mkv", "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"))
    database.runs[run.id] = run

    result = await engine.execute(run, config)

    archive = inbound / "archive" / FOLDER
    assert (archive / "extras/trailer.mkv").is_file()
    assert (archive / "readme.txt").is_file()
    assert result.archived == 2
    assert not (inbound / FOLDER).exists()


async def test_missing_source_is_recorded_and_does_not_stop_the_run(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep02.mkv")
    engine, database = executor
    run = build_run(
        media_move("ep01.mkv", "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"),
        media_move("ep02.mkv", "Show (2024) {tmdb-123}/S01/Show S01E02.mkv", "V2"),
    )
    database.runs[run.id] = run

    result = await engine.execute(run, config)

    assert result.missing == ("ep01.mkv",)
    assert result.moved == 1
    assert (library / "Show (2024) {tmdb-123}/S01/Show S01E02.mkv").is_file()


async def test_replaying_a_finished_plan_changes_nothing(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", size=7)
    engine, database = executor
    move = media_move("ep01.mkv", "Show (2024) {tmdb-123}/S01/Show S01E01.mkv")
    run = build_run(move)
    database.runs[run.id] = run
    await engine.execute(run, config)

    result = await engine.execute(run, config)

    destination = library / "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"
    assert destination.read_bytes() == b"x" * 7
    assert result.moved == 0
    assert result.missing == ()
    assert result.duplicates == ()


async def test_crash_midway_is_finished_by_running_again(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", "ep02.mkv")
    engine, database = executor
    moves = (
        media_move("ep01.mkv", "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"),
        media_move("ep02.mkv", "Show (2024) {tmdb-123}/S01/Show S01E02.mkv", "V2"),
    )
    run = build_run(*moves)
    database.runs[run.id] = run

    # Simulate the process dying after the first rename.
    await engine.apply_move(moves[0], config, run)

    result = await engine.execute(run, config)

    assert (library / "Show (2024) {tmdb-123}/S01/Show S01E01.mkv").is_file()
    assert (library / "Show (2024) {tmdb-123}/S01/Show S01E02.mkv").is_file()
    assert result.moved == 1


async def test_execution_records_what_it_actually_did(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", "extra.txt")
    destination = library / "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"already here")
    engine, database = executor
    run = build_run(media_move("ep01.mkv", destination.relative_to(library).as_posix()))
    database.runs[run.id] = run

    await engine.execute(run, config)

    recorded = database.runs[run.id].executed_moves
    # The duplicate is recorded as the move into fail that actually happened,
    # not as the library move that was planned.
    assert [(item.move.kind, item.outcome) for item in recorded] == [
        (MoveKind.FAIL, MoveOutcome.MOVED),
        (MoveKind.ARCHIVE, MoveOutcome.MOVED),
    ]


async def test_revert_puts_everything_back(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", "notes.txt")
    engine, database = executor
    run = build_run(media_move("ep01.mkv", "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"))
    database.runs[run.id] = run
    await engine.execute(run, config)

    await engine.revert(database.runs[run.id], config)

    assert (inbound / FOLDER / "ep01.mkv").is_file()
    assert (inbound / FOLDER / "notes.txt").is_file()
    assert not (library / "Show (2024) {tmdb-123}/S01/Show S01E01.mkv").exists()
    assert not (inbound / "archive" / FOLDER).exists()


async def test_revert_restores_a_duplicate_from_the_fail_bucket(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv")
    destination = library / "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"original")
    engine, database = executor
    run = build_run(media_move("ep01.mkv", destination.relative_to(library).as_posix()))
    database.runs[run.id] = run
    await engine.execute(run, config)

    await engine.revert(database.runs[run.id], config)

    assert (inbound / FOLDER / "ep01.mkv").is_file()
    assert destination.read_bytes() == b"original"
    assert not (inbound / "fail" / FOLDER).exists()


async def test_revert_is_itself_replayable(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv")
    engine, database = executor
    run = build_run(media_move("ep01.mkv", "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"))
    database.runs[run.id] = run
    await engine.execute(run, config)

    await engine.revert(database.runs[run.id], config)
    await engine.revert(database.runs[run.id], config)

    assert (inbound / FOLDER / "ep01.mkv").is_file()


async def test_revert_ignores_moves_that_never_happened(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    engine, database = executor
    make_files(library, "Show (2024) {tmdb-123}/S01/Show S01E01.mkv")
    run = build_run(
        executed=[
            ExecutedMove(
                media_move("ep01.mkv", "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"),
                MoveOutcome.ALREADY_DONE,
            )
        ]
    )
    database.runs[run.id] = run

    await engine.revert(run, config)

    assert (library / "Show (2024) {tmdb-123}/S01/Show S01E01.mkv").is_file()
    assert not (inbound / FOLDER).exists()


async def test_untagged_library_folder_is_renamed_then_used(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv")
    make_files(library / "Show" / "S01", "Show S01E01.mkv")
    engine, database = executor
    run = build_run(
        Move(
            kind=MoveKind.FOLDER_RENAME,
            source_root=Root.LIBRARY,
            source_path="Show",
            dest_root=Root.LIBRARY,
            dest_path="Show (2024) {tmdb-123}",
        ),
        media_move("ep01.mkv", "Show (2024) {tmdb-123}/S02/Show S02E01.mkv"),
    )
    database.runs[run.id] = run

    await engine.execute(run, config)

    assert not (library / "Show").exists()
    assert (library / "Show (2024) {tmdb-123}/S01/Show S01E01.mkv").is_file()
    assert (library / "Show (2024) {tmdb-123}/S02/Show S02E01.mkv").is_file()


async def test_folder_rename_does_not_merge_into_an_existing_canonical_folder(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv")
    make_files(library / "Show" / "S01", "old.mkv")
    make_files(library / "Show (2024) {tmdb-123}" / "S01", "new.mkv")
    engine, database = executor
    run = build_run(
        Move(
            kind=MoveKind.FOLDER_RENAME,
            source_root=Root.LIBRARY,
            source_path="Show",
            dest_root=Root.LIBRARY,
            dest_path="Show (2024) {tmdb-123}",
        ),
        media_move("ep01.mkv", "Show (2024) {tmdb-123}/S02/Show S02E01.mkv"),
    )
    database.runs[run.id] = run

    await engine.execute(run, config)

    assert (library / "Show" / "S01" / "old.mkv").is_file()
    assert (library / "Show (2024) {tmdb-123}/S02/Show S02E01.mkv").is_file()
    recorded = database.runs[run.id].executed_moves
    assert recorded[0].outcome is MoveOutcome.DUPLICATE


async def test_symlinked_destination_parent_is_refused(
    config: WatchConfig, roots: tuple[Path, Path], executor, tmp_path: Path
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv")
    outside = tmp_path / "outside"
    outside.mkdir()
    (library / "Show (2024) {tmdb-123}").mkdir()
    os.symlink(outside, library / "Show (2024) {tmdb-123}" / "S01")
    engine, database = executor
    run = build_run(media_move("ep01.mkv", "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"))
    database.runs[run.id] = run

    with pytest.raises(ExecutionError) as error:
        await engine.execute(run, config)

    assert error.value.code == "destination_escapes_root"
    assert not any(outside.iterdir())


async def test_path_traversal_in_a_stored_plan_is_refused(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, _ = roots
    make_files(inbound / FOLDER, "ep01.mkv")
    engine, database = executor
    run = build_run(media_move("ep01.mkv", "../escape/Show S01E01.mkv"))
    database.runs[run.id] = run

    with pytest.raises(Exception):
        await engine.execute(run, config)
