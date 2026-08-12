from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from reeloom.models import (
    ExecutedMove,
    MediaIdentity,
    MediaType,
    Move,
    MoveKind,
    MoveOutcome,
    Plan,
    Root,
    Run,
    RunResult,
    RunState,
    WatchConfig,
)
from reeloom.scanner import StabilityTracker
from reeloom.server.worker import NeedsAttention, Worker
from tests.conftest import make_files
from tests.fakes import FakeDatabase, RecordingNotifier

IDENTITY = MediaIdentity(
    media_type=MediaType.ANIME, tmdb_id=1, title="Show", year=2024
)


class StubIdentifier:
    def __init__(self, plan: Plan | None = None, error: Exception | None = None):
        self.plan = plan or Plan(identity=IDENTITY, moves=())
        self.error = error
        self.calls = 0

    async def identify(self, run: Run, config: WatchConfig) -> Plan:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.plan


class StubExecutor:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.reverted: list[str] = []
        self.discarded: list[str] = []

    async def execute(self, run: Run, config: WatchConfig) -> RunResult:
        self.executed.append(run.id)
        return RunResult(moved=3)

    async def revert(self, run: Run, config: WatchConfig) -> None:
        self.reverted.append(run.id)

    async def discard(self, run: Run, config: WatchConfig) -> int:
        self.discarded.append(run.id)
        return 1


def build(config: WatchConfig, **kwargs):
    database = FakeDatabase([config])
    worker = Worker(
        database,
        identifier=kwargs.pop("identifier", StubIdentifier()),
        executor=kwargs.pop("executor", StubExecutor()),
        tracker=StabilityTracker(clock=lambda: 10_000.0),
        **kwargs,
    )
    return database, worker


async def drain(worker: Worker, limit: int = 12) -> None:
    for _ in range(limit):
        if not await worker.tick():
            return


