from __future__ import annotations

from dataclasses import dataclass

from reeloom.executor.apply import (
    ApplyResult,
    ApplyStatus,
    FilesystemExecutor,
)
from reeloom.executor.errors import (
    ApprovalError,
    ExecutorError,
)
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.ports.approvals import ApprovalStore
from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.events import (
    ApplyFailed,
    ApplyStarted,
    MoveApplied,
    PlanApproved,
    RollbackCompleted,
    RunCompleted,
)
from reeloom.runtime.state import Phase, RunState, RunStatus, StopReason
from reeloom.runtime.store import InMemoryEventStore


@dataclass(frozen=True, slots=True)
class ApprovalResumeService:
    """Resume one stopped run without exposing apply to the Agent."""

    runtime: InMemoryEventStore
    approvals: ApprovalStore
    executor: FilesystemExecutor

    def approve_and_apply(
        self,
        approval: ApprovalRecord,
    ) -> ApplyResult:
        state = self._awaiting_state()
        if (
            not isinstance(approval, ApprovalRecord)
            or not approval.verify_id()
            or approval.run_id != state.run_id
            or approval.plan_hash != state.plan_hash
            or approval.scope is not ApprovalScope.APPLY
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        self.approvals.issue(approval)
        self.runtime.append(
            PlanApproved(
                plan_hash=approval.plan_hash,
                approval_id=approval.approval_id,
            )
        )
        self.runtime.append(
            ApplyStarted(
                plan_hash=approval.plan_hash,
                approval_id=approval.approval_id,
            )
        )
        return self._execute(approval.approval_id, recover=False)

    def recover(self) -> ApplyResult:
        state = self._applying_state()
        if state.approval_id is None:
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        return self._execute(state.approval_id, recover=True)

    def _execute(
        self,
        approval_id: str,
        *,
        recover: bool,
    ) -> ApplyResult:
        state = self._applying_state()
        if state.plan_hash is None:
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        try:
            operation = (
                self.executor.recover
                if recover
                else self.executor.apply
            )
            result = operation(
                plan_hash=state.plan_hash,
                approval_id=approval_id,
            )
        except (ApprovalError, ExecutorError) as error:
            self._record_failure(error.code.value)
            raise
        self._finish(result)
        return result

    def _finish(self, result: ApplyResult) -> None:
        state = self._applying_state()
        plan = state.rename_plan
        if plan is None:
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        if result.status is ApplyStatus.COMPLETED:
            observed = set(state.applied_source_ids)
            for move in plan.draft.moves:
                if move.source_id not in observed:
                    self.runtime.append(
                        MoveApplied(source_id=move.source_id)
                    )
            self.runtime.append(
                RunCompleted(
                    transaction_id=result.transaction_id,
                    applied_count=result.applied_count,
                )
            )
            return

        self._record_failure(
            (
                result.failure_code.value
                if result.failure_code is not None
                else "recovery_rollback"
            )
        )
        self.runtime.append(
            RollbackCompleted(
                transaction_id=result.transaction_id,
                rolled_back_count=result.rolled_back_count,
            )
        )

    def _record_failure(self, code: str) -> None:
        state = self._applying_state()
        if state.failure_code is None:
            self.runtime.append(ApplyFailed(code=code))

    def _awaiting_state(self) -> RunState:
        state = self.runtime.state
        if (
            state is None
            or state.status is not RunStatus.STOPPED
            or state.phase is not Phase.AWAITING_APPROVAL
            or state.stop_reason is not StopReason.AWAITING_APPROVAL
            or state.plan_hash is None
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        return state

    def _applying_state(self) -> RunState:
        state = self.runtime.state
        if (
            state is None
            or state.status is not RunStatus.RUNNING
            or state.phase is not Phase.APPLYING
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        return state
