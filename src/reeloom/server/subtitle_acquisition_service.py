from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Iterator, Protocol

from psycopg_pool import ConnectionPool

from reeloom.executor.errors import (
    ApprovalError,
    ApprovalErrorCode,
    ExecutorError,
    ExecutorErrorCode,
)
from reeloom.executor.subtitle_acquisition import (
    SubtitleAcquisitionExecutor,
    SubtitleAcquisitionResult,
)
from reeloom.executor.subtitle_marker_acquisition import (
    SubtitleMarkerAcquisitionExecutor,
)
from reeloom.executor.subtitle_publication import (
    SubtitlePublicationResult,
    SubtitlePublicationState,
)
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.subtitle_acquisition import SubtitleAcquisitionPlan
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.approvals import ApprovalStore
from reeloom.ports.subtitle_acquisition import SubtitleAcquisitionPlanStore
from reeloom.server.config import (
    ConfigRevision,
    ServerWorkType,
    SubtitleAcquisitionPolicy,
)
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.subtitle_successor import SubtitleAcquisitionSettlement
from reeloom.server.subtitle_successor_repository import (
    PostgresSubtitleSuccessorOutbox,
)
from reeloom.server.subtitle_publication_repository import (
    PostgresSubtitlePublicationRepository,
)

_LOGGER = logging.getLogger(__name__)
_FAILURE_STAGES = frozenset(
    {
        "destination_preflight",
        "staging_prepare",
        "staging_validate",
        "member_write",
        "publish",
    }
)
_FAILURE_REASONS = frozenset(
    {
        "name_exists",
        "create_failed",
        "entry_type_mismatch",
        "unsafe_permissions",
        "owner_mismatch",
        "not_empty",
        "unexpected_entries",
        "casefold_collision",
    }
)
_FAILURE_DETAIL_FIELDS = frozenset(
    {
        "actual_mode",
        "actual_uid",
        "entry_count",
        "expected_policy",
        "expected_uid",
        "member_index",
        "reason",
        "stage",
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


class SubtitleAcquisitionExecutorLease(Protocol):
    @property
    def executor(
        self,
    ) -> SubtitleAcquisitionExecutor | SubtitleMarkerAcquisitionExecutor: ...

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


@dataclass(frozen=True, slots=True)
class _Reservation:
    config: ConfigRevision
    watch_id: str
    discovery_id: str


class SubtitleAcquisitionCoordinator:
    """Config-bound production gate for subtitle acquisition effects."""

    def __init__(
        self,
        *,
        pool: ConnectionPool,
        plans: SubtitleAcquisitionPlanStore,
        approvals: ApprovalStore,
        executor_factory: SubtitleAcquisitionExecutorFactory,
        successors: PostgresSubtitleSuccessorOutbox | None = None,
        publications: PostgresSubtitlePublicationRepository | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._pool = pool
        self._plans = plans
        self._approvals = approvals
        self._executor_factory = executor_factory
        self._successors = successors
        self._publications = publications
        if successors is None and publications is None:
            raise TypeError("one subtitle settlement repository is required")
        self._clock = clock
        self._global_gate = threading.Lock()

    def register_plan(
        self,
        plan: SubtitleAcquisitionPlan,
    ) -> SubtitleAcquisitionRequestRecord:
        if not isinstance(plan, SubtitleAcquisitionPlan) or not plan.verify_hash():
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        stored = SubtitleAcquisitionPlan.from_canonical_bytes(
            self._plans.load(plan.plan_hash),
            plan_hash=plan.plan_hash,
        )
        if stored != plan:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT r.discovery_id, r.config_revision,
                               r.work_type, r.status,
                               r.subtitle_acquisition_lineage_key,
                               d.watch_id, d.source_folder,
                               d.folder_generation_id, d.snapshot_id,
                               observation.folder_device,
                               observation.folder_inode,
                               c.payload, state.phase
                        FROM runs AS r
                        JOIN discoveries AS d
                          ON d.discovery_id = r.discovery_id
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
                    config = ConfigRevision.from_json(json.dumps(row[11]))
                    if (
                        str(row[2]) != ServerWorkType.ANIME.value
                        or str(row[3]) != "running"
                        or row[4] is not None
                        or str(row[6]) != plan.source_folder
                        or str(row[7]) != plan.folder_generation_id
                        or str(row[8]) != plan.candidate_snapshot_id
                        or str(row[12])
                        != "build_subtitle_acquisition_plan"
                        or config.revision != int(row[1])
                        or config.revision_id != plan.config_revision_id
                        or not config.acgrip.enabled
                    ):
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    watch = next(
                        (
                            item
                            for item in config.watches
                            if item.watch_id == str(row[5])
                            and item.work_type is ServerWorkType.ANIME
                        ),
                        None,
                    )
                    if watch is None or not self._same_root(
                        plan, AuthorizedRoot.create(watch.root)
                    ):
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
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
                            config.subtitle_acquisition_policy.value,
                        ),
                    )
                    return SubtitleAcquisitionRequestRecord(
                        plan.run_id,
                        plan.plan_hash,
                        config.subtitle_acquisition_policy,
                        "planned",
                    )
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def approve_and_execute(
        self,
        *,
        run_id: str,
        plan_hash: str,
        automatic: bool,
    ) -> SubtitleAcquisitionRequestRecord:
        existing = self.resolve(run_id=run_id, plan_hash=plan_hash)
        if existing is not None and existing.status == "published":
            return existing
        operation_id = f"subtitle-{secrets.token_hex(16)}"
        with self._operation(
            run_id=run_id,
            plan_hash=plan_hash,
            operation_id=operation_id,
            automatic=automatic,
        ) as reservation:
            with self._global_gate:
                request = self.resolve(run_id=run_id, plan_hash=plan_hash)
                if request is None or request.status == "blocked":
                    raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
                approval_id = request.approval_id
                if approval_id is None:
                    approval = ApprovalRecord.create(
                        run_id=run_id,
                        plan_hash=plan_hash,
                        scope=ApprovalScope.SUBTITLE_ACQUIRE,
                        expires_at=self._clock() + timedelta(minutes=15),
                        nonce=secrets.token_urlsafe(32),
                    )
                    self._approvals.issue(approval)
                    self._mark_approved(
                        run_id=run_id,
                        plan_hash=plan_hash,
                        approval_id=approval.approval_id,
                    )
                    approval_id = approval.approval_id
                lease = self._executor_factory()
                try:
                    result, approval_id = asyncio.run(
                        self._execute_lease(
                            lease=lease,
                            run_id=run_id,
                            plan_hash=plan_hash,
                            approval_id=approval_id,
                        )
                    )
                except Exception as error:
                    if self._terminal_failure(error):
                        self._mark_blocked(
                            run_id=run_id,
                            plan_hash=plan_hash,
                            failure_code=self._failure_code(error),
                            failure_diagnostic=(
                                self._failure_diagnostic(error)
                            ),
                        )
                    raise
                plan = SubtitleAcquisitionPlan.from_canonical_bytes(
                    self._plans.load(plan_hash),
                    plan_hash=plan_hash,
                )
                if isinstance(result, SubtitlePublicationResult):
                    if result.state is not SubtitlePublicationState.COMPLETED:
                        error = self._publication_error(result)
                        if self._terminal_failure(error):
                            self._mark_blocked(
                                run_id=run_id,
                                plan_hash=plan_hash,
                                failure_code=self._failure_code(error),
                                failure_diagnostic={
                                    "schema_version": 2,
                                    "stage": "publication",
                                    "reason": result.reason or result.state.value,
                                },
                            )
                        raise error
                    if self._publications is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    self._publications.settle(
                        plan=plan,
                        approval_id=approval_id,
                        result=result,
                        origin_discovery_id=reservation.discovery_id,
                    )
                else:
                    if self._successors is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    settlement = SubtitleAcquisitionSettlement.create(
                        plan=plan,
                        result=result,
                        origin_discovery_id=reservation.discovery_id,
                    )
                    self._successors.settle(settlement)
                resolved = self.resolve(run_id=run_id, plan_hash=plan_hash)
                if resolved is None or resolved.status != "published":
                    raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
                return resolved

    def retry_blocked_and_execute(
        self,
        *,
        run_id: str,
        plan_hash: str,
    ) -> SubtitleAcquisitionRequestRecord:
        policy = self._reopen_retryable_failure(
            run_id=run_id,
            plan_hash=plan_hash,
        )
        return self.approve_and_execute(
            run_id=run_id,
            plan_hash=plan_hash,
            automatic=(policy is SubtitleAcquisitionPolicy.AUTOMATIC),
        )

    def fail_blocked(
        self,
        *,
        run_id: str,
        plan_hash: str,
    ) -> SubtitleAcquisitionRequestRecord:
        """End a run whose exact subtitle acquisition is blocked."""

        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE runs AS run
                        SET status = 'failed'
                        FROM subtitle_acquisition_requests AS request,
                             jobs AS job
                        WHERE request.run_id = %s
                          AND request.plan_hash = %s
                          AND request.status = 'blocked'
                          AND run.run_id = request.run_id
                          AND run.status = 'running'
                          AND job.run_id = run.run_id
                          AND job.status = 'completed'
                          AND NOT EXISTS (
                              SELECT 1
                              FROM run_operations AS operation
                              WHERE operation.run_id = run.run_id
                          )
                        RETURNING request.plan_hash, request.policy,
                                  request.status, request.approval_id,
                                  request.transaction_id,
                                  request.failure_code,
                                  request.failure_diagnostic
                        """,
                        (run_id, plan_hash),
                    ).fetchone()
                    if row is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    connection.execute(
                        """
                        INSERT INTO scheduler_audit
                            (event_type, subject_id)
                        VALUES ('subtitle_acquisition_failed', %s)
                        ON CONFLICT (event_type, subject_id) DO NOTHING
                        """,
                        (run_id,),
                    )
                    return self._record(run_id, row)
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def resolve_failed(
        self,
        *,
        run_id: str,
        plan_hash: str,
    ) -> SubtitleAcquisitionRequestRecord | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT request.plan_hash, request.policy,
                           request.status, request.approval_id,
                           request.transaction_id,
                           request.failure_code,
                           request.failure_diagnostic
                    FROM subtitle_acquisition_requests AS request
                    JOIN runs AS run ON run.run_id = request.run_id
                    WHERE request.run_id = %s
                      AND request.plan_hash = %s
                      AND request.status = 'blocked'
                      AND run.status = 'failed'
                    """,
                    (run_id, plan_hash),
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        return None if row is None else self._record(run_id, row)

    async def _execute_lease(
        self,
        *,
        lease: SubtitleAcquisitionExecutorLease,
        run_id: str,
        plan_hash: str,
        approval_id: str,
    ) -> tuple[SubtitleAcquisitionResult | SubtitlePublicationResult, str]:
        """Apply or recover on one event loop and always close on that loop."""

        try:
            try:
                result = await lease.executor.apply(
                    plan_hash=plan_hash,
                    approval_id=approval_id,
                )
            except ApprovalError as error:
                if error.code is ApprovalErrorCode.ALREADY_CLAIMED:
                    if isinstance(
                        lease.executor,
                        SubtitleMarkerAcquisitionExecutor,
                    ):
                        result = await lease.executor.reconcile(
                            plan_hash=plan_hash,
                            approval_id=approval_id,
                        )
                    else:
                        result = await lease.executor.recover(
                            plan_hash=plan_hash,
                            approval_id=approval_id,
                        )
                elif error.code is ApprovalErrorCode.EXPIRED:
                    replacement = ApprovalRecord.create(
                        run_id=run_id,
                        plan_hash=plan_hash,
                        scope=ApprovalScope.SUBTITLE_ACQUIRE,
                        expires_at=self._clock() + timedelta(minutes=15),
                        nonce=secrets.token_urlsafe(32),
                    )
                    self._approvals.issue(replacement)
                    self._replace_approval(
                        run_id=run_id,
                        plan_hash=plan_hash,
                        previous_approval_id=approval_id,
                        replacement_approval_id=replacement.approval_id,
                    )
                    approval_id = replacement.approval_id
                    result = await lease.executor.apply(
                        plan_hash=plan_hash,
                        approval_id=approval_id,
                    )
                else:
                    raise
            return result, approval_id
        finally:
            try:
                await lease.close()
            except Exception:
                # Closing a transport cannot undo a durable executor result and
                # must not turn a published/recoverable transaction into an
                # unrelated failure.
                _LOGGER.exception(
                    "failed to close subtitle acquisition lease"
                )

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
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        return None if row is None else self._record(run_id, row)

    def reconcile_approved(self) -> int:
        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT run_id, plan_hash, policy
                    FROM subtitle_acquisition_requests
                    WHERE status = 'approved'
                    ORDER BY updated_at, run_id
                    """
                ).fetchall()
                with connection.transaction():
                    connection.execute(
                        """
                        DELETE FROM run_operations
                        WHERE operation_kind IN
                            ('subtitle_acquire', 'subtitle_recover')
                        """
                    )
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
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
                _LOGGER.warning(
                    "subtitle_acquisition_recovery_pending run_id=%s "
                    "error_type=%s",
                    row[0],
                    type(error).__name__,
                )
        return completed

    @contextmanager
    def _operation(
        self,
        *,
        run_id: str,
        plan_hash: str,
        operation_id: str,
        automatic: bool,
    ) -> Iterator[_Reservation]:
        reservation = self._reserve(
            run_id=run_id,
            plan_hash=plan_hash,
            operation_id=operation_id,
            automatic=automatic,
        )
        primary: BaseException | None = None
        try:
            yield reservation
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                self._release(run_id=run_id, operation_id=operation_id)
            except Exception:
                if primary is None:
                    raise
                _LOGGER.exception(
                    "failed to release subtitle acquisition operation"
                )

    def _reserve(
        self,
        *,
        run_id: str,
        plan_hash: str,
        operation_id: str,
        automatic: bool,
    ) -> _Reservation:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT request.policy, request.status,
                               request.config_revision, c.payload,
                               d.watch_id, d.discovery_id,
                               r.status
                        FROM subtitle_acquisition_requests AS request
                        JOIN runs AS r ON r.run_id = request.run_id
                        JOIN discoveries AS d
                          ON d.discovery_id = r.discovery_id
                        JOIN config_revisions AS c
                          ON c.revision = request.config_revision
                        WHERE request.run_id = %s
                          AND request.plan_hash = %s
                        FOR UPDATE OF r, request
                        """,
                        (run_id, plan_hash),
                    ).fetchone()
                    if row is None or str(row[1]) not in {
                        "planned",
                        "approved",
                    }:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    config = ConfigRevision.from_json(json.dumps(row[3]))
                    policy = SubtitleAcquisitionPolicy(str(row[0]))
                    if (
                        not config.acgrip.enabled
                        or config.revision != int(row[2])
                        or config.subtitle_acquisition_policy is not policy
                        or policy is SubtitleAcquisitionPolicy.PLAN_ONLY
                        or automatic
                        != (policy is SubtitleAcquisitionPolicy.AUTOMATIC)
                        or str(row[6]) != "running"
                    ):
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    inserted = connection.execute(
                        """
                        INSERT INTO run_operations
                            (run_id, operation_id, operation_kind)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (run_id) DO NOTHING
                        RETURNING operation_id
                        """,
                        (
                            run_id,
                            operation_id,
                            (
                                "subtitle_recover"
                                if str(row[1]) == "approved"
                                else "subtitle_acquire"
                            ),
                        ),
                    ).fetchone()
                    if inserted is None:
                        raise ServerError(ServerErrorCode.RUN_BUSY)
                    return _Reservation(
                        config=config,
                        watch_id=str(row[4]),
                        discovery_id=str(row[5]),
                    )
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

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
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def _reopen_retryable_failure(
        self,
        *,
        run_id: str,
        plan_hash: str,
    ) -> SubtitleAcquisitionPolicy:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE subtitle_acquisition_requests AS request
                        SET status = 'approved', failure_code = NULL,
                            failure_diagnostic = NULL,
                            updated_at = clock_timestamp()
                        FROM runs AS run
                        WHERE request.run_id = %s
                          AND request.plan_hash = %s
                          AND request.status = 'blocked'
                          AND request.failure_code IN (
                              'destination_collision',
                              'atomic_move_unsupported'
                          )
                          AND request.approval_id IS NOT NULL
                          AND request.policy IN ('manual', 'automatic')
                          AND run.run_id = request.run_id
                          AND run.status = 'running'
                        RETURNING request.policy
                        """,
                        (run_id, plan_hash),
                    ).fetchone()
                    if row is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    return SubtitleAcquisitionPolicy(str(row[0]))
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def _mark_blocked(
        self,
        *,
        run_id: str,
        plan_hash: str,
        failure_code: str,
        failure_diagnostic: dict[str, object] | None,
    ) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        UPDATE subtitle_acquisition_requests
                        SET status = 'blocked', failure_code = %s,
                            failure_diagnostic = %s::jsonb,
                            transaction_id = NULL,
                            updated_at = clock_timestamp()
                        WHERE run_id = %s AND plan_hash = %s
                          AND status IN ('planned', 'approved')
                        """,
                        (
                            failure_code,
                            (
                                None
                                if failure_diagnostic is None
                                else json.dumps(failure_diagnostic)
                            ),
                            run_id,
                            plan_hash,
                        ),
                    )
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def _replace_approval(
        self,
        *,
        run_id: str,
        plan_hash: str,
        previous_approval_id: str,
        replacement_approval_id: str,
    ) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE subtitle_acquisition_requests
                        SET approval_id = %s,
                            updated_at = clock_timestamp()
                        WHERE run_id = %s AND plan_hash = %s
                          AND status = 'approved'
                          AND approval_id = %s
                        RETURNING run_id
                        """,
                        (
                            replacement_approval_id,
                            run_id,
                            plan_hash,
                            previous_approval_id,
                        ),
                    ).fetchone()
                    if row is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def _release(self, *, run_id: str, operation_id: str) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        DELETE FROM run_operations
                        WHERE run_id = %s AND operation_id = %s
                        """,
                        (run_id, operation_id),
                    )
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

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
        plan: SubtitleAcquisitionPlan,
        root: AuthorizedRoot,
    ) -> bool:
        return plan.source_root.path == PurePosixPath(root.path.as_posix())

    @staticmethod
    def _publication_error(
        result: SubtitlePublicationResult,
    ) -> ExecutorError:
        code = {
            SubtitlePublicationState.COLLISION: (
                ExecutorErrorCode.DESTINATION_COLLISION
            ),
            SubtitlePublicationState.UNSAFE: (
                ExecutorErrorCode.SYMLINK_NOT_ALLOWED
            ),
            SubtitlePublicationState.UNAVAILABLE: (
                ExecutorErrorCode.TRANSIENT_IO
            ),
            SubtitlePublicationState.COMPLETED: (
                ExecutorErrorCode.INVALID_PLAN
            ),
        }[result.state]
        return ExecutorError(
            code,
            context={
                "stage": "publication",
                "reason": result.reason or result.state.value,
            },
        )

    @staticmethod
    def _terminal_failure(error: Exception) -> bool:
        return isinstance(error, ExecutorError) and error.code in {
            ExecutorErrorCode.INVALID_PLAN,
            ExecutorErrorCode.ROOT_DRIFT,
            ExecutorErrorCode.SOURCE_DRIFT,
            ExecutorErrorCode.DESTINATION_COLLISION,
            ExecutorErrorCode.SYMLINK_NOT_ALLOWED,
            ExecutorErrorCode.CROSS_FILESYSTEM,
            ExecutorErrorCode.ATOMIC_MOVE_UNSUPPORTED,
            ExecutorErrorCode.PERMISSION_DENIED,
            ExecutorErrorCode.STATE_AMBIGUOUS,
            ExecutorErrorCode.MOVE_FAILED,
        }

    @staticmethod
    def _failure_code(error: Exception) -> str:
        if isinstance(error, (ExecutorError, ApprovalError)):
            return error.code.value
        return "subtitle_acquisition_failed"

    @staticmethod
    def _failure_diagnostic(
        error: Exception,
    ) -> dict[str, object] | None:
        if (
            not isinstance(error, ExecutorError)
            or error.code is not ExecutorErrorCode.DESTINATION_COLLISION
        ):
            return None
        context = dict(error.context)
        if (
            not context
            or not set(context) <= _FAILURE_DETAIL_FIELDS
            or context.get("stage") not in _FAILURE_STAGES
            or context.get("reason") not in _FAILURE_REASONS
        ):
            return None
        numeric_fields = {
            "actual_mode": (0, 0o777),
            "actual_uid": (0, 2**31 - 1),
            "entry_count": (0, 256),
            "expected_uid": (0, 2**31 - 1),
            "member_index": (0, 255),
        }
        for field, (minimum, maximum) in numeric_fields.items():
            value = context.get(field)
            if value is not None and (
                type(value) is not int
                or not minimum <= value <= maximum
            ):
                return None
        if context.get("expected_policy") not in {
            None,
            "owner_rwx_no_group_or_other_write",
        }:
            return None
        return {"schema_version": 1, **context}