async def test_stable_folder_with_video_becomes_a_run(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    database, worker = build(config)

    await worker.scan()

    assert [run.folder_name for run in database.runs.values()] == ["Show"]


async def test_folder_without_video_is_ignored(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Docs", "readme.txt")
    database, worker = build(config)

    await worker.scan()

    assert database.runs == {}


async def test_rescan_does_not_open_a_second_run_for_the_same_folder(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    database, worker = build(config)

    await worker.scan()
    await worker.scan()

    assert len(database.runs) == 1


async def test_folder_left_behind_by_a_settled_run_is_not_reopened(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    identifier = StubIdentifier(error=RuntimeError("boom"))
    database, worker = build(config, identifier=identifier)

    await drain(worker)
    await worker.scan()

    assert len(database.runs) == 1


async def test_settling_folder_is_reported_with_its_remaining_wait(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    database, worker = build(replace(config, stability_seconds=120))

    await worker.scan()

    assert database.runs == {}
    [folder] = worker.intake_status()
    assert folder.folder_name == "Show"
    assert folder.status == "settling"
    assert folder.remaining_seconds == 120.0
    assert folder.file_count == 1


async def test_empty_folder_is_reported_as_waiting_for_files(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    (inbound / "Show").mkdir()
    database, worker = build(config)

    await worker.scan()

    assert database.runs == {}
    [folder] = worker.intake_status()
    assert folder.status == "empty"
    assert folder.remaining_seconds is None


async def test_skipped_folders_are_reported_with_their_reason(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Docs", "readme.txt")
    _, worker = build(config)

    await worker.scan()

    [folder] = worker.intake_status()
    assert folder.status == "skipped"
    assert folder.reason == "no_video"


async def test_folder_that_became_a_run_leaves_the_intake_report(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    _, worker = build(config)

    await worker.scan()
    assert worker.intake_status() == []

    # The open run keeps the folder out of the report on later scans too.
    await worker.scan()
    assert worker.intake_status() == []


async def test_changed_content_opens_a_fresh_run_for_the_same_name(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    identifier = StubIdentifier(error=RuntimeError("boom"))
    database, worker = build(config, identifier=identifier)
    await drain(worker)

    make_files(inbound / "Show", "ep02.mkv")
    await worker.scan()

    assert len(database.runs) == 2


async def test_run_reaches_done_through_identify_and_execute(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    executor = StubExecutor()
    notifier = RecordingNotifier()
    database, worker = build(config, executor=executor, notifier=notifier)

    await drain(worker)

    run = next(iter(database.runs.values()))
    assert run.state is RunState.DONE
    assert run.plan is not None
    assert run.result == RunResult(moved=3)
    assert executor.executed == [run.id]
    assert [sent.id for sent in notifier.sent] == [run.id]


async def test_identification_failure_parks_the_run_for_a_human(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    identifier = StubIdentifier(error=NeedsAttention("ambiguous_title", hits=3))
    notifier = RecordingNotifier()
    database, worker = build(config, identifier=identifier, notifier=notifier)

    await drain(worker)

    run = next(iter(database.runs.values()))
    assert run.state is RunState.NEEDS_ATTENTION
    assert run.error == {"code": "ambiguous_title", "hits": 3}
    assert notifier.sent


async def test_unexpected_error_fails_the_run_without_touching_others(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    identifier = StubIdentifier(error=RuntimeError("boom"))
    database, worker = build(config, identifier=identifier)

    await drain(worker)

    run = next(iter(database.runs.values()))
    assert run.state is RunState.FAILED
    assert run.error is not None and run.error["detail"] == "boom"


async def test_missing_credentials_park_the_run_instead_of_failing_it(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    from reeloom.models import Deferred

    identifier = StubIdentifier(error=Deferred("model_not_configured"))
    database, worker = build(config, identifier=identifier)

    await drain(worker)

    run = next(iter(database.runs.values()))
    assert run.state is RunState.PENDING
    assert run.attempts == 0

    # Once configured, the parked run proceeds without any manual retry.
    identifier.error = None
    await drain(worker)
    assert database.runs[run.id].state is RunState.DONE


async def test_recover_rearms_interrupted_identification(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    database, worker = build(config)
    await worker.scan()
    run_id = next(iter(database.runs))
    await database.set_state(run_id, RunState.IDENTIFYING)

    await worker.recover()

    assert database.runs[run_id].state is RunState.PENDING


async def test_reverting_run_replays_into_execution(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    executor = StubExecutor()
    database, worker = build(config, executor=executor)
    await worker.scan()
    run_id = next(iter(database.runs))
    database.runs[run_id] = replace(
        database.runs[run_id],
        state=RunState.REVERTING,
        plan=Plan(identity=IDENTITY, moves=()),
    )

    await drain(worker)

    assert executor.reverted == [run_id]
    assert executor.executed == [run_id]
    assert database.runs[run_id].state is RunState.DONE
    assert database.runs[run_id].executed_moves == ()


async def test_discarding_an_executed_run_reverts_the_layout_first(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    executor = StubExecutor()
    database, worker = build(config, executor=executor)
    await worker.scan()
    run_id = next(iter(database.runs))
    database.runs[run_id] = replace(
        database.runs[run_id],
        state=RunState.DISCARDING,
        executed_moves=(
            ExecutedMove(
                Move(
                    kind=MoveKind.MEDIA,
                    source_root=Root.INBOUND,
                    source_path="Show/ep01.mkv",
                    dest_root=Root.LIBRARY,
                    dest_path="Show (2024) {tmdb-1}/S01/Show S01E01.mkv",
                ),
                MoveOutcome.MOVED,
            ),
        ),
    )

    await drain(worker)

    assert executor.reverted == [run_id]
    assert executor.discarded == [run_id]
    assert database.runs[run_id].state is RunState.DISCARDED
    assert database.runs[run_id].executed_moves == ()


async def test_discarding_an_unexecuted_run_skips_the_revert(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    executor = StubExecutor()
    database, worker = build(config, executor=executor)
    await worker.scan()
    run_id = next(iter(database.runs))
    database.runs[run_id] = replace(
        database.runs[run_id], state=RunState.DISCARDING
    )

    await drain(worker)

    assert executor.reverted == []
    assert executor.discarded == [run_id]
    assert database.runs[run_id].state is RunState.DISCARDED


async def test_disabled_config_is_not_scanned(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    database, worker = build(replace(config, enabled=False))

    await worker.scan()

    assert database.runs == {}


async def test_subtitle_stage_runs_only_for_anime_with_the_flag_on(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")

    class StubSubtitles:
        def __init__(self) -> None:
            self.calls = 0

        async def acquire(self, run, config, result):
            self.calls += 1
            return replace(result, subtitles_acquired=2)

    subtitles = StubSubtitles()
    database, worker = build(
        replace(config, acquire_subtitles=True), subtitles=subtitles
    )

    await drain(worker)

    run = next(iter(database.runs.values()))
    assert subtitles.calls == 1
    assert run.result is not None and run.result.subtitles_acquired == 2
    assert run.state is RunState.DONE


async def test_subtitle_failure_never_blocks_a_finished_run(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")

    class FailingSubtitles:
        async def acquire(self, run, config, result):
            raise RuntimeError("acgrip down")

    database, worker = build(
        replace(config, acquire_subtitles=True), subtitles=FailingSubtitles()
    )

    await drain(worker)

    run = next(iter(database.runs.values()))
    assert run.state is RunState.DONE
    assert run.result is not None
    assert "acgrip down" in run.result.subtitle_note


# ---- version replacement routing ----------------------------------------


class StubComparer:
    def __init__(self, plan: Plan | None = None, error: Exception | None = None):
        self.plan = plan
        self.error = error
        self.calls = 0

    async def compare(self, run: Run, config: WatchConfig) -> Plan | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.plan


def replace_config(config: WatchConfig) -> WatchConfig:
    return replace(config, replace_enabled=True)


async def test_replace_enabled_run_compares_before_executing(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    comparer = StubComparer()
    database, worker = build(replace_config(config), comparer=comparer)

    await drain(worker)

    run = next(iter(database.runs.values()))
    assert run.state is RunState.DONE
    assert comparer.calls == 1


async def test_compare_is_skipped_when_replacement_is_off(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    comparer = StubComparer()
    database, worker = build(config, comparer=comparer)

    await drain(worker)

    run = next(iter(database.runs.values()))
    assert run.state is RunState.DONE
    assert comparer.calls == 0


async def test_comparer_augmentation_replaces_the_stored_plan(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    augmented = Plan(identity=IDENTITY, moves=(), notes="augmented")
    comparer = StubComparer(plan=augmented)
    database, worker = build(replace_config(config), comparer=comparer)

    await drain(worker)

    run = next(iter(database.runs.values()))
    assert run.plan is not None and run.plan.notes == "augmented"
    assert run.state is RunState.DONE


async def test_comparer_parks_the_run_for_confirmation(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    notifier = RecordingNotifier()
    comparer = StubComparer(
        error=NeedsAttention("replace_confirmation", groups=[])
    )
    database, worker = build(
        replace_config(config), comparer=comparer, notifier=notifier
    )

    await drain(worker)

    run = next(iter(database.runs.values()))
    assert run.state is RunState.NEEDS_ATTENTION
    assert run.error is not None and run.error["code"] == "replace_confirmation"
    assert notifier.sent


async def test_toggled_off_mid_run_falls_through_to_executing(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    comparer = StubComparer()
    database, worker = build(config, comparer=comparer)
    run = Run(
        id="run-1",
        config_id=config.id,
        folder_name="Show",
        state=RunState.COMPARING,
        plan=Plan(identity=IDENTITY, moves=()),
    )
    database.runs[run.id] = run

    await drain(worker)

    assert database.runs[run.id].state is RunState.DONE
    assert comparer.calls == 0


async def test_revert_routes_back_through_comparing(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    comparer = StubComparer()
    executor = StubExecutor()
    database, worker = build(
        replace_config(config), comparer=comparer, executor=executor
    )
    executed = ExecutedMove(
        Move(
            kind=MoveKind.MEDIA,
            source_root=Root.INBOUND,
            source_path="Show/ep01.mkv",
            dest_root=Root.LIBRARY,
            dest_path="Show (2024) {tmdb-1}/S01/Show S01E01.mkv",
        ),
        MoveOutcome.MOVED,
    )
    run = Run(
        id="run-1",
        config_id=config.id,
        folder_name="Show",
        state=RunState.REVERTING,
        plan=Plan(identity=IDENTITY, moves=()),
        executed_moves=(executed,),
    )
    database.runs[run.id] = run

    await drain(worker)

    assert executor.reverted == ["run-1"]
    assert comparer.calls == 1
    assert database.runs[run.id].state is RunState.DONE


# ---- trash purging -------------------------------------------------------


import os
import time as time_module
import uuid as uuid_module

from reeloom.trash import TRASH_DIR


def drop_trash(root: Path, run_id: str, *, age_days: float = 10.0) -> Path:
    path = root / TRASH_DIR / run_id / "old.mkv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * 8)
    stamp = time_module.time() - age_days * 86400
    os.utime(path.parent, (stamp, stamp))
    return path


def settled_run(config: WatchConfig, run_id: str, state: RunState) -> Run:
    return Run(
        id=run_id,
        config_id=config.id,
        folder_name="Show",
        state=state,
    )


async def test_purge_removes_expired_trash_of_settled_runs(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    _, library = roots
    database, worker = build(config)
    run_id = str(uuid_module.uuid4())
    database.runs[run_id] = settled_run(config, run_id, RunState.DONE)
    path = drop_trash(library, run_id)

    await worker._purge_pass()

    assert not path.exists()
    assert not (library / TRASH_DIR).exists()


async def test_purge_keeps_recent_and_active_trash(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, library = roots
    database, worker = build(config)
    fresh_id = str(uuid_module.uuid4())
    active_id = str(uuid_module.uuid4())
    database.runs[fresh_id] = settled_run(config, fresh_id, RunState.DONE)
    database.runs[active_id] = settled_run(
        config, active_id, RunState.REVERTING
    )
    fresh = drop_trash(library, fresh_id, age_days=1.0)
    active = drop_trash(inbound, active_id, age_days=30.0)

    await worker._purge_pass()

    assert fresh.exists()  # inside the retention window
    assert active.exists()  # its run is still active


async def test_purge_covers_orphans_and_leaves_foreign_dirs(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    _, library = roots
    database, worker = build(config)
    orphan = drop_trash(library, str(uuid_module.uuid4()))  # run deleted
    foreign = drop_trash(library, "not-a-run-id")

    await worker._purge_pass()

    assert not orphan.exists()
    assert foreign.exists()  # never delete what reeloom did not write


async def test_purge_reaches_extra_dirs(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path
) -> None:
    extra = tmp_path / "anirss"
    extra.mkdir()
    rconfig = replace(
        config, replace_enabled=True, replace_extra_dirs=(str(extra),)
    )
    database, worker = build(rconfig)
    run_id = str(uuid_module.uuid4())
    database.runs[run_id] = settled_run(rconfig, run_id, RunState.DONE)
    path = drop_trash(extra, run_id)

    await worker._purge_pass()

    assert not path.exists()


async def test_retention_zero_purges_when_the_run_settles(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, library = roots
    make_files(inbound / "Show", "ep01.mkv")
    database, worker = build(config)
    database.settings["trash_retention_days"] = 0

    await worker.scan()
    run_id = next(iter(database.runs))
    trash = drop_trash(library, run_id, age_days=0.0)
    await drain(worker)

    assert database.runs[run_id].state is RunState.DONE
    assert not trash.exists()


async def test_purge_passes_are_rate_limited(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    _, library = roots
    database, worker = build(config)
    run_id = str(uuid_module.uuid4())
    database.runs[run_id] = settled_run(config, run_id, RunState.DONE)

    await worker._maybe_purge()
    path = drop_trash(library, run_id)
    await worker._maybe_purge()  # inside the hourly window: no pass runs

    assert path.exists()
