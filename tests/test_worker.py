from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from reeloom.models import (
    MediaIdentity,
    MediaType,
    Plan,
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

    async def execute(self, run: Run, config: WatchConfig) -> RunResult:
        self.executed.append(run.id)
        return RunResult(moved=3)

    async def revert(self, run: Run, config: WatchConfig) -> None:
        self.reverted.append(run.id)


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
