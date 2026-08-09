from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from reeloom.executor.forward import (
    ForwardExecutionResult,
    ForwardExecutor,
)
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.forward_execution import (
    ExecutionOperation,
    ExecutionOperationLease,
    RenamePlanV2,
)
from reeloom.kernel.movie_forward_execution import MovieRenamePlanV2
from reeloom.kernel.subtitle_acquisition import SubtitleAcquisitionPlanV2
from reeloom.kernel.initial_plan import parse_initial_plan
from reeloom.ports.plans import PlanStore
from reeloom.ports.subtitle_acquisition import SubtitleAcquisitionPlanStore
from reeloom.server.approval_repository import PostgresApprovalStore
from reeloom.server.config import ApplyPolicy, ConfigRevision
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.forward_operation_repository import (
    ForwardOperationError,
    ForwardOperationErrorCode,
    ForwardOperationView,
    PostgresForwardOperationRepository,
    execution_operation_id,
)
from reeloom.server.forward_actions import (
    ForwardAvailableAction,
    forward_available_actions,
)


def _now() -> datetime:
    return datetime.now(UTC)


class ForwardExecutionServiceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ForwardExecutionCommandResult:
    operation: ExecutionOperation
    result: ForwardExecutionResult | None = None

    @property
    def pending(self) -> bool:
        return not self.operation.terminal


