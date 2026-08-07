from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.forward_execution import (
    ExecutionItemOutcome,
    ExecutionOperation,
    ExecutionOperationLease,
    ExecutionOperationStatus,
    ForwardMoveDecision,
    PathObservationState,
    RenamePlanV2,
    decide_forward_move,
)
from reeloom.kernel.plan import PlannedMove
from reeloom.kernel.semantic_identity import SemanticSourceIdentity
from reeloom.ports.forward_filesystem import (
    ForwardFilesystem,
    ForwardMoveDiagnostic,
)

_EFFECT_MUTEX = threading.Lock()


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ForwardExecutionItemResult:
    source_id: CandidateId
    outcome: ExecutionItemOutcome
    diagnostic: ForwardMoveDiagnostic | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_id, CandidateId)
            or not isinstance(self.outcome, ExecutionItemOutcome)
            or (
                self.diagnostic is not None
                and not isinstance(
                    self.diagnostic, ForwardMoveDiagnostic
                )
            )
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)


@dataclass(frozen=True, slots=True)
class ForwardExecutionResult:
    operation: ExecutionOperation
    items: tuple[ForwardExecutionItemResult, ...]
    warnings: tuple[str, ...]
    fresh_scan_required: bool

    def __post_init__(self) -> None:
        if (
            not self.operation.terminal
            or not self.items
            or len({item.source_id for item in self.items})
            != len(self.items)
            or self.operation.outcomes
            != tuple(item.outcome for item in self.items)
            or not isinstance(self.warnings, tuple)
            or self.warnings != tuple(sorted(set(self.warnings)))
            or any(
                not isinstance(item, str)
                or not item
                or len(item.encode("utf-8")) > 128
                for item in self.warnings
            )
            or self.fresh_scan_required
            != (
                self.operation.status
                is not ExecutionOperationStatus.COMPLETED
            )
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)


@dataclass(frozen=True, slots=True)
class ForwardExecutor:
    filesystem: ForwardFilesystem
    clock: Callable[[], datetime] = _now
    sleeper: Callable[[float], None] = time.sleep
    observation_delays: tuple[float, ...] = (0.0, 0.05, 0.2, 0.5)

    def __post_init__(self) -> None:
        if (
            not self.observation_delays
            or len(self.observation_delays) > 8
            or self.observation_delays[0] != 0
            or any(
                not isinstance(item, (int, float))
                or isinstance(item, bool)
                or not 0 <= item <= 5
                for item in self.observation_delays
            )
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)

    def execute(
        self,
        plan: RenamePlanV2,
        lease: ExecutionOperationLease,
    ) -> ForwardExecutionResult:
        if (
            not isinstance(plan, RenamePlanV2)
            or not plan.verify_hash()
            or not isinstance(lease, ExecutionOperationLease)
            or lease.operation.run_id != plan.run_id
            or lease.operation.plan_hash != plan.plan_hash
            or not plan.draft.moves
        ):
            raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)
        items: list[ForwardExecutionItemResult] = []
        warnings: set[str] = set()
        for move in plan.draft.moves:
            result, move_warnings = self._execute_move(plan, move)
            items.append(result)
            warnings.update(move_warnings)
        operation = lease.settle(
            tuple(item.outcome for item in items),
            now=self.clock(),
        )
        return ForwardExecutionResult(
            operation=operation,
            items=tuple(items),
            warnings=tuple(sorted(warnings)),
            fresh_scan_required=(
                operation.status is not ExecutionOperationStatus.COMPLETED
            ),
        )

    def _execute_move(
        self,
        plan: RenamePlanV2,
        move: PlannedMove,
    ) -> tuple[ForwardExecutionItemResult, tuple[str, ...]]:
        expected = plan.candidate_snapshot.source_for(move.source_id)
        source_state, destination_state = self._observe(
            plan, move, expected
        )
        decision = decide_forward_move(source_state, destination_state)
        if decision is not ForwardMoveDecision.MOVE:
            return (
                ForwardExecutionItemResult(
                    source_id=move.source_id,
                    outcome=self._outcome(decision),
                ),
                (),
            )
        with _EFFECT_MUTEX:
            effect = self.filesystem.move(
                source_root=plan.source_root,
                source_path=expected.relative_path,
                expected=expected,
                destination_root=plan.output_root,
                destination_path=move.destination,
            )
        outcome = self._reobserve_after_move(plan, move, expected)
        return (
            ForwardExecutionItemResult(
                source_id=move.source_id,
                outcome=outcome,
                diagnostic=effect.diagnostic,
            ),
            effect.warnings,
        )

    def _reobserve_after_move(
        self,
        plan: RenamePlanV2,
        move: PlannedMove,
        expected: SemanticSourceIdentity,
    ) -> ExecutionItemOutcome:
        final = (
            PathObservationState.UNAVAILABLE,
            PathObservationState.UNAVAILABLE,
        )
        for delay in self.observation_delays:
            if delay:
                self.sleeper(delay)
            final = self._observe(plan, move, expected)
            source, destination = final
            if (
                source is PathObservationState.ABSENT
                and destination is PathObservationState.MATCHING
            ):
                return ExecutionItemOutcome.SATISFIED
            if PathObservationState.UNSAFE in final:
                return ExecutionItemOutcome.UNSAFE
            if destination is PathObservationState.MISMATCHED:
                return ExecutionItemOutcome.COLLISION
        decision = decide_forward_move(*final)
        if decision is ForwardMoveDecision.MOVE:
            return ExecutionItemOutcome.UNAVAILABLE
        return self._outcome(decision)

    def _observe(
        self,
        plan: RenamePlanV2,
        move: PlannedMove,
        expected: SemanticSourceIdentity,
    ) -> tuple[PathObservationState, PathObservationState]:
        return (
            self.filesystem.observe(
                root=plan.source_root,
                relative_path=expected.relative_path,
                expected=expected,
            ),
            self.filesystem.observe(
                root=plan.output_root,
                relative_path=move.destination,
                expected=expected,
            ),
        )

    @staticmethod
    def _outcome(decision: ForwardMoveDecision) -> ExecutionItemOutcome:
        try:
            return {
                ForwardMoveDecision.SATISFIED: (
                    ExecutionItemOutcome.SATISFIED
                ),
                ForwardMoveDecision.STALE: ExecutionItemOutcome.STALE,
                ForwardMoveDecision.COLLISION: (
                    ExecutionItemOutcome.COLLISION
                ),
                ForwardMoveDecision.UNSAFE: ExecutionItemOutcome.UNSAFE,
                ForwardMoveDecision.UNAVAILABLE: (
                    ExecutionItemOutcome.UNAVAILABLE
                ),
            }[decision]
        except KeyError:
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE) from None
