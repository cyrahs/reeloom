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


async def test_retrying_an_executed_run_keeps_the_recorded_summary(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")

    class FlakySubtitles:
        def __init__(self) -> None:
            self.calls = 0

        async def acquire(self, run, config, result):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("acgrip down")
            return replace(result, subtitles_acquired=1, subtitle_note="")

    ledger_move = ExecutedMove(
        Move(
            kind=MoveKind.MEDIA,
            source_root=Root.INBOUND,
            source_path="Show/ep01.mkv",
            dest_root=Root.LIBRARY,
            dest_path="Show (2024) {tmdb-1}/S01/Show S01E01.mkv",
        ),
        MoveOutcome.MOVED,
    )

    class IdempotentExecutor(StubExecutor):
        async def execute(self, run: Run, config: WatchConfig) -> RunResult:
            self.executed.append(run.id)
            if run.executed_moves:
                # The replay pass finds everything already done.
                return RunResult()
            await database.append_executed(run.id, ledger_move)
            return RunResult(
                moved=3,
                archived=2,
                duplicates=("dup.mkv",),
                missing=("gone.mkv",),
            )

    subtitles = FlakySubtitles()
    database, worker = build(
        replace(config, acquire_subtitles=True),
        executor=IdempotentExecutor(),
        subtitles=subtitles,
    )
    await drain(worker)
    run_id = next(iter(database.runs))
    settled = database.runs[run_id]
    assert settled.state is RunState.DONE
    assert settled.result is not None and settled.result.moved == 3

    # The retry endpoint re-enters EXECUTING; the replay must keep the
    # summary while the subtitle stage gets its second chance.
    await database.set_state(run_id, RunState.EXECUTING)
    await drain(worker)

    retried = database.runs[run_id]
    assert retried.state is RunState.DONE
    assert subtitles.calls == 2
    assert retried.result == RunResult(
        moved=3,
        archived=2,
        duplicates=("dup.mkv",),
        # Missing files are re-derived every pass, not accumulated.
        missing=(),
        subtitles_acquired=1,
    )


# ---- rescanning before re-identification --------------------------------


class SnapshotRecordingIdentifier(StubIdentifier):
    """Remembers the candidate listing each identification saw."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.seen: list[tuple[str, ...]] = []

    async def identify(self, run: Run, config: WatchConfig) -> Plan:
        self.seen.append(tuple(item.relative_path for item in run.snapshot))
        return await super().identify(run, config)


async def test_retry_rescans_the_folder_before_reidentifying(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv", "other01.mkv")
    identifier = SnapshotRecordingIdentifier(
        error=NeedsAttention("agent_reported_problem", reason="two shows")
    )
    database, worker = build(config, identifier=identifier)
    await drain(worker)
    run_id = next(iter(database.runs))
    assert database.runs[run_id].state is RunState.NEEDS_ATTENTION

    # The human pulls the stray file out, then hits retry.
    (inbound / "Show" / "other01.mkv").unlink()
    identifier.error = None
    await database.set_state(run_id, RunState.PENDING)
    await drain(worker)

    run = database.runs[run_id]
    assert run.state is RunState.DONE
    assert identifier.seen == [("ep01.mkv", "other01.mkv"), ("ep01.mkv",)]
    assert [item.candidate_id for item in run.snapshot] == ["V1"]
    assert (
        run_id,
        "folder rescanned: 1 file(s), 0 added, 1 removed",
    ) in database.logs


async def test_unchanged_folder_is_not_logged_as_rescanned(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    identifier = StubIdentifier(error=NeedsAttention("ambiguous_title", hits=3))
    database, worker = build(config, identifier=identifier)
    await drain(worker)
    run_id = next(iter(database.runs))

    identifier.error = None
    await database.set_state(run_id, RunState.PENDING)
    await drain(worker)

    assert database.runs[run_id].state is RunState.DONE
    assert not any(
        stored_id == run_id and message.startswith("folder rescanned")
        for stored_id, message in database.logs
    )


async def test_revising_an_executed_run_keeps_its_original_snapshot(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    # After execution the intake folder is gone; the model must still see the
    # listing the plan was built from, not an empty (or missing) folder.
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    identifier = SnapshotRecordingIdentifier()
    database, worker = build(config, identifier=identifier)
    await drain(worker)
    run_id = next(iter(database.runs))
    assert database.runs[run_id].state is RunState.DONE
    (inbound / "Show" / "ep01.mkv").unlink()
    (inbound / "Show").rmdir()
    await database.append_executed(
        run_id,
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
    )

    # The revise endpoint re-enters IDENTIFYING directly.
    await database.set_state(run_id, RunState.IDENTIFYING)
    await drain(worker)

    assert identifier.seen == [("ep01.mkv",), ("ep01.mkv",)]
    assert database.runs[run_id].state is RunState.DONE


async def test_retry_of_a_vanished_folder_parks_the_run(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv")
    identifier = StubIdentifier(error=NeedsAttention("ambiguous_title", hits=3))
    database, worker = build(config, identifier=identifier)
    await drain(worker)
    run_id = next(iter(database.runs))

    (inbound / "Show" / "ep01.mkv").unlink()
    (inbound / "Show").rmdir()
    identifier.error = None
    await database.set_state(run_id, RunState.PENDING)
    await drain(worker)

    run = database.runs[run_id]
    assert run.state is RunState.NEEDS_ATTENTION
    assert run.error == {"code": "folder_missing", "folder": "Show"}
    assert identifier.calls == 1


async def test_retry_of_a_folder_left_without_video_parks_the_run(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    make_files(inbound / "Show", "ep01.mkv", "notes.txt")
    identifier = StubIdentifier(error=NeedsAttention("ambiguous_title", hits=3))
    database, worker = build(config, identifier=identifier)
    await drain(worker)
    run_id = next(iter(database.runs))

    (inbound / "Show" / "ep01.mkv").unlink()
    identifier.error = None
    await database.set_state(run_id, RunState.PENDING)
    await drain(worker)

    run = database.runs[run_id]
    assert run.state is RunState.NEEDS_ATTENTION
    assert run.error == {"code": "no_video", "folder": "Show"}


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
    inbound, _ = roots
    database, worker = build(config)
    run_id = str(uuid_module.uuid4())
    database.runs[run_id] = settled_run(config, run_id, RunState.DONE)
    path = drop_trash(inbound, run_id)

    await worker._purge_pass()

    assert not path.exists()
    assert not (inbound / TRASH_DIR).exists()


async def test_purge_keeps_recent_and_active_trash(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    database, worker = build(config)
    fresh_id = str(uuid_module.uuid4())
    active_id = str(uuid_module.uuid4())
    database.runs[fresh_id] = settled_run(config, fresh_id, RunState.DONE)
    database.runs[active_id] = settled_run(
        config, active_id, RunState.REVERTING
    )
    fresh = drop_trash(inbound, fresh_id, age_days=1.0)
    active = drop_trash(inbound, active_id, age_days=30.0)

    await worker._purge_pass()

    assert fresh.exists()  # inside the retention window
    assert active.exists()  # its run is still active


async def test_purge_covers_orphans_and_leaves_foreign_dirs(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    database, worker = build(config)
    orphan = drop_trash(inbound, str(uuid_module.uuid4()))  # run deleted
    foreign = drop_trash(inbound, "not-a-run-id")

    await worker._purge_pass()

    assert not orphan.exists()
    assert foreign.exists()  # never delete what reeloom did not write


async def test_purge_never_reaches_outside_the_watch_roots(
    config: WatchConfig, roots: tuple[Path, Path], tmp_path: Path
) -> None:
    """Trash lives only under the watch root now; a stray trash directory in
    the library or an extra dir is not reeloom's to delete."""

    _, library = roots
    extra = tmp_path / "anirss"
    extra.mkdir()
    rconfig = replace(
        config, replace_enabled=True, replace_extra_dirs=(str(extra),)
    )
    database, worker = build(rconfig)
    run_id = str(uuid_module.uuid4())
    database.runs[run_id] = settled_run(rconfig, run_id, RunState.DONE)
    stray_library = drop_trash(library, run_id)
    stray_extra = drop_trash(extra, run_id)

    await worker._purge_pass()

    assert stray_library.exists()
    assert stray_extra.exists()


async def test_retention_zero_purges_when_the_run_settles(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, library = roots
    make_files(inbound / "Show", "ep01.mkv")
    database, worker = build(config)
    database.settings["trash_retention_days"] = 0

    await worker.scan()
    run_id = next(iter(database.runs))
    trash = drop_trash(inbound, run_id, age_days=0.0)
    await drain(worker)

    assert database.runs[run_id].state is RunState.DONE
    assert not trash.exists()


async def test_purge_passes_are_rate_limited(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, _ = roots
    database, worker = build(config)
    run_id = str(uuid_module.uuid4())
    database.runs[run_id] = settled_run(config, run_id, RunState.DONE)

    await worker._maybe_purge()
    path = drop_trash(inbound, run_id)
    await worker._maybe_purge()  # inside the hourly window: no pass runs

    assert path.exists()


# ---- daily subtitle recheck ----------------------------------------------


from datetime import datetime, timedelta, timezone

from reeloom.server.worker import SUBTITLE_RECHECK_INTERVAL_SECONDS

NOTE_MISSING = "未找到合适的字幕发布"


def finished_anime_run(
    config: WatchConfig,
    run_id: str = "run-1",
    *,
    note: str = NOTE_MISSING,
    settled_days_ago: float = 2.0,
    created_days_ago: float | None = None,
    extra: dict | None = None,
) -> Run:
    now = datetime.now(timezone.utc)
    return Run(
        id=run_id,
        config_id=config.id,
        folder_name=f"Show {run_id}",
        state=RunState.DONE,
        plan=Plan(identity=IDENTITY, moves=()),
        result=RunResult(moved=12, subtitle_note=note),
        extra=extra or {},
        created_at=now - timedelta(days=created_days_ago or settled_days_ago),
        updated_at=now - timedelta(days=settled_days_ago),
    )


class RecheckSubtitles:
    """Finds ``found`` subtitles on every pass."""

    def __init__(self, found: int) -> None:
        self.found = found
        self.calls: list[str] = []

    async def acquire(self, run, config, result):
        self.calls.append(run.id)
        if not self.found:
            return result
        return replace(
            result,
            subtitles_acquired=result.subtitles_acquired + self.found,
            subtitle_note="",
        )


def recheck_config(config: WatchConfig) -> WatchConfig:
    return replace(config, acquire_subtitles=True)


async def test_a_daily_recheck_finds_subtitles_and_announces_them(
    config: WatchConfig,
) -> None:
    config = recheck_config(config)
    subtitles = RecheckSubtitles(found=12)
    notifier = RecordingNotifier()
    database, worker = build(config, subtitles=subtitles, notifier=notifier)
    database.runs["run-1"] = finished_anime_run(config)

    assert await worker._maybe_recheck_subtitles() is True
    armed = database.runs["run-1"]
    assert armed.state is RunState.ACQUIRING_SUBS
    assert armed.extra["subtitle_recheck"]["pending"] is True

    await drain(worker)

    run = database.runs["run-1"]
    assert subtitles.calls == ["run-1"]
    assert run.state is RunState.DONE
    assert run.result is not None
    assert run.result.subtitles_acquired == 12
    assert run.result.subtitle_note == ""
    assert run.result.moved == 12
    record = run.extra["subtitle_recheck"]
    assert record["count"] == 1 and "pending" not in record
    # Announced as a subtitle arrival, not as a second "organized" message.
    assert [item.id for item in notifier.rechecked] == ["run-1"]
    assert notifier.sent == []


async def test_a_recheck_that_finds_nothing_stays_quiet(
    config: WatchConfig,
) -> None:
    config = recheck_config(config)
    subtitles = RecheckSubtitles(found=0)
    notifier = RecordingNotifier()
    database, worker = build(config, subtitles=subtitles, notifier=notifier)
    database.runs["run-1"] = finished_anime_run(config)

    assert await worker._maybe_recheck_subtitles() is True
    await drain(worker)

    run = database.runs["run-1"]
    assert subtitles.calls == ["run-1"]
    assert run.state is RunState.DONE
    assert run.result is not None and run.result.subtitle_note == NOTE_MISSING
    assert notifier.sent == [] and notifier.rechecked == []
    messages = [entry["message"] for entry in await database.list_logs("run-1")]
    assert any("subtitle recheck #1" in message for message in messages)
    assert any("found nothing" in message for message in messages)
    # The record keeps the clock, so the next look is a day away.
    assert run.extra["subtitle_recheck"]["count"] == 1


async def test_a_run_is_rechecked_at_most_once_a_day(
    config: WatchConfig,
) -> None:
    config = recheck_config(config)
    database, worker = build(config, subtitles=RecheckSubtitles(found=1))
    database.runs["fresh"] = finished_anime_run(
        config, "fresh", settled_days_ago=0.5
    )
    database.runs["seen"] = finished_anime_run(
        config,
        "seen",
        extra={
            "subtitle_recheck": {
                "count": 3,
                "last_at": time_module.time()
                - SUBTITLE_RECHECK_INTERVAL_SECONDS / 2,
            }
        },
    )

    assert await worker._maybe_recheck_subtitles() is False
    assert all(run.state is RunState.DONE for run in database.runs.values())


@pytest.mark.parametrize(
    "reason",
    ["too_old", "switched_off", "nothing_missing", "acquire_off", "movie", "disabled"],
)
async def test_runs_outside_the_recheck_scope_are_left_alone(
    config: WatchConfig, reason: str
) -> None:
    config = recheck_config(config)
    run = finished_anime_run(config)
    settings: dict = {}
    if reason == "too_old":
        run = finished_anime_run(config, created_days_ago=40.0)
    elif reason == "switched_off":
        settings = {"subtitle_recheck_days": 0}
    elif reason == "nothing_missing":
        run = finished_anime_run(config, note="")
    elif reason == "acquire_off":
        config = replace(config, acquire_subtitles=False)
    elif reason == "movie":
        config = replace(config, media_type=MediaType.MOVIE)
    elif reason == "disabled":
        config = replace(config, enabled=False)
    database, worker = build(config, subtitles=RecheckSubtitles(found=1))
    database.settings.update(settings)
    database.runs[run.id] = run

    assert await worker._maybe_recheck_subtitles() is False
    assert database.runs[run.id].state is RunState.DONE


async def test_rechecks_go_one_at_a_time_and_never_beside_active_work(
    config: WatchConfig,
) -> None:
    config = recheck_config(config)
    subtitles = RecheckSubtitles(found=1)
    database, worker = build(config, subtitles=subtitles)
    database.runs["newer"] = finished_anime_run(
        config, "newer", settled_days_ago=2.0
    )
    database.runs["older"] = finished_anime_run(
        config, "older", settled_days_ago=5.0
    )

    # The run that has waited longest goes first, alone.
    assert await worker._maybe_recheck_subtitles() is True
    assert database.runs["older"].state is RunState.ACQUIRING_SUBS
    assert database.runs["newer"].state is RunState.DONE

    # While it is being worked on, nothing else is armed.
    worker._last_subtitle_recheck = float("-inf")
    assert await worker._maybe_recheck_subtitles() is False
    assert database.runs["newer"].state is RunState.DONE

    await drain(worker)
    worker._last_subtitle_recheck = float("-inf")
    assert await worker._maybe_recheck_subtitles() is True
    assert database.runs["newer"].state is RunState.ACQUIRING_SUBS
    await drain(worker)
    assert subtitles.calls == ["older", "newer"]


async def test_recheck_passes_are_rate_limited(config: WatchConfig) -> None:
    config = recheck_config(config)
    database, worker = build(config, subtitles=RecheckSubtitles(found=1))

    assert await worker._maybe_recheck_subtitles() is False
    database.runs["run-1"] = finished_anime_run(config)
    # Inside the pass window: the new candidate waits for the next pass.
    assert await worker._maybe_recheck_subtitles() is False
    assert database.runs["run-1"].state is RunState.DONE


async def test_without_a_subtitle_service_nothing_is_rechecked(
    config: WatchConfig,
) -> None:
    config = recheck_config(config)
    database, worker = build(config)
    database.runs["run-1"] = finished_anime_run(config)

    assert await worker._maybe_recheck_subtitles() is False
    assert database.runs["run-1"].state is RunState.DONE


async def test_an_exhausted_window_warns_once_and_stops(
    config: WatchConfig,
) -> None:
    config = recheck_config(config)
    subtitles = RecheckSubtitles(found=1)
    notifier = RecordingNotifier()
    database, worker = build(config, subtitles=subtitles, notifier=notifier)
    database.runs["run-1"] = finished_anime_run(
        config,
        created_days_ago=40.0,
        settled_days_ago=40.0,
        extra={"subtitle_recheck": {"count": 30, "last_at": 0.0}},
    )

    assert await worker._maybe_recheck_subtitles() is False

    run = database.runs["run-1"]
    assert run.state is RunState.DONE
    assert subtitles.calls == []
    assert [item.id for item in notifier.given_up] == ["run-1"]
    assert notifier.sent == [] and notifier.rechecked == []
    record = run.extra["subtitle_recheck"]
    assert record["given_up"] is True and record["count"] == 30
    messages = [entry["message"] for entry in await database.list_logs("run-1")]
    assert any("gave up after 30 attempt(s)" in message for message in messages)

    # The next pass sees the mark and stays silent.
    worker._last_subtitle_recheck = float("-inf")
    assert await worker._maybe_recheck_subtitles() is False
    assert len(notifier.given_up) == 1


async def test_a_run_never_rechecked_before_expiry_gets_no_warning(
    config: WatchConfig,
) -> None:
    config = recheck_config(config)
    notifier = RecordingNotifier()
    database, worker = build(
        config, subtitles=RecheckSubtitles(found=1), notifier=notifier
    )
    database.runs["run-1"] = finished_anime_run(
        config, created_days_ago=40.0, settled_days_ago=40.0
    )

    assert await worker._maybe_recheck_subtitles() is False
    assert notifier.given_up == []
    assert "subtitle_recheck" not in database.runs["run-1"].extra


async def test_a_longer_window_resumes_a_given_up_run(
    config: WatchConfig,
) -> None:
    config = recheck_config(config)
    subtitles = RecheckSubtitles(found=1)
    database, worker = build(config, subtitles=subtitles)
    database.settings["subtitle_recheck_days"] = 60
    database.runs["run-1"] = finished_anime_run(
        config,
        created_days_ago=40.0,
        settled_days_ago=40.0,
        extra={
            "subtitle_recheck": {"count": 30, "last_at": 0.0, "given_up": True}
        },
    )

    assert await worker._maybe_recheck_subtitles() is True
    armed = database.runs["run-1"]
    assert armed.state is RunState.ACQUIRING_SUBS
    assert armed.extra["subtitle_recheck"] == {
        "count": 31,
        "last_at": armed.extra["subtitle_recheck"]["last_at"],
        "pending": True,
    }


async def test_recheck_status_reports_every_stage(config: WatchConfig) -> None:
    from reeloom.server.worker import subtitle_recheck_status

    config = recheck_config(config)
    database = FakeDatabase([config])
    database.runs["waiting"] = finished_anime_run(config, "waiting")
    searching = finished_anime_run(
        config,
        "searching",
        extra={"subtitle_recheck": {"count": 2, "last_at": 5.0, "pending": True}},
    )
    database.runs["searching"] = replace(searching, state=RunState.ACQUIRING_SUBS)
    database.runs["given-up"] = finished_anime_run(
        config,
        "given-up",
        created_days_ago=40.0,
        settled_days_ago=40.0,
        extra={"subtitle_recheck": {"count": 30, "last_at": 1.0, "given_up": True}},
    )
    # A first-pass acquisition is not a recheck, whatever its note says.
    database.runs["first-pass"] = replace(
        finished_anime_run(config, "first-pass"), state=RunState.ACQUIRING_SUBS
    )
    database.runs["satisfied"] = finished_anime_run(config, "satisfied", note="")

    now = time_module.time()
    items = {
        item.run.id: item
        for item in await subtitle_recheck_status(database, now=now, days=30)
    }

    assert set(items) == {"waiting", "searching", "given-up"}
    waiting = items["waiting"]
    assert waiting.status == "waiting" and waiting.count == 0
    assert waiting.next_at is not None and waiting.next_at < now
    assert items["searching"].status == "searching"
    assert items["searching"].next_at is None
    given_up = items["given-up"]
    assert given_up.status == "given_up" and given_up.warned is True
    assert given_up.deadline < now
    payload = waiting.to_json()
    assert payload["title"] == "Show" and payload["note"] == NOTE_MISSING
    assert payload["config_name"] == config.name

    assert await subtitle_recheck_status(database, now=now, days=0) == []
