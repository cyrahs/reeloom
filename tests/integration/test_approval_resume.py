from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reeloom.adapters.approval import FilesystemApprovalStore
from reeloom.adapters.event_store import FilesystemEventStore
from reeloom.adapters.filesystem import (
    FilesystemPlanCompiler,
    FilesystemScanner,
)
from reeloom.adapters.journal import FilesystemJournalStore
from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.executor.apply import (
    ApplyStatus,
    FilesystemExecutor,
)
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.candidates import CandidateSnapshot
from reeloom.kernel.inventory import ExistingInventory
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.naming import SeriesIdentity
from reeloom.kernel.rename_plan import RenamePlan
from reeloom.kernel.tmdb import TmdbCandidateRef, TmdbWorkType
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.runtime.events import (
    ApplyFailed,
    ApplyStarted,
    ApprovalRequested,
    CandidateSnapshotCreated,
    ExistingInventoryObserved,
    MappingSubmitted,
    MoveApplied,
    PlanApproved,
    PlanBuilt,
    RollbackCompleted,
    RunCompleted,
    RunStarted,
    RunStopped,
    SeriesSelected,
    TmdbCandidatesObserved,
    TmdbSeasonCatalogObserved,
    ToolRequested,
    ToolSucceeded,
)
from reeloom.runtime.resume import ApprovalResumeService
from reeloom.runtime.state import Phase, RunStatus, StopReason
from reeloom.runtime.store import InMemoryEventStore

_NOW = datetime(2026, 7, 24, 18, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Environment:
    source: Path
    output: Path
    plan: RenamePlan
    approval: ApprovalRecord
    runtime: FilesystemEventStore
    service: ApprovalResumeService


def test_approved_stopped_run_resumes_through_executor(
    tmp_path: Path,
) -> None:
    environment = _setup(tmp_path)

    result = environment.service.approve_and_apply(
        environment.approval
    )

    assert result.status is ApplyStatus.COMPLETED
    assert environment.runtime.state is not None
    assert environment.runtime.state.phase is Phase.COMPLETED
    assert environment.runtime.state.status is RunStatus.STOPPED
    assert not (environment.source / "episode.mkv").exists()
    destination = environment.output / Path(
        environment.plan.draft.moves[0].destination
    )
    assert destination.read_bytes() == b"video"
    assert tuple(
        type(stored.event)
        for stored in environment.runtime.events[-4:]
    ) == (
        PlanApproved,
        ApplyStarted,
        MoveApplied,
        RunCompleted,
    )


def test_crashed_apply_resumes_through_typed_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path)

    def crash_before_move_event(*args: object) -> None:
        del args
        raise KeyboardInterrupt

    monkeypatch.setattr(
        FilesystemJournalStore,
        "record_move",
        crash_before_move_event,
    )
    with pytest.raises(KeyboardInterrupt):
        environment.service.approve_and_apply(
            environment.approval
        )
    monkeypatch.undo()

    result = environment.service.recover()

    assert result.status is ApplyStatus.ROLLED_BACK
    assert environment.runtime.state is not None
    assert environment.runtime.state.phase is Phase.ROLLED_BACK
    assert (
        environment.source / "episode.mkv"
    ).read_bytes() == b"video"
    assert tuple(
        type(stored.event)
        for stored in environment.runtime.events[-2:]
    ) == (ApplyFailed, RollbackCompleted)


def test_crash_after_approval_issue_can_retry_same_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path)
    original_append = FilesystemEventStore.append

    def crash_before_plan_approved(
        store: FilesystemEventStore,
        event: object,
    ) -> object:
        if isinstance(event, PlanApproved):
            raise KeyboardInterrupt
        return original_append(store, event)  # type: ignore[arg-type]

    monkeypatch.setattr(
        FilesystemEventStore,
        "append",
        crash_before_plan_approved,
    )
    with pytest.raises(KeyboardInterrupt):
        environment.service.approve_and_apply(
            environment.approval
        )
    monkeypatch.undo()

    result = _restart_service(environment).approve_and_apply(
        environment.approval
    )

    assert result.status is ApplyStatus.COMPLETED


