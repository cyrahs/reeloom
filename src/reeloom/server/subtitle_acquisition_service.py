from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Protocol

from psycopg_pool import ConnectionPool

from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.executor.subtitle_marker_acquisition import (
    SubtitleMarkerAcquisitionExecutor,
)
from reeloom.executor.subtitle_publication import (
    SubtitlePublicationResult,
    SubtitlePublicationState,
)
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.forward_execution import (
    ExecutionOperation,
    ExecutionOperationStatus,
)
from reeloom.kernel.subtitle_acquisition import SubtitleAcquisitionPlanV2
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.subtitle_acquisition import SubtitleAcquisitionPlanStore
from reeloom.runtime.events import RunFailed, SubtitleAcquisitionPlanCompleted
from reeloom.runtime.state import Phase
from reeloom.server.approval_repository import PostgresApprovalStore
from reeloom.server.config import (
    ConfigRevision,
    ServerWorkType,
    SubtitleAcquisitionPolicy,
    SubtitleProvider,
)
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.forward_operation_repository import (
    ForwardOperationError,
    ForwardOperationErrorCode,
    PostgresForwardOperationRepository,
    execution_operation_id,
)
from reeloom.server.runtime_store import PostgresEventStore

_LOG = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class SubtitleAcquisitionExecutorLease(Protocol):
    @property
    def executor(self) -> SubtitleMarkerAcquisitionExecutor: ...

    async def close(self) -> None: ...


class SubtitleAcquisitionExecutorFactory(Protocol):
    def __call__(self) -> SubtitleAcquisitionExecutorLease: ...


@dataclass(frozen=True, slots=True)
class SubtitleAcquisitionRequestRecord:
    run_id: str
    plan_hash: str
    policy: SubtitleAcquisitionPolicy
    status: str
    approval_id: str | None = None
    transaction_id: str | None = None
    failure_code: str | None = None
    failure_diagnostic: dict[str, object] | None = None


