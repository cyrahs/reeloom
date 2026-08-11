"""End-to-end revision: re-plan a finished run, undo it, apply the new plan."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from reeloom.executor import FilesystemExecutor
from reeloom.models import (
    MediaIdentity,
    MediaType,
    Move,
    MoveKind,
    Plan,
    Root,
    Run,
    RunState,
    WatchConfig,
)
from reeloom.naming import folder_name
from reeloom.scanner import StabilityTracker
from reeloom.server.worker import Worker
from tests.conftest import make_files
from tests.fakes import FakeDatabase

IDENTITY = MediaIdentity(MediaType.ANIME, 123, "Show", 2024)
FOLDER = "Drop"


def season_plan(season: int, *files: str) -> Plan:
    root = folder_name(IDENTITY)
    return Plan(
        identity=IDENTITY,
        moves=tuple(
            Move(
                kind=MoveKind.MEDIA,
                source_root=Root.INBOUND,
                source_path=f"{FOLDER}/{name}",
                dest_root=Root.LIBRARY,
                dest_path=(
                    f"{root}/S{season:02d}/Show S{season:02d}E{index:02d}.mkv"
                ),
                candidate_id=f"V{index}",
            )
            for index, name in enumerate(files, start=1)
        ),
    )


class PlanSequence:
    """Returns a different plan on each identification."""

    def __init__(self, *plans: Plan) -> None:
        self.plans = list(plans)
        self.calls = 0

    async def identify(self, run: Run, config: WatchConfig) -> Plan:
        self.calls += 1
        return self.plans.pop(0)


async def drain(worker: Worker, limit: int = 20) -> None:
    for _ in range(limit):
        if not await worker.tick():
            return


async def test_a_revised_run_is_reverted_and_reapplied(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "a.mkv", "b.mkv", "notes.txt")
    database = FakeDatabase([config])
    identifier = PlanSequence(
        season_plan(1, "a.mkv", "b.mkv"), season_plan(2, "a.mkv", "b.mkv")
    )
    worker = Worker(
        database,
        identifier=identifier,
        executor=FilesystemExecutor(database),
        tracker=StabilityTracker(clock=lambda: 1000.0),
    )
    await drain(worker)

    root = library / folder_name(IDENTITY)
    assert (root / "S01/Show S01E01.mkv").is_file()
    assert (inbound / "archive" / FOLDER / "notes.txt").is_file()
    run_id = next(iter(database.runs))
    assert database.runs[run_id].state is RunState.DONE

    # The user asks for season 2 instead.
    await database.add_interaction(run_id, "revision", "these are season 2")
    await database.set_state(run_id, RunState.IDENTIFYING)
    await drain(worker)

    run = database.runs[run_id]
    assert run.state is RunState.DONE
    assert identifier.calls == 2
    assert (root / "S02/Show S02E01.mkv").is_file()
    assert (root / "S02/Show S02E02.mkv").is_file()
    assert not (root / "S01/Show S01E01.mkv").exists()
    # Residue was returned to the folder and swept again under the new plan.
    assert (inbound / "archive" / FOLDER / "notes.txt").is_file()


async def test_revision_of_an_unexecuted_run_skips_the_revert(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "a.mkv")
    database = FakeDatabase([config])
    worker = Worker(
        database,
        identifier=PlanSequence(season_plan(1, "a.mkv")),
        executor=FilesystemExecutor(database),
        tracker=StabilityTracker(clock=lambda: 1000.0),
    )
    await worker.scan()
    run_id = next(iter(database.runs))
    await database.set_state(run_id, RunState.IDENTIFYING)

    await drain(worker)

    assert database.runs[run_id].state is RunState.DONE
    assert (library / folder_name(IDENTITY) / "S01/Show S01E01.mkv").is_file()


async def test_an_interrupted_revert_finishes_on_the_next_pass(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, library = roots
    make_files(inbound / FOLDER, "a.mkv", "b.mkv")
    database = FakeDatabase([config])
    executor = FilesystemExecutor(database)
    worker = Worker(
        database,
        identifier=PlanSequence(
            season_plan(1, "a.mkv", "b.mkv"), season_plan(2, "a.mkv", "b.mkv")
        ),
        executor=executor,
        tracker=StabilityTracker(clock=lambda: 1000.0),
    )
    await drain(worker)
    run_id = next(iter(database.runs))

    # Re-identify: the run now holds the season 2 plan and is about to revert.
    await database.set_state(run_id, RunState.IDENTIFYING)
    await worker.tick()
    run = database.runs[run_id]
    assert run.state is RunState.REVERTING

    # Undo only the first move, as if the process died partway through.
    await executor.revert(
        replace(run, executed_moves=run.executed_moves[:1]), config
    )

    await drain(worker)

    root = library / folder_name(IDENTITY)
    assert database.runs[run_id].state is RunState.DONE
    assert (root / "S02/Show S02E01.mkv").is_file()
    assert (root / "S02/Show S02E02.mkv").is_file()
    assert not (root / "S01").exists() or not any((root / "S01").iterdir())
