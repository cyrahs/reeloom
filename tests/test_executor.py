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


async def test_bundled_subtitles_are_counted_separately(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", "ep01.chs.ass")
    engine, database = executor
    run = build_run(
        media_move("ep01.mkv", "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"),
        media_move(
            "ep01.chs.ass", "Show (2024) {tmdb-123}/S01/Show S01E01.chs.ass", "S1"
        ),
    )
    database.runs[run.id] = run

    result = await engine.execute(run, config)

    assert (library / "Show (2024) {tmdb-123}/S01/Show S01E01.chs.ass").is_file()
    assert result.moved == 1
    assert result.subtitles_moved == 1


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


async def test_residue_is_archived_with_a_single_rename(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(
        inbound / FOLDER,
        "ep01.mkv",
        "extras/trailer.mkv",
        "extras/scans/cover.jpg",
        "readme.txt",
    )
    engine, database = executor
    run = build_run(media_move("ep01.mkv", "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"))
    database.runs[run.id] = run

    result = await engine.execute(run, config)

    archives = [
        item
        for item in database.runs[run.id].executed_moves
        if item.move.kind is MoveKind.ARCHIVE
    ]
    # The whole emptied folder went to the bucket in one rename.
    assert [(item.move.source_path, item.move.dest_path) for item in archives] == [
        (FOLDER, f"archive/{FOLDER}")
    ]
    assert result.archived == 3
    assert (inbound / "archive" / FOLDER / "extras/scans/cover.jpg").is_file()


async def test_a_mapped_file_leaves_its_subfolder_free_to_move_whole(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "Discs/ep01.mkv", "Discs/log.txt", "Discs/art.jpg")
    engine, database = executor
    run = build_run(
        media_move("Discs/ep01.mkv", "Show (2024) {tmdb-123}/S00/Show S00E01.mkv")
    )
    database.runs[run.id] = run

    result = await engine.execute(run, config)

    # The mapped video went to the library first; what remained of the
    # subfolder moved as one unit.
    assert (library / "Show (2024) {tmdb-123}/S00/Show S00E01.mkv").is_file()
    assert (inbound / "archive" / FOLDER / "Discs/log.txt").is_file()
    assert (inbound / "archive" / FOLDER / "Discs/art.jpg").is_file()
    archives = [
        item
        for item in database.runs[run.id].executed_moves
        if item.move.kind is MoveKind.ARCHIVE
    ]
    assert len(archives) == 1
    assert result.archived == 2


async def test_an_occupied_bucket_name_forces_subfolder_and_file_units(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", "extras/trailer.mkv", "readme.txt")
    # An earlier run of a same-named folder already archived something.
    make_files(inbound / "archive" / FOLDER, "old.txt")
    engine, database = executor
    run = build_run(media_move("ep01.mkv", "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"))
    database.runs[run.id] = run

    result = await engine.execute(run, config)

    archives = sorted(
        item.move.source_path
        for item in database.runs[run.id].executed_moves
        if item.move.kind is MoveKind.ARCHIVE
    )
    assert archives == [f"{FOLDER}/extras", f"{FOLDER}/readme.txt"]
    assert result.archived == 2
    assert (inbound / "archive" / FOLDER / "old.txt").is_file()
    assert (inbound / "archive" / FOLDER / "extras/trailer.mkv").is_file()
    assert (inbound / "archive" / FOLDER / "readme.txt").is_file()


async def test_discard_parks_the_folder_with_one_rename(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, _ = roots
    make_files(inbound / FOLDER, "ep01.mkv", "extras/trailer.mkv")
    engine, database = executor
    run = build_run(snapshot=(SnapshotFile("V1", "ep01.mkv", FileKind.VIDEO, 1),))
    database.runs[run.id] = run

    moved = await engine.discard(run, config)

    assert moved == 2
    assert (inbound / "fail" / FOLDER / "ep01.mkv").is_file()
    assert (inbound / "fail" / FOLDER / "extras/trailer.mkv").is_file()
    assert not (inbound / FOLDER).exists()
    fails = [
        item
        for item in database.runs[run.id].executed_moves
        if item.move.kind is MoveKind.FAIL
    ]
    assert [(item.move.source_path, item.move.dest_path) for item in fails] == [
        (FOLDER, f"fail/{FOLDER}")
    ]


async def test_reverting_a_folder_level_archive_restores_the_tree(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", "extras/trailer.mkv", "readme.txt")
    engine, database = executor
    run = build_run(media_move("ep01.mkv", "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"))
    database.runs[run.id] = run
    await engine.execute(run, config)

    await engine.revert(database.runs[run.id], config)

    assert (inbound / FOLDER / "ep01.mkv").is_file()
    assert (inbound / FOLDER / "extras/trailer.mkv").is_file()
    assert (inbound / FOLDER / "readme.txt").is_file()
    assert not (library / "Show (2024) {tmdb-123}/S01/Show S01E01.mkv").exists()
    assert not (inbound / "archive" / FOLDER).exists()


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


async def test_discard_parks_the_folder_and_deletes_acquired_subtitles(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, _ = roots
    make_files(inbound / FOLDER, "ep01.mkv", "extras/notes.txt")
    make_files(inbound / "archive" / FOLDER / ".acquired", "Show S01E01.chs.ass")
    engine, database = executor
    run = build_run()
    database.runs[run.id] = run

    moved = await engine.discard(run, config)

    assert moved == 2
    assert (inbound / "fail" / FOLDER / "ep01.mkv").is_file()
    assert (inbound / "fail" / FOLDER / "extras/notes.txt").is_file()
    assert not (inbound / FOLDER).exists()
    # Reeloom's own downloads are deleted, not parked: the fail bucket holds
    # exactly what came in, and the emptied archive folder is cleaned up.
    assert not (inbound / "archive" / FOLDER).exists()


async def test_discard_does_not_follow_a_symlinked_staging_folder(
    config: WatchConfig, roots: tuple[Path, Path], executor, tmp_path: Path
) -> None:
    inbound, _ = roots
    make_files(inbound / FOLDER, "ep01.mkv")
    outside = tmp_path / "outside"
    make_files(outside, "keep.ass")
    staging_parent = inbound / "archive" / FOLDER
    staging_parent.mkdir(parents=True)
    os.symlink(outside, staging_parent / ".acquired")
    engine, database = executor
    run = build_run()
    database.runs[run.id] = run

    await engine.discard(run, config)

    assert (outside / "keep.ass").is_file()


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


# ---- version replacement (trash moves) ----------------------------------


from reeloom.trash import TRASH_DIR, purge_run_trash  # noqa: E402

DEST = "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"


def trash_replaced(
    relative: str, *, root: Root = Root.LIBRARY, extra_base: str | None = None
) -> Move:
    origin = "extra-1" if root is Root.EXTRA else root.value
    return Move(
        kind=MoveKind.TRASH_REPLACED,
        source_root=root,
        source_path=relative,
        dest_root=Root.INBOUND,
        dest_path=f"{TRASH_DIR}/run-1/{origin}/{relative}",
        extra_base=extra_base,
    )


def replacement_run(*, extra_base: str | None = None) -> Run:
    moves = [trash_replaced(DEST)]
    if extra_base is not None:
        moves.append(
            trash_replaced(
                "Show/ep01.mp4", root=Root.EXTRA, extra_base=extra_base
            )
        )
    moves.append(media_move("ep01.mkv", DEST))
    return build_run(*moves)


async def test_replacement_displaces_the_old_version_then_imports(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", size=32)
    make_files(library, DEST, size=16)
    engine, database = executor
    run = replacement_run()
    database.runs[run.id] = run

    result = await engine.execute(run, config)

    assert (library / DEST).stat().st_size == 32
    assert (inbound / TRASH_DIR / "run-1" / "library" / DEST).stat().st_size == 16
    assert result.replaced == ("Show S01E01.mkv",)
    assert result.moved == 1
    assert result.duplicates == ()


async def test_replacement_reaches_into_extra_dirs(
    config: WatchConfig, roots: tuple[Path, Path], executor, tmp_path: Path
) -> None:
    inbound, library = roots
    extra = tmp_path / "anirss"
    make_files(inbound / FOLDER, "ep01.mkv", size=32)
    make_files(library, DEST, size=16)
    make_files(extra, "Show/ep01.mp4", size=8)
    engine, database = executor
    run = replacement_run(extra_base=str(extra))
    database.runs[run.id] = run

    result = await engine.execute(run, config)

    # The extra dir's old copy is trashed under the watch root too, in its
    # own origin segment.
    assert (inbound / TRASH_DIR / "run-1" / "extra-1" / "Show/ep01.mp4").is_file()
    assert not (extra / "Show/ep01.mp4").exists()
    assert result.replaced == ("Show S01E01.mkv", "ep01.mp4")


async def test_replaying_a_completed_replacement_changes_nothing(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", size=32)
    make_files(library, DEST, size=16)
    engine, database = executor
    run = replacement_run()
    database.runs[run.id] = run
    await engine.execute(run, config)

    # Replay: the trash destination exists, so the trash move is done — the
    # new file now sitting at the old path must not be displaced again.
    replay = await engine.execute(database.runs[run.id], config)

    assert (library / DEST).stat().st_size == 32
    assert (inbound / TRASH_DIR / "run-1" / "library" / DEST).stat().st_size == 16
    assert replay.replaced == ()
    assert replay.duplicates == ()


async def test_half_completed_replacement_resumes(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", size=32)
    # The crash happened after the old file was trashed.
    make_files(inbound, f"{TRASH_DIR}/run-1/library/{DEST}", size=16)
    engine, database = executor
    run = replacement_run()
    database.runs[run.id] = run

    result = await engine.execute(run, config)

    assert (library / DEST).stat().st_size == 32
    assert result.moved == 1
    assert result.replaced == ()  # already trashed by the pre-crash pass


async def test_discarded_duplicate_lands_in_trash_not_fail(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", size=16)
    engine, database = executor
    source = f"{FOLDER}/ep01.mkv"
    move = Move(
        kind=MoveKind.TRASH_DUPLICATE,
        source_root=Root.INBOUND,
        source_path=source,
        dest_root=Root.INBOUND,
        dest_path=f"{TRASH_DIR}/run-1/inbound/{source}",
        candidate_id="V1",
    )
    run = build_run(move)
    database.runs[run.id] = run

    result = await engine.execute(run, config)

    assert (inbound / TRASH_DIR / "run-1" / "inbound" / source).is_file()
    assert not (inbound / "fail").exists()
    assert result.discarded == ("ep01.mkv",)
    assert result.moved == 0


async def test_revert_restores_both_sides_of_a_replacement(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", size=32)
    make_files(library, DEST, size=16)
    engine, database = executor
    run = replacement_run()
    database.runs[run.id] = run
    await engine.execute(run, config)

    await engine.revert(database.runs[run.id], config)

    assert (library / DEST).stat().st_size == 16
    assert (inbound / FOLDER / "ep01.mkv").stat().st_size == 32
    # The emptied trash area cleans up after itself.
    assert not (inbound / TRASH_DIR).exists()


async def test_revert_after_purge_skips_the_lost_file(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", size=32)
    make_files(library, DEST, size=16)
    engine, database = executor
    run = replacement_run()
    database.runs[run.id] = run
    await engine.execute(run, config)
    purge_run_trash(inbound, "run-1")

    await engine.revert(database.runs[run.id], config)

    # The new file went back to the inbound folder; the purged old version
    # is gone and its restore was skipped rather than failing the revert.
    assert (inbound / FOLDER / "ep01.mkv").stat().st_size == 32
    assert not (library / DEST).exists()


async def test_extra_move_without_a_base_is_refused(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    engine, database = executor
    run = build_run(trash_replaced("Show/ep01.mp4", root=Root.EXTRA))
    database.runs[run.id] = run
    with pytest.raises(ExecutionError):
        await engine.execute(run, config)


async def test_trash_cannot_escape_through_a_symlink(
    config: WatchConfig, roots: tuple[Path, Path], executor, tmp_path: Path
) -> None:
    inbound, _ = roots
    extra = tmp_path / "anirss"
    outside = tmp_path / "outside"
    outside.mkdir()
    make_files(extra, "Show/ep01.mp4")
    (inbound / TRASH_DIR).symlink_to(outside)
    engine, database = executor
    run = build_run(
        trash_replaced("Show/ep01.mp4", root=Root.EXTRA, extra_base=str(extra))
    )
    database.runs[run.id] = run

    with pytest.raises(ExecutionError):
        await engine.execute(run, config)
    assert (extra / "Show/ep01.mp4").is_file()


async def test_a_missing_trash_source_leaves_no_empty_skeleton(
    config: WatchConfig, roots: tuple[Path, Path], executor
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "ep01.mkv", size=32)
    # The library file the plan wants to displace is already gone.
    engine, database = executor
    run = replacement_run()
    database.runs[run.id] = run

    result = await engine.execute(run, config)

    assert result.missing == ("Show S01E01.mkv",)
    assert (library / DEST).stat().st_size == 32
    assert not (inbound / TRASH_DIR).exists()