def test_crash_after_plan_approved_recovers_before_apply_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path)
    original_append = FilesystemEventStore.append

    def crash_before_apply_started(
        store: FilesystemEventStore,
        event: object,
    ) -> object:
        if isinstance(event, ApplyStarted):
            raise KeyboardInterrupt
        return original_append(store, event)  # type: ignore[arg-type]

    monkeypatch.setattr(
        FilesystemEventStore,
        "append",
        crash_before_apply_started,
    )
    with pytest.raises(KeyboardInterrupt):
        environment.service.approve_and_apply(
            environment.approval
        )
    monkeypatch.undo()

    result = _restart_service(environment).recover()

    assert result.status is ApplyStatus.COMPLETED


def test_crash_after_apply_started_recovers_before_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path)

    def crash_before_apply(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(FilesystemExecutor, "apply", crash_before_apply)
    with pytest.raises(KeyboardInterrupt):
        environment.service.approve_and_apply(
            environment.approval
        )
    monkeypatch.undo()

    result = _restart_service(environment).recover()

    assert result.status is ApplyStatus.COMPLETED


def test_crash_after_journal_recovers_before_approval_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path)

    def crash_before_claim(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(
        FilesystemApprovalStore,
        "claim",
        crash_before_claim,
    )
    with pytest.raises(KeyboardInterrupt):
        environment.service.approve_and_apply(
            environment.approval
        )
    monkeypatch.undo()

    result = _restart_service(environment).recover()

    assert result.status is ApplyStatus.COMPLETED


def test_crash_after_executor_terminal_recovers_domain_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _setup(tmp_path)

    def crash_before_domain_terminal(*args: object) -> None:
        del args
        raise KeyboardInterrupt

    monkeypatch.setattr(
        ApprovalResumeService,
        "_finish",
        crash_before_domain_terminal,
    )
    with pytest.raises(KeyboardInterrupt):
        environment.service.approve_and_apply(
            environment.approval
        )
    monkeypatch.undo()

    result = _restart_service(environment).recover()
    state = _restart_service(environment).runtime.state

    assert result.status is ApplyStatus.COMPLETED
    assert state is not None
    assert state.phase is Phase.COMPLETED


def _setup(tmp_path: Path) -> _Environment:
    source = tmp_path / "incoming"
    output = tmp_path / "anime"
    plans_root = tmp_path / "plans"
    approvals_root = tmp_path / "approvals"
    journals_root = tmp_path / "journals"
    events_root = tmp_path / "events"
    for root in (
        source,
        output,
        plans_root,
        approvals_root,
        journals_root,
        events_root,
    ):
        root.mkdir()
    (source / "episode.mkv").write_bytes(b"video")

    scan = FilesystemScanner().scan(AuthorizedRoot.create(source))
    mapping = MappingDraft.from_dict(
        {
            "videos": [
                {
                    "video_id": "video:1",
                    "season": 1,
                    "episode_start": 1,
                    "episode_end": 1,
                }
            ],
            "subtitles": [],
        },
        candidates=scan.snapshot.candidates,
        catalog=EpisodeCatalog.from_counts({1: 1}),
    )
    series = SeriesIdentity("正确动画", 2024, 200)
    compiler = FilesystemPlanCompiler(
        scan=scan,
        output_root=AuthorizedRoot.create(output),
    )
    plan = compiler.compile(
        run_id="run-resume",
        work_type=TmdbWorkType.ANIME,
        series=series,
        mapping=mapping,
        subtitle_variants=(),
        created_at=_NOW,
    )
    plans = FilesystemPlanStore(
        AuthorizedRoot.create(plans_root)
    )
    plans.save(plan)

    runtime = FilesystemEventStore(
        AuthorizedRoot.create(events_root),
        run_id=plan.run_id,
    )
    _populate_awaiting_runtime(
        runtime,
        plan,
        scan.snapshot.candidates,
    )
    approval = ApprovalRecord.create(
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
        scope=ApprovalScope.APPLY,
        expires_at=_NOW + timedelta(minutes=5),
        nonce="z" * 32,
    )
    approvals = FilesystemApprovalStore(
        AuthorizedRoot.create(approvals_root),
        clock=lambda: _NOW,
    )
    service = ApprovalResumeService(
        runtime=runtime,
        approvals=approvals,
        executor=FilesystemExecutor(
            plans=plans,
            approvals=approvals,
            journals=FilesystemJournalStore(
                AuthorizedRoot.create(journals_root)
            ),
        ),
    )
    return _Environment(
        source=source,
        output=output,
        plan=plan,
        approval=approval,
        runtime=runtime,
        service=service,
    )


def _restart_service(
    environment: _Environment,
) -> ApprovalResumeService:
    return ApprovalResumeService(
        runtime=FilesystemEventStore(
            environment.runtime.root,
            run_id=environment.plan.run_id,
        ),
        approvals=environment.service.approvals,
        executor=environment.service.executor,
    )


def _populate_awaiting_runtime(
    runtime: InMemoryEventStore | FilesystemEventStore,
    plan: RenamePlan,
    candidates: CandidateSnapshot,
) -> None:
    candidate_ids = tuple(
        candidate.id for candidate in candidates.candidates
    )
    runtime.append(
        RunStarted(
            run_id=plan.run_id,
            work_type=TmdbWorkType.ANIME,
        )
    )
    runtime.append(
        CandidateSnapshotCreated(
            snapshot_id=plan.candidate_snapshot_id,
            candidate_count=len(candidate_ids),
            candidate_ids=candidate_ids,
            source_root=plan.source_root,
            output_root=plan.output_root,
        )
    )
    runtime.append(
        TmdbCandidatesObserved(
            candidates=(
                TmdbCandidateRef(
                    work_type=TmdbWorkType.ANIME,
                    tmdb_id=200,
                ),
            )
        )
    )
    runtime.append(
        SeriesSelected(
            series=plan.draft.series,
            work_type=TmdbWorkType.ANIME,
        )
    )
    runtime.append(
        ToolRequested(
            call_id="season",
            tool_name="get_tmdb_season",
        )
    )
    runtime.append(
        TmdbSeasonCatalogObserved(
            call_id="season",
            tmdb_id=200,
            work_type=TmdbWorkType.ANIME,
            season_number=1,
            episode_count=1,
        )
    )
    runtime.append(
        ToolSucceeded(
            call_id="season",
            tool_name="get_tmdb_season",
        )
    )
    runtime.append(
        ToolRequested(
            call_id="inventory",
            tool_name="get_existing_inventory",
        )
    )
    runtime.append(
        ExistingInventoryObserved(
            call_id="inventory",
            tmdb_id=200,
            work_type=TmdbWorkType.ANIME,
            occupied=ExistingInventory(
                work_type=TmdbWorkType.ANIME,
                tmdb_id=200,
            ).occupied,
        )
    )
    runtime.append(
        ToolSucceeded(
            call_id="inventory",
            tool_name="get_existing_inventory",
        )
    )
    runtime.append(
        ToolRequested(
            call_id="mapping",
            tool_name="submit_mapping",
        )
    )
    runtime.append(
        MappingSubmitted(
            call_id="mapping",
            candidate_snapshot_id=plan.candidate_snapshot_id,
            mapping=plan.draft.mapping,
        )
    )
    runtime.append(
        ToolSucceeded(
            call_id="mapping",
            tool_name="submit_mapping",
        )
    )
    runtime.append(PlanBuilt(plan=plan))
    runtime.append(ApprovalRequested(plan_hash=plan.plan_hash))
    runtime.append(
        RunStopped(reason=StopReason.AWAITING_APPROVAL)
    )