class SubtitleAcquisitionCoordinator:
    """Semantic-v2 subtitle planner/effect boundary.

    Approval authorizes one shared operation. Filesystem effects are always
    reconciled from current state; this coordinator has no v1 journal,
    recovery approval, replacement approval, or subtitle-specific successor.
    """

    def __init__(
        self,
        *,
        pool: ConnectionPool,
        plans: SubtitleAcquisitionPlanStore,
        executor_factory: SubtitleAcquisitionExecutorFactory,
        operation_approvals: PostgresApprovalStore,
        operations: PostgresForwardOperationRepository,
        worker_id: str = "subtitle-operation-worker",
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._pool = pool
        self._plans = plans
        self._executor_factory = executor_factory
        self._operation_approvals = operation_approvals
        self._operations = operations
        self._worker_id = worker_id
        self._clock = clock

    def register_plan(
        self,
        plan: SubtitleAcquisitionPlanV2,
    ) -> SubtitleAcquisitionRequestRecord:
        if not isinstance(plan, SubtitleAcquisitionPlanV2) or not plan.verify_hash():
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        try:
            stored = SubtitleAcquisitionPlanV2.from_canonical_bytes(
                self._plans.load(plan.plan_hash), plan_hash=plan.plan_hash
            )
        except Exception:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT) from None
        if stored != plan:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT r.config_revision, r.work_type, r.status,
                               r.subtitle_acquisition_lineage_key,
                               d.watch_id, d.source_folder,
                               d.folder_generation_id, d.snapshot_id,
                               observation.inventory_id,
                               c.payload, state.phase
                        FROM runs AS r
                        JOIN discoveries AS d USING (discovery_id)
                        JOIN watch_folder_observations AS observation
                          ON observation.discovery_id = d.discovery_id
                        JOIN config_revisions AS c
                          ON c.revision = r.config_revision
                        JOIN run_states AS state ON state.run_id = r.run_id
                        WHERE r.run_id = %s
                        FOR UPDATE OF r
                        """,
                        (plan.run_id,),
                    ).fetchone()
                    if row is None:
                        raise ServerError(ServerErrorCode.RUN_NOT_FOUND)
                    config = ConfigRevision.from_json(json.dumps(row[9]))
                    if (
                        str(row[1]) != ServerWorkType.ANIME.value
                        or str(row[2]) != "running"
                        or row[3] is not None
                        or str(row[4]) != plan.watch_id
                        or str(row[5]) != plan.source_folder
                        or str(row[6]) != plan.folder_generation_id
                        or str(row[7]) != plan.candidate_snapshot_id
                        or str(row[8]) != plan.inventory_id
                        or str(row[10]) != "build_subtitle_acquisition_plan"
                        or config.revision != int(row[0])
                        or config.revision != plan.config_revision
                        or config.revision_id != plan.config_revision_id
                    ):
                        raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
                    watch = next(
                        (
                            item
                            for item in config.watches
                            if item.watch_id == plan.watch_id
                            and item.work_type is ServerWorkType.ANIME
                        ),
                        None,
                    )
                    if (
                        watch is None
                        or not watch.subtitle_acquisition.enabled
                        or watch.subtitle_acquisition.provider
                        is not SubtitleProvider.ACGRIP
                        or not self._same_root(
                            plan, AuthorizedRoot.create(watch.root)
                        )
                    ):
                        raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
                    binding = connection.execute(
                        """
                        INSERT INTO effect_plan_bindings_v2
                            (run_id, plan_hash, plan_kind, approval_scope)
                        VALUES (%s, %s, 'subtitle_acquire',
                                'subtitle_acquire')
                        ON CONFLICT (run_id, plan_hash) DO NOTHING
                        RETURNING plan_kind, approval_scope
                        """,
                        (plan.run_id, plan.plan_hash),
                    ).fetchone()
                    if binding is None:
                        binding = connection.execute(
                            """
                            SELECT plan_kind, approval_scope
                            FROM effect_plan_bindings_v2
                            WHERE run_id = %s AND plan_hash = %s
                            """,
                            (plan.run_id, plan.plan_hash),
                        ).fetchone()
                    if binding is None or tuple(map(str, binding)) != (
                        "subtitle_acquire",
                        "subtitle_acquire",
                    ):
                        raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
                    existing = connection.execute(
                        """
                        SELECT plan_hash, policy, status, approval_id,
                               transaction_id, failure_code,
                               failure_diagnostic
                        FROM subtitle_acquisition_requests
                        WHERE run_id = %s
                        """,
                        (plan.run_id,),
                    ).fetchone()
                    if existing is not None:
                        record = self._record(plan.run_id, existing)
                        if record.plan_hash != plan.plan_hash:
                            raise ServerError(
                                ServerErrorCode.INTERACTION_CONFLICT
                            )
                        return record
                    connection.execute(
                        """
                        INSERT INTO subtitle_acquisition_requests
                            (run_id, plan_hash, config_revision,
                             policy, status)
                        VALUES (%s, %s, %s, %s, 'planned')
                        """,
                        (
                            plan.run_id,
                            plan.plan_hash,
                            config.revision,
                            watch.subtitle_acquisition.policy.value,
                        ),
                    )
                    if (
                        watch.subtitle_acquisition.policy
                        is SubtitleAcquisitionPolicy.PLAN_ONLY
                    ):
                        connection.execute(
                            """
                            INSERT INTO handled_folder_inventories_v2
                                (watch_id, source_folder, inventory_id,
                                 run_id, terminal_status)
                            VALUES (%s, %s, %s, %s, 'completed')
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                plan.watch_id,
                                plan.source_folder,
                                plan.inventory_id,
                                plan.run_id,
                            ),
                        )
                    return SubtitleAcquisitionRequestRecord(
                        plan.run_id,
                        plan.plan_hash,
                        watch.subtitle_acquisition.policy,
                        "planned",
                    )
        except ServerError:
            raise
        except Exception:
            _LOG.exception("subtitle_plan_registration_failed")
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def approve_and_execute(
        self,
        *,
        run_id: str,
        plan_hash: str,
        automatic: bool,
    ) -> SubtitleAcquisitionRequestRecord:
        request = self.resolve(run_id=run_id, plan_hash=plan_hash)
        if request is None:
            raise ServerError(ServerErrorCode.RUN_NOT_FOUND)
        if request.policy is SubtitleAcquisitionPolicy.PLAN_ONLY:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        if automatic != (
            request.policy is SubtitleAcquisitionPolicy.AUTOMATIC
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        operation_id = execution_operation_id(
            run_id=run_id, plan_hash=plan_hash
        )
        try:
            operation = self._operations.get(operation_id)
        except ForwardOperationError as error:
            if error.code is not ForwardOperationErrorCode.OPERATION_NOT_FOUND:
                raise
            now = self._clock()
            approval = self._operation_approvals.issue_or_reuse(
                ApprovalRecord.create(
                    run_id=run_id,
                    plan_hash=plan_hash,
                    scope=ApprovalScope.SUBTITLE_ACQUIRE,
                    expires_at=now + timedelta(minutes=15),
                    nonce=secrets.token_urlsafe(32),
                )
            )
            self._mark_approved(
                run_id=run_id,
                plan_hash=plan_hash,
                approval_id=approval.approval_id,
            )
            operation = self._operations.authorize(
                ExecutionOperation.authorized(
                    operation_id=operation_id,
                    run_id=run_id,
                    plan_hash=plan_hash,
                ),
                approval_id=approval.approval_id,
                now=now,
                scope=ApprovalScope.SUBTITLE_ACQUIRE,
                operation_kind="subtitle_acquire",
            )
        if operation.terminal:
            return self._resolve_terminal(operation=operation, request=request)
        lease = self._operations.claim(
            operation_id,
            worker_id=self._worker_id,
            now=self._clock(),
            lease_for=timedelta(minutes=1),
            operation_kind="subtitle_acquire",
        )
        if lease is None:
            current = self._operations.get(operation_id)
            if current.terminal:
                return self._resolve_terminal(
                    operation=current, request=request
                )
            return request
        plan = self._load_plan(plan_hash)
        executor_lease = self._executor_factory()
        try:
            try:
                result = asyncio.run(
                    executor_lease.executor.execute_current(
                        plan_hash=plan_hash
                    )
                )
            except ExecutorError as error:
                result = SubtitlePublicationResult(
                    state=(
                        SubtitlePublicationState.UNSAFE
                        if error.code
                        in {
                            ExecutorErrorCode.INVALID_PLAN,
                            ExecutorErrorCode.SYMLINK_NOT_ALLOWED,
                        }
                        else SubtitlePublicationState.UNAVAILABLE
                    ),
                    publication_directory=(
                        plan.destination_directory.as_posix()
                    ),
                    published_count=0,
                    reason=error.code.value,
                )
            self._project_terminal(
                run_id=run_id, plan_hash=plan_hash, result=result
            )
            self._operations.settle_subtitle_result(
                lease,
                result,
                origin_discovery_id=self._origin_discovery_id(run_id),
                now=self._clock(),
            )
        finally:
            try:
                asyncio.run(executor_lease.close())
            except Exception:
                _LOG.exception("failed to close subtitle executor lease")
        resolved = self.resolve(run_id=run_id, plan_hash=plan_hash)
        if resolved is None:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        return resolved

    def resolve(
        self,
        *,
        run_id: str,
        plan_hash: str,
    ) -> SubtitleAcquisitionRequestRecord | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT plan_hash, policy, status, approval_id,
                           transaction_id, failure_code,
                           failure_diagnostic
                    FROM subtitle_acquisition_requests
                    WHERE run_id = %s AND plan_hash = %s
                    """,
                    (run_id, plan_hash),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        return None if row is None else self._record(run_id, row)

    def reconcile_approved(self) -> int:
        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT request.run_id, request.plan_hash, request.policy
                    FROM subtitle_acquisition_requests AS request
                    JOIN runs AS run ON run.run_id = request.run_id
                    WHERE request.status = 'approved'
                      AND run.status = 'running'
                    ORDER BY updated_at, run_id
                    """
                ).fetchall()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        completed = 0
        for row in rows:
            try:
                self.approve_and_execute(
                    run_id=str(row[0]),
                    plan_hash=str(row[1]),
                    automatic=(str(row[2]) == "automatic"),
                )
                completed += 1
            except Exception as error:
                _LOG.warning(
                    "subtitle_operation_reconcile_pending run_id=%s "
                    "error_type=%s",
                    row[0],
                    type(error).__name__,
                )
        return completed

    def _resolve_terminal(
        self,
        *,
        operation: ExecutionOperation,
        request: SubtitleAcquisitionRequestRecord,
    ) -> SubtitleAcquisitionRequestRecord:
        if request.status in {"published", "blocked"}:
            return request
        if operation.status is not ExecutionOperationStatus.UNAVAILABLE:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        result = SubtitlePublicationResult(
            state=SubtitlePublicationState.UNAVAILABLE,
            publication_directory="",
            published_count=0,
            reason="operation_retry_exhausted",
        )
        self._project_terminal(
            run_id=operation.run_id,
            plan_hash=operation.plan_hash,
            result=result,
        )
        self._operations.settle_exhausted_subtitle(
            operation.operation_id,
            origin_discovery_id=self._origin_discovery_id(operation.run_id),
            now=self._clock(),
        )
        resolved = self.resolve(
            run_id=operation.run_id, plan_hash=operation.plan_hash
        )
        if resolved is None:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        return resolved

    def _load_plan(self, plan_hash: str) -> SubtitleAcquisitionPlanV2:
        try:
            return SubtitleAcquisitionPlanV2.from_canonical_bytes(
                self._plans.load(plan_hash), plan_hash=plan_hash
            )
        except Exception:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT) from None

    def _origin_discovery_id(self, run_id: str) -> str:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    "SELECT discovery_id FROM runs WHERE run_id = %s",
                    (run_id,),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        if row is None:
            raise ServerError(ServerErrorCode.RUN_NOT_FOUND)
        return str(row[0])

    def _project_terminal(
        self,
        *,
        run_id: str,
        plan_hash: str,
        result: SubtitlePublicationResult,
    ) -> None:
        store = PostgresEventStore(self._pool, run_id=run_id)
        state = store.state
        if state is None or state.phase in {Phase.COMPLETED, Phase.FAILED}:
            return
        if result.state is SubtitlePublicationState.COMPLETED:
            store.append(SubtitleAcquisitionPlanCompleted(plan_hash))
            return
        store.append(
            RunFailed(
                {
                    SubtitlePublicationState.COLLISION: (
                        "destination_collision"
                    ),
                    SubtitlePublicationState.UNSAFE: "unsafe_entry",
                    SubtitlePublicationState.UNAVAILABLE: "root_unavailable",
                    SubtitlePublicationState.COMPLETED: "completed",
                }[result.state]
            )
        )

    def _mark_approved(
        self,
        *,
        run_id: str,
        plan_hash: str,
        approval_id: str,
    ) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE subtitle_acquisition_requests
                        SET status = 'approved', approval_id = %s,
                            updated_at = clock_timestamp()
                        WHERE run_id = %s AND plan_hash = %s
                          AND status = 'planned'
                        RETURNING run_id
                        """,
                        (approval_id, run_id, plan_hash),
                    ).fetchone()
                    if row is None:
                        existing = connection.execute(
                            """
                            SELECT approval_id
                            FROM subtitle_acquisition_requests
                            WHERE run_id = %s AND plan_hash = %s
                              AND status = 'approved'
                            """,
                            (run_id, plan_hash),
                        ).fetchone()
                        if existing is None or str(existing[0]) != approval_id:
                            raise ServerError(
                                ServerErrorCode.INTERACTION_CONFLICT
                            )
        except ServerError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    @staticmethod
    def _record(
        run_id: str,
        row: object,
    ) -> SubtitleAcquisitionRequestRecord:
        values = tuple(row)  # type: ignore[arg-type]
        return SubtitleAcquisitionRequestRecord(
            run_id=run_id,
            plan_hash=str(values[0]),
            policy=SubtitleAcquisitionPolicy(str(values[1])),
            status=str(values[2]),
            approval_id=None if values[3] is None else str(values[3]),
            transaction_id=None if values[4] is None else str(values[4]),
            failure_code=None if values[5] is None else str(values[5]),
            failure_diagnostic=(
                None if values[6] is None else dict(values[6])
            ),
        )

    @staticmethod
    def _same_root(
        plan: SubtitleAcquisitionPlanV2,
        root: AuthorizedRoot,
    ) -> bool:
        return plan.source_root.path == PurePosixPath(root.path.as_posix())
