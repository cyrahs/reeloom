from __future__ import annotations

from dataclasses import dataclass

from reeloom.executor.apply import (
    ApplyResult,
    ApplyStatus,
    FilesystemExecutor,
)
from reeloom.executor.errors import (
    ApprovalError,
    ApprovalErrorCode,
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
from reeloom.runtime.store import EventStore


@dataclass(frozen=True, slots=True)
class ApprovalResumeService:
    """Resume one stopped run without exposing apply to the Agent."""

    runtime: EventStore
    approvals: ApprovalStore
    executor: FilesystemExecutor

    def approve_and_apply(
        self,
        approval: ApprovalRecord,
    ) -> ApplyResult:
        state = self.runtime.state
        if (
            state is None
            or not isinstance(approval, ApprovalRecord)
            or not approval.verify_id()
            or approval.run_id != state.run_id
            or approval.plan_hash != state.plan_hash
            or approval.scope is not ApprovalScope.APPLY
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        if (
            state.status is RunStatus.STOPPED
            and state.phase is Phase.AWAITING_APPROVAL
            and state.stop_reason is StopReason.AWAITING_APPROVAL
            and state.approval_id is None
        ):
            self.approvals.issue(approval)
            self.runtime.append(
                PlanApproved(
                    plan_hash=approval.plan_hash,
                    approval_id=approval.approval_id,
                )
            )
        elif not (
            state.status is RunStatus.RUNNING
            and state.phase in {Phase.AWAITING_APPROVAL, Phase.APPLYING}
            and state.approval_id == approval.approval_id
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        self._start_apply_if_needed()
        return self._execute(approval.approval_id)

    def recover(self) -> ApplyResult:
        state = self.runtime.state
        if (
            state is None
            or state.status is not RunStatus.RUNNING
            or state.phase
            not in {Phase.AWAITING_APPROVAL, Phase.APPLYING}
            or state.approval_id is None
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        self._start_apply_if_needed()
        state = self._applying_state()
        if state.approval_id is None:
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        return self._execute(state.approval_id)

    def _execute(
        self,
        approval_id: str,
    ) -> ApplyResult:
        state = self._applying_state()
        if state.plan_hash is None:
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_TRANSITION
            )
        try:
            try:
                result = self.executor.apply(
                    plan_hash=state.plan_hash,
                    approval_id=approval_id,
                )
            except ApprovalError as error:
                if error.code is not ApprovalErrorCode.ALREADY_CLAIMED:
                    raise
                result = self.executor.recover(
                    plan_hash=state.plan_hash,
                    approval_id=approval_id,
                )
        except (ApprovalError, ExecutorError) as error:
            self._record_failure(error.code.value)
            raise
        self._finish(result)
        return result

    def _start_apply_if_needed(self) -> None:
        state = self.runtime.state
        if (
            state is not None
            and state.status is RunStatus.RUNNING
            and state.phase is Phase.AWAITING_APPROVAL
            and state.plan_hash is not None
            and state.approval_id is not None
        ):
            self.runtime.append(
                ApplyStarted(
                    plan_hash=state.plan_hash,
                    approval_id=state.approval_id,
                )
            )

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
