from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from reeloom.executor.forward import (
    ForwardExecutionItemResult,
    ForwardExecutionResult,
)
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.forward_execution import (
    ExecutionItemOutcome,
    ExecutionOperation,
    ExecutionOperationLease,
    RenamePlanV2,
    compile_plan_draft_v2,
)
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.naming import SeriesIdentity
from reeloom.kernel.semantic_identity import (
    SemanticCandidateSnapshot,
    SemanticRootBinding,
    SemanticSourceIdentity,
)
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.server.config import ApplyPolicy
from reeloom.server.forward_execution_service import (
    ForwardExecutionCoordinator,
    ForwardExecutionServiceError,
)
from reeloom.server.forward_operation_repository import (
    ForwardOperationError,
    ForwardOperationErrorCode,
    ForwardOperationView,
    execution_operation_id,
)

_NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def _plan() -> RenamePlanV2:
    source = SemanticSourceIdentity(
        candidate_id=CandidateId(CandidateKind.VIDEO, 1),
        kind=CandidateKind.VIDEO,
        relative_path=PurePosixPath("Work/episode.mkv"),
        size_bytes=1024,
    )
    snapshot = SemanticCandidateSnapshot.create((source,))
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
        candidates=snapshot.candidates,
        catalog=EpisodeCatalog.from_counts({1: 1}),
    )
    return RenamePlanV2.create(
        run_id="run:m14",
        config_revision=1,
        watch_id="watch:m14",
        work_type=TmdbWorkType.ANIME,
        created_at=_NOW,
        source_root=SemanticRootBinding(PurePosixPath("/incoming")),
        output_root=SemanticRootBinding(PurePosixPath("/library")),
        candidate_snapshot=snapshot,
        subtitle_variants=(),
        draft=compile_plan_draft_v2(
            series=SeriesIdentity("Series", 2026, 14),
            mapping=mapping,
            candidates=snapshot,
            subtitle_variants=(),
        ),
    )


class _Plans:
    def __init__(self, plan: RenamePlanV2) -> None:
        self.plan = plan

    def load(self, plan_hash: str) -> bytes:
        assert plan_hash == self.plan.plan_hash
        return self.plan.canonical_bytes()


class _Configs:
    def __init__(self, policy: ApplyPolicy) -> None:
        self.policy = policy

    def get(self, revision: int) -> object:
        assert revision == 1
        return SimpleNamespace(
            apply_policy=self.policy,
            watches=(SimpleNamespace(watch_id="watch:m14"),),
        )


class _Approvals:
    def __init__(self) -> None:
        self.issued = 0

    def issue_or_reuse(self, approval: object) -> object:
        self.issued += 1
        return approval