class ForwardExecutionCoordinator:
    """Server-owned authorization and reconciliation for one v2 operation."""

    def __init__(
        self,
        *,
        configs: PostgresConfigRepository,
        plans: PlanStore,
        approvals: PostgresApprovalStore,
        operations: PostgresForwardOperationRepository,
        executor: ForwardExecutor,
        worker_id: str,
        subtitle_plans: SubtitleAcquisitionPlanStore | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._configs = configs
        self._plans = plans
        self._approvals = approvals
        self._operations = operations
        self._executor = executor
        self._worker_id = worker_id
        self._subtitle_plans = subtitle_plans
        self._clock = clock

    def execute_manual(
        self, *, run_id: str, plan_hash: str
    ) -> ForwardExecutionCommandResult:
        return self._execute(
            run_id=run_id,
            plan_hash=plan_hash,
            required_policy=ApplyPolicy.MANUAL,
        )

    def is_v2_plan(self, *, run_id: str, plan_hash: str) -> bool:
        try:
            self._load(run_id=run_id, plan_hash=plan_hash)
        except ForwardExecutionServiceError:
            return False
        return True

    def execute_automatic(
        self, *, run_id: str, plan_hash: str
    ) -> ForwardExecutionCommandResult:
        return self._execute(
            run_id=run_id,
            plan_hash=plan_hash,
            required_policy=ApplyPolicy.AUTOMATIC,
        )

    def reconcile(
        self, *, run_id: str, plan_hash: str
    ) -> ForwardExecutionCommandResult:
        plan, config = self._load(run_id=run_id, plan_hash=plan_hash)
        if config.apply_policy is ApplyPolicy.PLAN_ONLY:
            raise ForwardExecutionServiceError("plan_only")
        return self._run_existing(plan)

    def view(self, *, run_id: str, plan_hash: str) -> ForwardOperationView:
        plan, _config = self._load(run_id=run_id, plan_hash=plan_hash)
        return self._operations.get_view(
            execution_operation_id(
                run_id=plan.run_id,
                plan_hash=plan.plan_hash,
            )
        )

    def request_rescan(
        self, *, run_id: str, plan_hash: str
    ) -> ForwardOperationView:
        try:
            plan, config = self._load(run_id=run_id, plan_hash=plan_hash)
            policy = config.apply_policy
        except ForwardExecutionServiceError:
            plan, config = self._load_subtitle(
                run_id=run_id, plan_hash=plan_hash
            )
            policy = config.apply_policy
        view = self._operations.get_view(
            execution_operation_id(
                run_id=plan.run_id,
                plan_hash=plan.plan_hash,
            )
        )
        if ForwardAvailableAction.RESCAN not in forward_available_actions(
            policy=policy,
            operation_status=view.operation.status,
        ):
            raise ForwardExecutionServiceError("rescan_not_allowed")
        self._operations.requeue_rescan(
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
            now=self._clock(),
        )
        return self._operations.get_view(
            execution_operation_id(
                run_id=plan.run_id,
                plan_hash=plan.plan_hash,
            )
        )

    def _load_subtitle(
        self, *, run_id: str, plan_hash: str
    ) -> tuple[SubtitleAcquisitionPlanV2, ConfigRevision]:
        if self._subtitle_plans is None:
            raise ForwardExecutionServiceError("invalid_v2_plan")
        try:
            plan = SubtitleAcquisitionPlanV2.from_canonical_bytes(
                self._subtitle_plans.load(plan_hash), plan_hash=plan_hash
            )
        except Exception:
            raise ForwardExecutionServiceError("invalid_v2_plan") from None
        if plan.run_id != run_id:
            raise ForwardExecutionServiceError("invalid_v2_plan")
        config = self._configs.get(plan.config_revision)
        watch = next(
            (
                item
                for item in config.watches
                if item.watch_id == plan.watch_id
            ),
            None,
        )
        if watch is None:
            raise ForwardExecutionServiceError("watch_unavailable")
        return plan, config

    def reconcile_one(self) -> ForwardExecutionCommandResult | None:
        """Lease one unfinished operation without consulting browser state."""

        now = self._clock()
        lease = self._operations.claim_next(
            worker_id=self._worker_id,
            now=now,
            lease_for=timedelta(minutes=1),
        )
        if lease is None:
            return None
        plan, _config = self._load(
            run_id=lease.operation.run_id,
            plan_hash=lease.operation.plan_hash,
        )
        return self._run_lease(plan, lease)

    def _execute(
        self,
        *,
        run_id: str,
        plan_hash: str,
        required_policy: ApplyPolicy,
    ) -> ForwardExecutionCommandResult:
        plan, config = self._load(run_id=run_id, plan_hash=plan_hash)
        if config.apply_policy is not required_policy:
            raise ForwardExecutionServiceError("policy_mismatch")
        operation_id = execution_operation_id(
            run_id=run_id, plan_hash=plan_hash
        )
        try:
            existing = self._operations.get(operation_id)
        except ForwardOperationError as error:
            if error.code is not ForwardOperationErrorCode.OPERATION_NOT_FOUND:
                raise
        else:
            if (
                required_policy is ApplyPolicy.MANUAL
                and not existing.terminal
                and ForwardAvailableAction.EXECUTE
                not in forward_available_actions(
                    policy=config.apply_policy,
                    operation_status=existing.status,
                )
            ):
                raise ForwardExecutionServiceError("execution_not_allowed")
            return self._run_existing(plan)
        if (
            required_policy is ApplyPolicy.MANUAL
            and ForwardAvailableAction.EXECUTE
            not in forward_available_actions(
                policy=config.apply_policy,
                operation_status=None,
            )
        ):
            raise ForwardExecutionServiceError("execution_not_allowed")
        now = self._clock()
        approval = self._approvals.issue_or_reuse(
            ApprovalRecord.create(
                run_id=run_id,
                plan_hash=plan_hash,
                scope=ApprovalScope.APPLY,
                expires_at=now + timedelta(minutes=15),
                nonce=secrets.token_urlsafe(32),
            )
        )
        self._operations.authorize(
            ExecutionOperation.authorized(
                operation_id=operation_id,
                run_id=run_id,
                plan_hash=plan_hash,
            ),
            approval_id=approval.approval_id,
            now=now,
        )
        return self._run_existing(plan)

    def _run_existing(
        self, plan: RenamePlanV2 | MovieRenamePlanV2
    ) -> ForwardExecutionCommandResult:
        operation_id = execution_operation_id(
            run_id=plan.run_id, plan_hash=plan.plan_hash
        )
        operation = self._operations.get(operation_id)
        if operation.terminal:
            return ForwardExecutionCommandResult(operation)
        now = self._clock()
        lease = self._operations.claim(
            operation_id,
            worker_id=self._worker_id,
            now=now,
            lease_for=timedelta(minutes=1),
        )
        if lease is None:
            return ForwardExecutionCommandResult(
                self._operations.get(operation_id)
            )
        return self._run_lease(plan, lease)

    def _run_lease(
        self,
        plan: RenamePlanV2 | MovieRenamePlanV2,
        lease: ExecutionOperationLease,
    ) -> ForwardExecutionCommandResult:
        result = self._executor.execute(plan, lease)
        settled = self._operations.settle_result(
            lease,
            result,
            now=self._clock(),
        )
        return ForwardExecutionCommandResult(settled, result)

    def _load(
        self, *, run_id: str, plan_hash: str
    ) -> tuple[RenamePlanV2 | MovieRenamePlanV2, ConfigRevision]:
        plan = parse_initial_plan(
            self._plans.load(plan_hash), plan_hash=plan_hash
        )
        if (
            not isinstance(plan, (RenamePlanV2, MovieRenamePlanV2))
            or plan.run_id != run_id
        ):
            raise ForwardExecutionServiceError("invalid_v2_plan")
        config = self._configs.get(plan.config_revision)
        watch = next(
            (
                item
                for item in config.watches
                if item.watch_id == plan.watch_id
            ),
            None,
        )
        if watch is None:
            raise ForwardExecutionServiceError("watch_unavailable")
        return plan, config