class _Operations:
    def __init__(self) -> None:
        self.operation: ExecutionOperation | None = None
        self.settled = 0
        self.rescans = 0

    def get(self, operation_id: str) -> ExecutionOperation:
        if self.operation is None:
            raise ForwardOperationError(
                ForwardOperationErrorCode.OPERATION_NOT_FOUND
            )
        assert operation_id == self.operation.operation_id
        return self.operation

    def authorize(
        self,
        operation: ExecutionOperation,
        **_: object,
    ) -> ExecutionOperation:
        self.operation = operation
        return operation

    def claim(
        self,
        operation_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> ExecutionOperationLease | None:
        assert self.operation is not None
        assert operation_id == self.operation.operation_id
        if self.operation.terminal:
            return None
        lease = ExecutionOperationLease.issue(
            self.operation,
            worker_id=worker_id,
            now=now,
            lease_for=lease_for,
        )
        self.operation = lease.operation
        return lease

    def settle_result(
        self,
        lease: ExecutionOperationLease,
        result: ForwardExecutionResult,
        **_: object,
    ) -> ExecutionOperation:
        assert result.operation.operation_id == lease.operation.operation_id
        self.operation = result.operation
        self.settled += 1
        return result.operation

    def get_view(self, operation_id: str) -> ForwardOperationView:
        return ForwardOperationView(operation=self.get(operation_id))

    def requeue_rescan(self, **_: object) -> None:
        assert self.operation is not None
        self.rescans += 1


class _Executor:
    def execute(
        self, plan: RenamePlanV2, lease: ExecutionOperationLease
    ) -> ForwardExecutionResult:
        operation = lease.settle(
            (ExecutionItemOutcome.SATISFIED,),
            now=_NOW + timedelta(seconds=1),
        )
        return ForwardExecutionResult(
            operation=operation,
            items=(
                ForwardExecutionItemResult(
                    plan.sources[0].candidate_id,
                    ExecutionItemOutcome.SATISFIED,
                ),
            ),
            warnings=(),
            fresh_scan_required=False,
        )


def _coordinator(
    policy: ApplyPolicy,
) -> tuple[ForwardExecutionCoordinator, _Approvals, _Operations, RenamePlanV2]:
    plan = _plan()
    approvals = _Approvals()
    operations = _Operations()
    coordinator = ForwardExecutionCoordinator(
        configs=_Configs(policy),  # type: ignore[arg-type]
        plans=_Plans(plan),  # type: ignore[arg-type]
        approvals=approvals,  # type: ignore[arg-type]
        operations=operations,  # type: ignore[arg-type]
        executor=_Executor(),  # type: ignore[arg-type]
        worker_id="worker:m14",
        clock=lambda: _NOW,
    )
    return coordinator, approvals, operations, plan


def test_manual_execute_consumes_one_approval_and_replays_terminal() -> None:
    coordinator, approvals, operations, plan = _coordinator(
        ApplyPolicy.MANUAL
    )

    first = coordinator.execute_manual(
        run_id=plan.run_id, plan_hash=plan.plan_hash
    )
    replay = coordinator.execute_manual(
        run_id=plan.run_id, plan_hash=plan.plan_hash
    )

    assert first.operation.terminal
    assert replay.operation == first.operation
    assert replay.result is None
    assert approvals.issued == 1
    assert operations.settled == 1


def test_browser_cannot_select_automatic_policy() -> None:
    coordinator, approvals, operations, plan = _coordinator(
        ApplyPolicy.AUTOMATIC
    )

    with pytest.raises(ForwardExecutionServiceError, match="policy_mismatch"):
        coordinator.execute_manual(
            run_id=plan.run_id, plan_hash=plan.plan_hash
        )

    result = coordinator.execute_automatic(
        run_id=plan.run_id, plan_hash=plan.plan_hash
    )
    assert result.operation.terminal
    assert approvals.issued == 1
    assert operations.settled == 1


def test_plan_only_never_authorizes_forward_operation() -> None:
    coordinator, approvals, operations, plan = _coordinator(
        ApplyPolicy.PLAN_ONLY
    )

    with pytest.raises(ForwardExecutionServiceError, match="plan_only"):
        coordinator.reconcile(
            run_id=plan.run_id, plan_hash=plan.plan_hash
        )

    assert approvals.issued == 0
    assert operations.operation is None


def test_rescan_reuses_terminal_operation_without_new_approval() -> None:
    coordinator, approvals, operations, plan = _coordinator(
        ApplyPolicy.MANUAL
    )
    operations.operation = ExecutionOperation.restore(
        schema_version="2",
        operation_id=execution_operation_id(
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
        ),
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
        status="stale",
        attempt_count=1,
        outcomes=("stale",),
    )

    view = coordinator.request_rescan(
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
    )

    assert view.operation is operations.operation
    assert operations.rescans == 1
    assert approvals.issued == 0


def test_rescan_rejects_nonterminal_operation() -> None:
    coordinator, _approvals, operations, plan = _coordinator(
        ApplyPolicy.MANUAL
    )
    operations.operation = ExecutionOperation.authorized(
        operation_id=execution_operation_id(
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
        ),
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
    )

    with pytest.raises(
        ForwardExecutionServiceError, match="rescan_not_allowed"
    ):
        coordinator.request_rescan(
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
        )

    assert operations.rescans == 0
