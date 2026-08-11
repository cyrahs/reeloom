from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from psycopg_pool import ConnectionPool
from psycopg.errors import UniqueViolation

from reeloom.kernel.errors import DomainError
from reeloom.kernel.approval import ApprovalScope
from reeloom.kernel.forward_execution import (
    CURRENT_EXECUTION_OPERATION_SCHEMA_VERSION,
    ExecutionItemOutcome,
    ExecutionOperation,
    ExecutionOperationLease,
    ExecutionOperationStatus,
)
from reeloom.executor.forward import ForwardExecutionResult
from reeloom.executor.subtitle_publication import (
    SubtitlePublicationResult,
    SubtitlePublicationState,
)
from reeloom.executor.folder_housekeeping_v2 import housekeeping_target_name
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.subtitle_successor import subtitle_lineage_key

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PLAN_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_LOG = logging.getLogger(__name__)
_MAX_RECONCILIATION_ATTEMPTS = 5
_GENERATION_ACCEPT_TIMEOUT = timedelta(minutes=10)
_SUBTITLE_PUBLICATION_REASONS = frozenset(
    {
        "invalid_binding",
        "no_follow_unavailable",
        "directory_unreadable",
        "unexpected_entry",
        "casefold_collision",
        "invalid_complete_marker",
        "member_mismatch",
        "content_unavailable",
        "content_mismatch",
        "member_post_write_mismatch",
        "marker_post_write_mismatch",
        "exclusive_name_exists",
        "unsafe_path",
        "filesystem_unavailable",
    }
)


def _subtitle_failure_diagnostic(
    result: SubtitlePublicationResult,
) -> dict[str, object] | None:
    if result.state is SubtitlePublicationState.COMPLETED:
        return None
    reason = result.reason
    if reason not in _SUBTITLE_PUBLICATION_REASONS:
        reason = {
            SubtitlePublicationState.COLLISION: "collision",
            SubtitlePublicationState.UNSAFE: "unsafe",
            SubtitlePublicationState.UNAVAILABLE: "unavailable",
        }[result.state]
    return {
        "schema_version": 2,
        "stage": "publication",
        "reason": reason,
    }


class ForwardOperationErrorCode(StrEnum):
    INVALID_OPERATION = "invalid_operation"
    APPROVAL_UNAVAILABLE = "approval_unavailable"
    OPERATION_CONFLICT = "operation_conflict"
    LEASE_CONFLICT = "lease_conflict"
    OPERATION_NOT_FOUND = "operation_not_found"


class ForwardOperationError(RuntimeError):
    def __init__(self, code: ForwardOperationErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ForwardRescanClaim:
    operation_id: str
    run_id: str
    worker_id: str
    attempt_count: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class GenerationRequestClaim:
    request_id: str
    origin_run_id: str
    worker_id: str
    attempt_count: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class ForwardOperationView:
    operation: ExecutionOperation
    operation_kind: str = "media_move"
    items: tuple[dict[str, str | None], ...] = ()
    warnings: tuple[str, ...] = ()
    fresh_scan_required: bool = False
    rescan_state: str | None = None
    successor_run_id: str | None = None


def execution_operation_id(*, run_id: str, plan_hash: str) -> str:
    if (
        not isinstance(run_id, str)
        or _RUN_ID.fullmatch(run_id) is None
        or not isinstance(plan_hash, str)
        or _PLAN_HASH.fullmatch(plan_hash) is None
    ):
        raise ForwardOperationError(
            ForwardOperationErrorCode.INVALID_OPERATION
        )
    digest = hashlib.sha256(
        f"{run_id}\0{plan_hash}".encode("utf-8")
    ).hexdigest()
    return f"execution-operation-v2-{digest}"


def _generation_binding(operation_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(
        f"{operation_id}\0generation".encode("utf-8")
    ).hexdigest()
    return (
        f"generation-request-v2-{digest}",
        f"generation-v2-{digest}",
    )


def _enqueue_generation_request(
    connection: object,
    *,
    request_id: str,
    request_kind: str,
    origin_run_id: str,
    operation_id: str,
    watch_id: str,
    source_folder: str,
    expected_inventory_id: str,
    generation_nonce: str,
    lineage_key: str | None = None,
) -> None:
    """Persist a generation intent without endangering effect settlement.

    Only one queued/leased/accepted request may own a watched folder.  A
    competing operation is represented by its own blocked request so the
    operation result, handled inventory and run terminalization can still be
    committed atomically.  Retrying that request later never replays the
    filesystem effect.
    """

    connection.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO generation_requests_v2
            (request_id, request_kind, origin_run_id, operation_id,
             watch_id, source_folder, expected_inventory_id,
             generation_nonce, lineage_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            request_id,
            request_kind,
            origin_run_id,
            operation_id,
            watch_id,
            source_folder,
            expected_inventory_id,
            generation_nonce,
            lineage_key,
        ),
    )
    existing = connection.execute(  # type: ignore[attr-defined]
        """
        SELECT request_id
        FROM generation_requests_v2
        WHERE operation_id = %s
        """,
        (operation_id,),
    ).fetchone()
    if existing is not None:
        return
    connection.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO generation_requests_v2
            (request_id, request_kind, origin_run_id, operation_id,
             watch_id, source_folder, expected_inventory_id,
             generation_nonce, lineage_key, state, warning)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                'blocked', 'active_generation_conflict')
        ON CONFLICT (operation_id) DO NOTHING
        """,
        (
            request_id + "-blocked",
            request_kind,
            origin_run_id,
            operation_id,
            watch_id,
            source_folder,
            expected_inventory_id,
            generation_nonce + "-blocked",
            lineage_key,
        ),
    )


def _validate_time(value: datetime) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ForwardOperationError(
            ForwardOperationErrorCode.INVALID_OPERATION
        )


def _operation_from_row(row: object) -> ExecutionOperation:
    if not isinstance(row, (tuple, list)) or len(row) < 7:
        raise ForwardOperationError(
            ForwardOperationErrorCode.INVALID_OPERATION
        )
    try:
        return ExecutionOperation.restore(
            schema_version=str(row[0]),
            operation_id=row[1],
            run_id=row[2],
            plan_hash=row[3],
            status=row[4],
            attempt_count=row[5],
            outcomes=row[6],
        )
    except DomainError:
        raise ForwardOperationError(
            ForwardOperationErrorCode.INVALID_OPERATION
        ) from None


class PostgresForwardOperationRepository:
    """Single durable ledger for v2 execution and internal reconciliation."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def authorize(
        self,
        operation: ExecutionOperation,
        *,
        approval_id: str,
        now: datetime,
        scope: ApprovalScope = ApprovalScope.APPLY,
        operation_kind: str = "media_move",
    ) -> ExecutionOperation:
        _validate_time(now)
        if (
            not isinstance(operation, ExecutionOperation)
            or operation.status is not ExecutionOperationStatus.AUTHORIZED
            or operation.attempt_count != 0
            or operation.outcomes
            or operation.operation_id
            != execution_operation_id(
                run_id=operation.run_id,
                plan_hash=operation.plan_hash,
            )
            or not isinstance(approval_id, str)
            or not approval_id
            or not isinstance(scope, ApprovalScope)
            or operation_kind not in {"media_move", "subtitle_acquire"}
            or (
                operation_kind == "media_move"
                and scope is not ApprovalScope.APPLY
            )
            or (
                operation_kind == "subtitle_acquire"
                and scope is not ApprovalScope.SUBTITLE_ACQUIRE
            )
        ):
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        SELECT pg_advisory_xact_lock(
                            hashtextextended('reeloom-run:' || %s, 0)
                        )
                        """,
                        (operation.run_id,),
                    )
                    control = connection.execute(
                        """
                        SELECT revision, mode, effect_kind,
                               effect_plan_hash, effect_policy,
                               operation_id, run.status,
                               planning.run_id
                        FROM run_lifecycle_controls_v2 AS control
                        JOIN runs AS run USING (run_id)
                        LEFT JOIN planning_terminal_results_v2 AS planning
                          USING (run_id)
                        WHERE control.run_id = %s
                        FOR UPDATE OF control, run
                        """,
                        (operation.run_id,),
                    ).fetchone()
                    if (
                        control is None
                        or str(control[1]) != "forward_v2"
                        or str(control[2]) != operation_kind
                        or str(control[3]) != operation.plan_hash
                        or str(control[4]) not in {"manual", "automatic"}
                        or str(control[6]) not in {
                            "registered",
                            "running",
                            "awaiting_approval",
                            "applying",
                        }
                        or control[7] is not None
                        or (
                            control[5] is not None
                            and str(control[5]) != operation.operation_id
                        )
                    ):
                        raise ForwardOperationError(
                            ForwardOperationErrorCode.OPERATION_CONFLICT
                        )
                    inserted = connection.execute(
                        """
                        INSERT INTO execution_operations_v2
                            (operation_id, schema_version, run_id, plan_hash,
                             approval_id, operation_kind, status,
                             attempt_count, outcomes, authorized_at,
                             updated_at)
                        SELECT %s, 2, a.run_id, a.plan_hash, a.approval_id,
                               %s, 'authorized', 0, '[]'::jsonb, %s, %s
                        FROM approvals AS a
                        JOIN effect_plan_bindings_v2 AS p
                          ON p.run_id = a.run_id
                         AND p.plan_hash = a.plan_hash
                        WHERE a.approval_id = %s
                          AND a.run_id = %s
                          AND a.plan_hash = %s
                          AND a.scope = %s
                          AND p.plan_kind = %s
                          AND p.approval_scope = %s
                          AND a.expires_at > %s
                          AND NOT EXISTS (
                              SELECT 1
                              FROM planning_terminal_results_v2 AS terminal
                              WHERE terminal.run_id = a.run_id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM approval_claims AS old_claim
                              WHERE old_claim.approval_id = a.approval_id
                          )
                        ON CONFLICT DO NOTHING
                        RETURNING schema_version, operation_id, run_id,
                                  plan_hash, status, attempt_count, outcomes
                        """,
                        (
                            operation.operation_id,
                            operation_kind,
                            now,
                            now,
                            approval_id,
                            operation.run_id,
                            operation.plan_hash,
                            scope.value,
                            operation_kind,
                            scope.value,
                            now,
                        ),
                    ).fetchone()
                    if inserted is not None:
                        bound = connection.execute(
                            """
                            UPDATE run_lifecycle_controls_v2
                            SET operation_id = %s,
                                revision = revision + 1,
                                updated_at = %s
                            WHERE run_id = %s
                              AND revision = %s
                              AND operation_id IS NULL
                              AND effect_plan_hash = %s
                              AND effect_kind = %s
                            RETURNING operation_id
                            """,
                            (
                                operation.operation_id,
                                now,
                                operation.run_id,
                                int(control[0]),
                                operation.plan_hash,
                                operation_kind,
                            ),
                        ).fetchone()
                        if bound is None:
                            raise ForwardOperationError(
                                ForwardOperationErrorCode.OPERATION_CONFLICT
                            )
                        return _operation_from_row(inserted)
                    existing = connection.execute(
                        """
                        SELECT schema_version, operation_id, run_id,
                               plan_hash, status, attempt_count, outcomes,
                               approval_id, operation_kind
                        FROM execution_operations_v2
                        WHERE operation_id = %s
                        """,
                        (operation.operation_id,),
                    ).fetchone()
                    if (
                        existing is not None
                        and str(existing[7]) == approval_id
                        and str(existing[2]) == operation.run_id
                        and str(existing[3]) == operation.plan_hash
                        and str(existing[8]) == operation_kind
                        and str(control[5]) == operation.operation_id
                    ):
                        return _operation_from_row(existing)
                    approval = connection.execute(
                        """
                        SELECT 1 FROM approvals
                        WHERE approval_id = %s AND run_id = %s
                          AND plan_hash = %s AND scope = %s
                          AND expires_at > %s
                        """,
                        (
                            approval_id,
                            operation.run_id,
                            operation.plan_hash,
                            scope.value,
                            now,
                        ),
                    ).fetchone()
                    raise ForwardOperationError(
                        ForwardOperationErrorCode.APPROVAL_UNAVAILABLE
                        if approval is None
                        else ForwardOperationErrorCode.OPERATION_CONFLICT
                    )
        except ForwardOperationError:
            raise
        except UniqueViolation:
            raise ForwardOperationError(
                ForwardOperationErrorCode.OPERATION_CONFLICT
            ) from None
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> ExecutionOperationLease | None:
        return self._claim(
            operation_id=None,
            operation_kind="media_move",
            worker_id=worker_id,
            now=now,
            lease_for=lease_for,
        )

    def find_unstarted_automatic(
        self, *, operation_kind: str
    ) -> tuple[str, str] | None:
        """Find a durable automatic head not yet bound to an operation.

        Authorization itself takes the per-run advisory lock and is
        idempotent, so multiple service instances may observe the same head.
        """

        if operation_kind not in {"media_move", "subtitle_acquire"}:
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT control.run_id, control.effect_plan_hash
                    FROM run_lifecycle_controls_v2 AS control
                    LEFT JOIN planning_terminal_results_v2 AS terminal
                      ON terminal.run_id = control.run_id
                    WHERE control.mode = 'forward_v2'
                      AND control.effect_kind = %s
                      AND control.effect_policy = 'automatic'
                      AND control.operation_id IS NULL
                      AND terminal.run_id IS NULL
                      AND control.effect_plan_hash IS NOT NULL
                    ORDER BY control.updated_at, control.run_id
                    LIMIT 1
                    """,
                    (operation_kind,),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        if row is None:
            return None
        return str(row[0]), str(row[1])

    def fail_unstarted_automatic(
        self,
        *,
        run_id: str,
        plan_hash: str,
        reason_code: str,
        now: datetime,
        operation_kind: str = "media_move",
    ) -> bool:
        """Retire one poison automatic head without creating an operation."""

        _validate_time(now)
        if (
            not isinstance(run_id, str)
            or not run_id
            or not isinstance(plan_hash, str)
            or not plan_hash
            or not isinstance(reason_code, str)
            or not 1 <= len(reason_code.encode("utf-8")) <= 128
            or operation_kind not in {"media_move", "subtitle_acquire"}
        ):
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        SELECT pg_advisory_xact_lock(
                            hashtextextended('reeloom-run:' || %s, 0)
                        )
                        """,
                        (run_id,),
                    )
                    row = connection.execute(
                        """
                        SELECT control.revision, discovery.watch_id,
                               discovery.source_folder,
                               discovery.inventory_id
                        FROM run_lifecycle_controls_v2 AS control
                        JOIN runs AS run USING (run_id)
                        JOIN discoveries AS discovery USING (discovery_id)
                        WHERE control.run_id = %s
                          AND control.mode = 'forward_v2'
                          AND control.effect_kind = %s
                          AND control.effect_policy = 'automatic'
                          AND control.effect_plan_hash = %s
                          AND control.operation_id IS NULL
                          AND NOT EXISTS (
                              SELECT 1
                              FROM planning_terminal_results_v2 AS terminal
                              WHERE terminal.run_id = control.run_id
                          )
                        FOR UPDATE OF control, run
                        """,
                        (run_id, operation_kind, plan_hash),
                    ).fetchone()
                    if row is None:
                        return False
                    connection.execute(
                        """
                        INSERT INTO planning_terminal_results_v2
                            (run_id, plan_hash, outcome, reason_code,
                             source_disposition)
                        VALUES (%s, %s, 'agent_failed', %s, 'preserve')
                        """,
                        (run_id, plan_hash, reason_code),
                    )
                    if row[2] is not None and row[3] is not None:
                        connection.execute(
                            """
                            INSERT INTO handled_folder_inventories_v2
                                (watch_id, source_folder, inventory_id,
                                 run_id, terminal_status, handled_at)
                            VALUES (%s, %s, %s, %s, 'agent_failed', %s)
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                str(row[1]),
                                str(row[2]),
                                str(row[3]),
                                run_id,
                                now,
                            ),
                        )
                    connection.execute(
                        "UPDATE runs SET status = 'failed' WHERE run_id = %s",
                        (run_id,),
                    )
                    if operation_kind == "subtitle_acquire":
                        connection.execute(
                            """
                            UPDATE subtitle_acquisition_requests
                            SET status = 'blocked', failure_code = %s,
                                failure_diagnostic = NULL, updated_at = %s
                            WHERE run_id = %s AND plan_hash = %s
                              AND status IN ('planned', 'approved')
                            """,
                            (
                                reason_code,
                                now,
                                run_id,
                                plan_hash,
                            ),
                        )
                    self._project_terminal_run_state(
                        connection,
                        run_id=run_id,
                        completed=False,
                        failure_code=reason_code,
                    )
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'completed', boot_id = NULL,
                            updated_at = %s
                        WHERE run_id = %s
                          AND status IN ('pending', 'running')
                        """,
                        (now, run_id),
                    )
                    digest = hashlib.sha256(
                        f"{run_id}\0{reason_code}\0attention".encode("utf-8")
                    ).hexdigest()
                    connection.execute(
                        """
                        INSERT INTO notification_intents_v2
                            (intent_id, run_id, control_revision,
                             intent_kind, semantic_key)
                        VALUES (%s, %s, %s, 'attention_required', %s)
                        ON CONFLICT (semantic_key) DO NOTHING
                        """,
                        (
                            f"notification-intent-v2-{digest}",
                            run_id,
                            int(row[0]),
                            f"attention_required:{digest}",
                        ),
                    )
                    return True
        except (ForwardOperationError, ServerError):
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def claim_generation_request(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> GenerationRequestClaim | None:
        _validate_time(now)
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or lease_for <= timedelta(0)
        ):
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    accepted_deadline = now - _GENERATION_ACCEPT_TIMEOUT
                    connection.execute(
                        """
                        UPDATE generation_requests_v2
                        SET state = 'blocked', warning = 'accepted_timeout',
                            updated_at = %s
                        WHERE state = 'accepted'
                          AND successor_discovery_id IS NULL
                          AND accepted_at <= %s
                          AND attempt_count >= 5
                        """,
                        (now, accepted_deadline),
                    )
                    connection.execute(
                        """
                        UPDATE generation_requests_v2
                        SET state = 'queued', accepted_at = NULL,
                            available_at = %s, warning = NULL,
                            updated_at = %s
                        WHERE state = 'accepted'
                          AND successor_discovery_id IS NULL
                          AND accepted_at <= %s
                          AND attempt_count < 5
                        """,
                        (now, now, accepted_deadline),
                    )
                    connection.execute(
                        """
                        UPDATE generation_requests_v2
                        SET state = 'blocked',
                            warning = 'retry_exhausted',
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            updated_at = %s
                        WHERE state = 'leased'
                          AND lease_expires_at <= %s
                          AND attempt_count >= 5
                        """,
                        (now, now),
                    )
                    row = connection.execute(
                        """
                        SELECT request_id, origin_run_id, attempt_count
                        FROM generation_requests_v2
                        WHERE attempt_count < 5
                          AND (
                              state = 'queued'
                              OR (state = 'leased'
                                  AND lease_expires_at <= %s)
                          )
                          AND available_at <= %s
                        ORDER BY created_at, request_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                        """,
                        (now, now),
                    ).fetchone()
                    if row is None:
                        return None
                    expires = now + lease_for
                    attempt = int(row[2]) + 1
                    connection.execute(
                        """
                        UPDATE generation_requests_v2
                        SET state = 'leased', attempt_count = %s,
                            lease_owner = %s, lease_expires_at = %s,
                            updated_at = %s
                        WHERE request_id = %s
                        """,
                        (attempt, worker_id, expires, now, str(row[0])),
                    )
                    return GenerationRequestClaim(
                        request_id=str(row[0]),
                        origin_run_id=str(row[1]),
                        worker_id=worker_id,
                        attempt_count=attempt,
                        lease_expires_at=expires,
                    )
        except ForwardOperationError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def retry_generation_request(
        self,
        claim: GenerationRequestClaim,
        *,
        now: datetime,
        delay: timedelta,
        warning: str,
    ) -> None:
        _validate_time(now)
        if (
            not isinstance(claim, GenerationRequestClaim)
            or delay < timedelta(0)
            or not isinstance(warning, str)
            or not 1 <= len(warning.encode("utf-8")) <= 128
        ):
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        terminal = claim.attempt_count >= 5
        try:
            with self._pool.connection() as connection:
                updated = connection.execute(
                    """
                    UPDATE generation_requests_v2
                    SET state = %s,
                        warning = CASE WHEN %s THEN %s ELSE NULL END,
                        available_at = %s,
                        lease_owner = NULL, lease_expires_at = NULL,
                        updated_at = %s
                    WHERE request_id = %s AND state = 'leased'
                      AND attempt_count = %s
                      AND lease_owner = %s AND lease_expires_at = %s
                    RETURNING request_id
                    """,
                    (
                        "blocked" if terminal else "queued",
                        terminal,
                        warning,
                        now + delay,
                        now,
                        claim.request_id,
                        claim.attempt_count,
                        claim.worker_id,
                        claim.lease_expires_at,
                    ),
                ).fetchone()
                if updated is None:
                    raise ForwardOperationError(
                        ForwardOperationErrorCode.LEASE_CONFLICT
                    )
        except ForwardOperationError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def claim(
        self,
        operation_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
        operation_kind: str = "media_move",
    ) -> ExecutionOperationLease | None:
        if not isinstance(operation_id, str) or not operation_id:
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        return self._claim(
            operation_id=operation_id,
            operation_kind=operation_kind,
            worker_id=worker_id,
            now=now,
            lease_for=lease_for,
        )

    def renew_lease(
        self,
        lease: ExecutionOperationLease,
        *,
        now: datetime,
        lease_for: timedelta,
    ) -> ExecutionOperationLease:
        _validate_time(now)
        if (
            not isinstance(lease, ExecutionOperationLease)
            or not timedelta(seconds=1)
            <= lease_for
            <= timedelta(hours=1)
            or now >= lease.expires_at
        ):
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        expires = now + lease_for
        try:
            renewed = ExecutionOperationLease(
                operation=lease.operation,
                worker_id=lease.worker_id,
                expires_at=expires,
            )
        except DomainError:
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            ) from None
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "SET LOCAL lock_timeout = '5s'"
                    )
                    connection.execute(
                        "SET LOCAL statement_timeout = '5s'"
                    )
                    row = connection.execute(
                        """
                        UPDATE execution_operations_v2
                        SET lease_expires_at = %s, updated_at = %s
                        WHERE operation_id = %s
                          AND status = 'running'
                          AND attempt_count = %s
                          AND lease_owner = %s
                          AND lease_expires_at = %s
                          AND lease_expires_at > %s
                        RETURNING operation_id
                        """,
                        (
                            expires,
                            now,
                            lease.operation.operation_id,
                            lease.operation.attempt_count,
                            lease.worker_id,
                            lease.expires_at,
                            now,
                        ),
                    ).fetchone()
            if row is None:
                raise ForwardOperationError(
                    ForwardOperationErrorCode.LEASE_CONFLICT
                )
            return renewed
        except ForwardOperationError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def _claim(
        self,
        *,
        operation_id: str | None,
        operation_kind: str,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> ExecutionOperationLease | None:
        _validate_time(now)
        if operation_kind not in {"media_move", "subtitle_acquire"}:
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    self._settle_one_exhausted_locked(
                        connection,
                        operation_kind=operation_kind,
                        operation_id=operation_id,
                        now=now,
                    )
                    row = connection.execute(
                        """
                        SELECT operation.schema_version,
                               operation.operation_id, operation.run_id,
                               operation.plan_hash, operation.status,
                               operation.attempt_count, operation.outcomes
                        FROM execution_operations_v2 AS operation
                        JOIN run_lifecycle_controls_v2 AS control
                          ON control.run_id = operation.run_id
                         AND control.operation_id = operation.operation_id
                         AND control.mode = 'forward_v2'
                        WHERE operation.attempt_count < %s
                          AND operation.operation_kind = %s
                          AND (%s::text IS NULL
                               OR operation.operation_id = %s)
                          AND (
                              operation.status = 'authorized'
                              OR (
                                  operation.status = 'running'
                                  AND operation.lease_expires_at <= %s
                              )
                          )
                        ORDER BY operation.authorized_at,
                                 operation.operation_id
                        FOR UPDATE OF operation SKIP LOCKED
                        LIMIT 1
                        """,
                        (
                            _MAX_RECONCILIATION_ATTEMPTS,
                            operation_kind,
                            operation_id,
                            operation_id,
                            now,
                        ),
                    ).fetchone()
                    if row is None:
                        return None
                    operation = _operation_from_row(row)
                    try:
                        lease = ExecutionOperationLease.issue(
                            operation,
                            worker_id=worker_id,
                            now=now,
                            lease_for=lease_for,
                        )
                    except DomainError:
                        raise ForwardOperationError(
                            ForwardOperationErrorCode.INVALID_OPERATION
                        ) from None
                    connection.execute(
                        """
                        UPDATE execution_operations_v2
                        SET status = 'running', attempt_count = %s,
                            outcomes = '[]'::jsonb,
                            lease_owner = %s, lease_expires_at = %s,
                            updated_at = %s
                        WHERE operation_id = %s
                        """,
                        (
                            lease.operation.attempt_count,
                            lease.worker_id,
                            lease.expires_at,
                            now,
                            lease.operation.operation_id,
                        ),
                    )
                    return lease
        except (ForwardOperationError, ServerError):
            raise
        except Exception:
            _LOG.exception("forward_operation_claim_failed")
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def _settle_one_exhausted_locked(
        self,
        connection: object,
        *,
        operation_kind: str,
        operation_id: str | None,
        now: datetime,
    ) -> None:
        exhausted = connection.execute(  # type: ignore[attr-defined]
            """
            SELECT operation.operation_id, operation.run_id,
                   operation.plan_hash, discovery.discovery_id,
                   discovery.watch_id, discovery.source_folder,
                   discovery.inventory_id
            FROM execution_operations_v2 AS operation
            JOIN run_lifecycle_controls_v2 AS control
              ON control.run_id = operation.run_id
             AND control.operation_id = operation.operation_id
             AND control.mode = 'forward_v2'
            JOIN runs AS run ON run.run_id = operation.run_id
            JOIN discoveries AS discovery USING (discovery_id)
            WHERE operation.operation_kind = %s
              AND (%s::text IS NULL OR operation.operation_id = %s)
              AND operation.status = 'running'
              AND operation.lease_expires_at <= %s
              AND operation.attempt_count >= %s
            ORDER BY operation.authorized_at, operation.operation_id
            FOR UPDATE OF operation SKIP LOCKED
            LIMIT 1
            """,
            (
                operation_kind,
                operation_id,
                operation_id,
                now,
                _MAX_RECONCILIATION_ATTEMPTS,
            ),
        ).fetchone()
        if exhausted is None:
            return
        exhausted_operation_id = str(exhausted[0])
        run_id = str(exhausted[1])
        plan_hash = str(exhausted[2])
        connection.execute(  # type: ignore[attr-defined]
            """
            UPDATE execution_operations_v2
            SET status = 'unavailable',
                outcomes = '["unavailable"]'::jsonb,
                lease_owner = NULL, lease_expires_at = NULL,
                updated_at = %s
            WHERE operation_id = %s
            """,
            (now, exhausted_operation_id),
        )
        item = json.dumps(
            [
                {
                    "diagnostic": "transient_io",
                    "outcome": "unavailable",
                    "source_id": (
                        "subtitle-publication"
                        if operation_kind == "subtitle_acquire"
                        else "media-operation"
                    ),
                }
            ],
            separators=(",", ":"),
        )
        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO execution_operation_results_v2
                (operation_id, items, warnings,
                 fresh_scan_required, settled_at)
            VALUES (%s, %s::jsonb, '["retry_exhausted"]'::jsonb,
                    true, %s)
            ON CONFLICT (operation_id) DO NOTHING
            """,
            (exhausted_operation_id, item, now),
        )
        source_folder = exhausted[5]
        inventory_id = exhausted[6]
        if source_folder is not None and inventory_id is not None:
            request_id, nonce = _generation_binding(
                exhausted_operation_id
            )
            _enqueue_generation_request(
                connection,
                request_id=request_id,
                request_kind="operation_rescan",
                origin_run_id=run_id,
                operation_id=exhausted_operation_id,
                watch_id=str(exhausted[4]),
                source_folder=str(source_folder),
                expected_inventory_id=str(inventory_id),
                generation_nonce=nonce,
            )
            connection.execute(  # type: ignore[attr-defined]
                """
                INSERT INTO handled_folder_inventories_v2
                    (watch_id, source_folder, inventory_id, run_id,
                     operation_id, terminal_status, handled_at)
                VALUES (%s, %s, %s, %s, %s, 'unavailable', %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    str(exhausted[4]),
                    str(source_folder),
                    str(inventory_id),
                    run_id,
                    exhausted_operation_id,
                    now,
                ),
            )
        if operation_kind == "subtitle_acquire":
            connection.execute(  # type: ignore[attr-defined]
                """
                UPDATE subtitle_acquisition_requests
                SET status = 'blocked',
                    transaction_id = %s,
                    failure_code = 'root_unavailable',
                    updated_at = %s
                WHERE run_id = %s AND plan_hash = %s
                """,
                (exhausted_operation_id, now, run_id, plan_hash),
            )
        connection.execute(  # type: ignore[attr-defined]
            "UPDATE runs SET status = 'failed' WHERE run_id = %s",
            (run_id,),
        )
        self._project_terminal_run_state(
            connection,
            run_id=run_id,
            completed=False,
            failure_code="root_unavailable",
        )
        connection.execute(  # type: ignore[attr-defined]
            """
            UPDATE jobs SET status = 'completed', boot_id = NULL,
                            updated_at = %s
            WHERE run_id = %s AND status IN ('pending', 'running')
            """,
            (now, run_id),
        )
        self._notification_intent_for_operation(
            connection,
            operation_id=exhausted_operation_id,
            intent_kind="attention_required",
        )

    def settle(
        self,
        lease: ExecutionOperationLease,
        outcomes: tuple[ExecutionItemOutcome, ...],
        *,
        now: datetime,
    ) -> ExecutionOperation:
        _validate_time(now)
        if not isinstance(lease, ExecutionOperationLease):
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        try:
            settled = lease.settle(outcomes, now=now)
        except DomainError:
            raise ForwardOperationError(
                ForwardOperationErrorCode.LEASE_CONFLICT
            ) from None
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE execution_operations_v2
                        SET status = %s, outcomes = %s::jsonb,
                            lease_owner = NULL, lease_expires_at = NULL,
                            updated_at = %s
                        WHERE operation_id = %s
                          AND status = 'running'
                          AND attempt_count = %s
                          AND lease_owner = %s
                          AND lease_expires_at = %s
                          AND lease_expires_at > %s
                        RETURNING schema_version, operation_id, run_id,
                                  plan_hash, status, attempt_count, outcomes
                        """,
                        (
                            settled.status.value,
                            json.dumps(
                                [item.value for item in settled.outcomes],
                                separators=(",", ":"),
                            ),
                            now,
                            settled.operation_id,
                            settled.attempt_count,
                            lease.worker_id,
                            lease.expires_at,
                            now,
                        ),
                    ).fetchone()
                    if row is None:
                        raise ForwardOperationError(
                            ForwardOperationErrorCode.LEASE_CONFLICT
                        )
                    return _operation_from_row(row)
        except ForwardOperationError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def settle_result(
        self,
        lease: ExecutionOperationLease,
        result: ForwardExecutionResult,
        *,
        now: datetime,
    ) -> ExecutionOperation:
        """Atomically persist terminal truth and its durable rescan intent."""

        _validate_time(now)
        if (
            not isinstance(lease, ExecutionOperationLease)
            or not isinstance(result, ForwardExecutionResult)
            or result.operation.operation_id
            != lease.operation.operation_id
            or result.operation.attempt_count
            != lease.operation.attempt_count
        ):
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        try:
            settled = lease.settle(result.operation.outcomes, now=now)
        except DomainError:
            raise ForwardOperationError(
                ForwardOperationErrorCode.LEASE_CONFLICT
            ) from None
        if settled != result.operation:
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        items = [
            {
                "diagnostic": (
                    None
                    if item.diagnostic is None
                    else item.diagnostic.value
                ),
                "outcome": item.outcome.value,
                "source_id": str(item.source_id),
            }
            for item in result.items
        ]
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE execution_operations_v2
                        SET status = %s, outcomes = %s::jsonb,
                            lease_owner = NULL, lease_expires_at = NULL,
                            updated_at = %s
                        WHERE operation_id = %s
                          AND status = 'running'
                          AND attempt_count = %s
                          AND lease_owner = %s
                          AND lease_expires_at = %s
                          AND lease_expires_at > %s
                        RETURNING schema_version, operation_id, run_id,
                                  plan_hash, status, attempt_count, outcomes
                        """,
                        (
                            settled.status.value,
                            json.dumps(
                                [item.value for item in settled.outcomes],
                                separators=(",", ":"),
                            ),
                            now,
                            settled.operation_id,
                            settled.attempt_count,
                            lease.worker_id,
                            lease.expires_at,
                            now,
                        ),
                    ).fetchone()
                    if row is None:
                        raise ForwardOperationError(
                            ForwardOperationErrorCode.LEASE_CONFLICT
                        )
                    connection.execute(
                        """
                        INSERT INTO execution_operation_results_v2
                            (operation_id, items, warnings,
                             fresh_scan_required, settled_at)
                        VALUES (%s, %s::jsonb, %s::jsonb, %s, %s)
                        """,
                        (
                            settled.operation_id,
                            json.dumps(items, separators=(",", ":")),
                            json.dumps(
                                list(result.warnings),
                                separators=(",", ":"),
                            ),
                            result.fresh_scan_required,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE runs
                        SET status = CASE
                            WHEN %s = 'completed' THEN 'completed'
                            ELSE 'failed'
                        END
                        WHERE run_id = %s
                          AND status NOT IN ('superseded', 'completed')
                        """,
                        (settled.status.value, settled.run_id),
                    )
                    self._project_terminal_run_state(
                        connection,
                        run_id=settled.run_id,
                        completed=(
                            settled.status
                            is ExecutionOperationStatus.COMPLETED
                        ),
                        failure_code=(
                            None
                            if settled.status
                            is ExecutionOperationStatus.COMPLETED
                            else settled.status.value
                        ),
                    )
                    scope = connection.execute(
                        """
                        SELECT d.watch_id, d.source_folder, d.inventory_id,
                               r.config_revision, w.semantic_v2
                        FROM runs AS r
                        JOIN discoveries AS d USING (discovery_id)
                        JOIN watch_states AS w ON w.watch_id = d.watch_id
                        WHERE r.run_id = %s
                        """,
                        (settled.run_id,),
                    ).fetchone()
                    if (
                        scope is not None
                        and bool(scope[4])
                        and scope[1] is not None
                        and scope[2] is not None
                    ):
                        source_folder = str(scope[1])
                        if result.fresh_scan_required:
                            request_id, nonce = _generation_binding(
                                settled.operation_id
                            )
                            _enqueue_generation_request(
                                connection,
                                request_id=request_id,
                                request_kind="operation_rescan",
                                origin_run_id=settled.run_id,
                                operation_id=settled.operation_id,
                                watch_id=str(scope[0]),
                                source_folder=source_folder,
                                expected_inventory_id=str(scope[2]),
                                generation_nonce=nonce,
                            )
                        connection.execute(
                            """
                            INSERT INTO handled_folder_inventories_v2
                                (watch_id, source_folder, inventory_id,
                                 run_id, operation_id, terminal_status,
                                 handled_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                str(scope[0]),
                                source_folder,
                                str(scope[2]),
                                settled.run_id,
                                settled.operation_id,
                                settled.status.value,
                                now,
                            ),
                        )
                        if (
                            settled.status
                            is ExecutionOperationStatus.COMPLETED
                        ):
                            connection.execute(
                                """
                                INSERT INTO folder_housekeeping_v2
                                    (housekeeping_id, run_id, operation_id,
                                     config_revision, watch_id,
                                     source_folder, target_folder, action,
                                     created_at, updated_at)
                                VALUES (%s, %s, %s, %s, %s, %s, %s,
                                        'archive', %s, %s)
                                ON CONFLICT (run_id) DO NOTHING
                                """,
                                (
                                    "folder-housekeeping-v2-"
                                    + hashlib.sha256(
                                        settled.run_id.encode("utf-8")
                                    ).hexdigest(),
                                    settled.run_id,
                                    settled.operation_id,
                                    int(scope[3]),
                                    str(scope[0]),
                                    source_folder,
                                    housekeeping_target_name(
                                        source_folder, settled.run_id
                                    ),
                                    now,
                                    now,
                                ),
                            )
                    self._notification_intent_for_operation(
                        connection,
                        operation_id=settled.operation_id,
                        intent_kind=(
                            "operation_completed"
                            if settled.status
                            is ExecutionOperationStatus.COMPLETED
                            else "attention_required"
                        ),
                    )
                    return _operation_from_row(row)
        except ForwardOperationError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def settle_subtitle_result(
        self,
        lease: ExecutionOperationLease,
        result: SubtitlePublicationResult,
        *,
        origin_discovery_id: str,
        now: datetime,
    ) -> ExecutionOperation:
        """Settle subtitle publication in the shared v2 operation ledger."""

        _validate_time(now)
        if (
            not isinstance(lease, ExecutionOperationLease)
            or not isinstance(result, SubtitlePublicationResult)
            or not isinstance(origin_discovery_id, str)
            or not origin_discovery_id
        ):
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        outcome = {
            SubtitlePublicationState.COMPLETED: ExecutionItemOutcome.SATISFIED,
            SubtitlePublicationState.COLLISION: ExecutionItemOutcome.COLLISION,
            SubtitlePublicationState.UNSAFE: ExecutionItemOutcome.UNSAFE,
            SubtitlePublicationState.UNAVAILABLE: ExecutionItemOutcome.UNAVAILABLE,
        }[result.state]
        try:
            settled = lease.settle((outcome,), now=now)
        except DomainError:
            raise ForwardOperationError(
                ForwardOperationErrorCode.LEASE_CONFLICT
            ) from None
        failure_code = {
            SubtitlePublicationState.COMPLETED: None,
            SubtitlePublicationState.COLLISION: "destination_collision",
            SubtitlePublicationState.UNSAFE: "unsafe_entry",
            SubtitlePublicationState.UNAVAILABLE: "root_unavailable",
        }[result.state]
        failure_diagnostic = _subtitle_failure_diagnostic(result)
        item = {
            "diagnostic": {
                SubtitlePublicationState.COMPLETED: None,
                SubtitlePublicationState.COLLISION: "collision",
                SubtitlePublicationState.UNSAFE: "unsafe",
                SubtitlePublicationState.UNAVAILABLE: "transient_io",
            }[result.state],
            "outcome": outcome.value,
            "source_id": "subtitle-publication",
        }
        lineage_key = subtitle_lineage_key(origin_discovery_id)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    kind = connection.execute(
                        """
                        SELECT operation_kind
                        FROM execution_operations_v2
                        WHERE operation_id = %s
                        """,
                        (settled.operation_id,),
                    ).fetchone()
                    if kind is None or str(kind[0]) != "subtitle_acquire":
                        raise ForwardOperationError(
                            ForwardOperationErrorCode.INVALID_OPERATION
                        )
                    row = connection.execute(
                        """
                        UPDATE execution_operations_v2
                        SET status = %s, outcomes = %s::jsonb,
                            lease_owner = NULL, lease_expires_at = NULL,
                            updated_at = %s
                        WHERE operation_id = %s
                          AND status = 'running'
                          AND attempt_count = %s
                          AND lease_owner = %s
                          AND lease_expires_at = %s
                          AND lease_expires_at > %s
                        RETURNING schema_version, operation_id, run_id,
                                  plan_hash, status, attempt_count, outcomes
                        """,
                        (
                            settled.status.value,
                            json.dumps([outcome.value]),
                            now,
                            settled.operation_id,
                            settled.attempt_count,
                            lease.worker_id,
                            lease.expires_at,
                            now,
                        ),
                    ).fetchone()
                    if row is None:
                        raise ForwardOperationError(
                            ForwardOperationErrorCode.LEASE_CONFLICT
                        )
                    connection.execute(
                        """
                        INSERT INTO execution_operation_results_v2
                            (operation_id, items, warnings,
                             fresh_scan_required, settled_at)
                        VALUES (%s, %s::jsonb, '[]'::jsonb, true, %s)
                        """,
                        (
                            settled.operation_id,
                            json.dumps([item], separators=(",", ":")),
                            now,
                        ),
                    )
                    scope = connection.execute(
                        """
                        SELECT d.watch_id, d.source_folder, d.inventory_id
                        FROM runs AS r
                        JOIN discoveries AS d USING (discovery_id)
                        WHERE r.run_id = %s AND d.discovery_id = %s
                        """,
                        (settled.run_id, origin_discovery_id),
                    ).fetchone()
                    if scope is None:
                        raise ForwardOperationError(
                            ForwardOperationErrorCode.INVALID_OPERATION
                        )
                    request_id, nonce = _generation_binding(
                        settled.operation_id
                    )
                    _enqueue_generation_request(
                        connection,
                        request_id=request_id,
                        request_kind=(
                            "subtitle_successor"
                            if result.state
                            is SubtitlePublicationState.COMPLETED
                            else "operation_rescan"
                        ),
                        origin_run_id=settled.run_id,
                        operation_id=settled.operation_id,
                        watch_id=str(scope[0]),
                        source_folder=str(scope[1]),
                        expected_inventory_id=str(scope[2]),
                        generation_nonce=nonce,
                        lineage_key=(
                            lineage_key
                            if result.state
                            is SubtitlePublicationState.COMPLETED
                            else None
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO handled_folder_inventories_v2
                            (watch_id, source_folder, inventory_id,
                             run_id, operation_id, terminal_status,
                             handled_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            str(scope[0]),
                            str(scope[1]),
                            str(scope[2]),
                            settled.run_id,
                            settled.operation_id,
                            settled.status.value,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE subtitle_acquisition_requests
                        SET status = %s, failure_code = %s,
                            transaction_id = %s,
                            failure_diagnostic = %s::jsonb,
                            updated_at = %s
                        WHERE run_id = %s AND plan_hash = %s
                        """,
                        (
                            (
                                "published"
                                if result.state
                                is SubtitlePublicationState.COMPLETED
                                else "blocked"
                            ),
                            failure_code,
                            settled.operation_id,
                            (
                                None
                                if failure_diagnostic is None
                                else json.dumps(
                                    failure_diagnostic,
                                    separators=(",", ":"),
                                )
                            ),
                            now,
                            settled.run_id,
                            settled.plan_hash,
                        ),
                    )
                    if result.state is SubtitlePublicationState.COMPLETED:
                        connection.execute(
                            """
                            INSERT INTO subtitle_acquisition_lineages
                                (lineage_key, root_discovery_id)
                            VALUES (%s, %s)
                            ON CONFLICT DO NOTHING
                            """,
                            (lineage_key, origin_discovery_id),
                        )
                    connection.execute(
                        """
                        UPDATE runs
                        SET status = %s,
                            subtitle_acquisition_lineage_key = CASE
                                WHEN %s::text IS NULL
                                THEN %s
                                ELSE subtitle_acquisition_lineage_key
                            END
                        WHERE run_id = %s
                        """,
                        (
                            (
                                "superseded"
                                if result.state
                                is SubtitlePublicationState.COMPLETED
                                else "failed"
                            ),
                            failure_code,
                            lineage_key,
                            settled.run_id,
                        ),
                    )
                    self._project_terminal_run_state(
                        connection,
                        run_id=settled.run_id,
                        completed=(
                            result.state
                            is SubtitlePublicationState.COMPLETED
                        ),
                        failure_code=failure_code,
                    )
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'completed', boot_id = NULL,
                            updated_at = %s
                        WHERE run_id = %s
                          AND status IN ('pending', 'running')
                        """,
                        (now, settled.run_id),
                    )
                    self._notification_intent_for_operation(
                        connection,
                        operation_id=settled.operation_id,
                        intent_kind=(
                            "operation_completed"
                            if result.state
                            is SubtitlePublicationState.COMPLETED
                            else "attention_required"
                        ),
                    )
                    return _operation_from_row(row)
        except ForwardOperationError:
            raise
        except Exception:
            _LOG.exception("subtitle_operation_settlement_failed")
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def settle_exhausted_subtitle(
        self,
        operation_id: str,
        *,
        origin_discovery_id: str,
        now: datetime,
    ) -> ExecutionOperation:
        """Project a lease-exhausted subtitle operation into one terminal truth.

        The claim path deliberately stops after a bounded number of attempts.
        This method closes the remaining control-plane state without performing
        another filesystem effect, so the run can be rescanned or deleted.
        """

        _validate_time(now)
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or not isinstance(origin_discovery_id, str)
            or not origin_discovery_id
        ):
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        item = {
            "diagnostic": "transient_io",
            "outcome": ExecutionItemOutcome.UNAVAILABLE.value,
            "source_id": "subtitle-publication",
        }
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT schema_version, operation_id, run_id,
                               plan_hash, status, attempt_count, outcomes
                        FROM execution_operations_v2
                        WHERE operation_id = %s
                          AND operation_kind = 'subtitle_acquire'
                          AND status = 'unavailable'
                        FOR UPDATE
                        """,
                        (operation_id,),
                    ).fetchone()
                    if row is None:
                        raise ForwardOperationError(
                            ForwardOperationErrorCode.OPERATION_CONFLICT
                        )
                    operation = _operation_from_row(row)
                    existing = connection.execute(
                        """
                        SELECT 1 FROM execution_operation_results_v2
                        WHERE operation_id = %s
                        """,
                        (operation_id,),
                    ).fetchone()
                    if existing is not None:
                        self._notification_intent_for_operation(
                            connection,
                            operation_id=operation_id,
                            intent_kind="attention_required",
                        )
                        return operation
                    scope = connection.execute(
                        """
                        SELECT d.watch_id, d.source_folder, d.inventory_id
                        FROM runs AS r
                        JOIN discoveries AS d USING (discovery_id)
                        WHERE r.run_id = %s AND d.discovery_id = %s
                        """,
                        (operation.run_id, origin_discovery_id),
                    ).fetchone()
                    if scope is None:
                        raise ForwardOperationError(
                            ForwardOperationErrorCode.INVALID_OPERATION
                        )
                    connection.execute(
                        """
                        INSERT INTO execution_operation_results_v2
                            (operation_id, items, warnings,
                             fresh_scan_required, settled_at)
                        VALUES (%s, %s::jsonb, '[]'::jsonb, true, %s)
                        """,
                        (
                            operation_id,
                            json.dumps([item], separators=(",", ":")),
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO handled_folder_inventories_v2
                            (watch_id, source_folder, inventory_id,
                             run_id, operation_id, terminal_status,
                             handled_at)
                        VALUES (%s, %s, %s, %s, %s, 'unavailable', %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            str(scope[0]),
                            str(scope[1]),
                            str(scope[2]),
                            operation.run_id,
                            operation_id,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE subtitle_acquisition_requests
                        SET status = 'blocked',
                            failure_code = 'root_unavailable',
                            transaction_id = %s, updated_at = %s
                        WHERE run_id = %s AND plan_hash = %s
                        """,
                        (
                            operation_id,
                            now,
                            operation.run_id,
                            operation.plan_hash,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE runs SET status = 'failed'
                        WHERE run_id = %s
                          AND status NOT IN ('completed', 'superseded')
                        """,
                        (operation.run_id,),
                    )
                    self._project_terminal_run_state(
                        connection,
                        run_id=operation.run_id,
                        completed=False,
                        failure_code="root_unavailable",
                    )
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'completed', boot_id = NULL,
                            updated_at = %s
                        WHERE run_id = %s
                          AND status IN ('pending', 'running')
                        """,
                        (now, operation.run_id),
                    )
                    self._notification_intent_for_operation(
                        connection,
                        operation_id=operation_id,
                        intent_kind="attention_required",
                    )
                    return operation
        except ForwardOperationError:
            raise
        except Exception:
            _LOG.exception("subtitle_operation_exhaustion_settlement_failed")
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    @staticmethod
    def _notification_intent_for_operation(
        connection: object,
        *,
        operation_id: str,
        intent_kind: str,
    ) -> None:
        digest = hashlib.sha256(
            f"{operation_id}\0{intent_kind}".encode("utf-8")
        ).hexdigest()
        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO notification_intents_v2
                (intent_id, run_id, control_revision, operation_id,
                 intent_kind, semantic_key)
            SELECT %s, operation.run_id, control.revision,
                   operation.operation_id, %s, %s
            FROM execution_operations_v2 AS operation
            JOIN run_lifecycle_controls_v2 AS control
              ON control.operation_id = operation.operation_id
            WHERE operation.operation_id = %s
            ON CONFLICT (semantic_key) DO NOTHING
            """,
            (
                f"notification-intent-v2-{digest}",
                intent_kind,
                f"{intent_kind}:{operation_id}",
                operation_id,
            ),
        )

    @staticmethod
    def _project_terminal_run_state(
        connection: object,
        *,
        run_id: str,
        completed: bool,
        failure_code: str | None,
    ) -> None:
        phase = "completed" if completed else "failed"
        status = "stopped" if completed else "failed"
        connection.execute(  # type: ignore[attr-defined]
            """
            UPDATE run_states
            SET phase = %s, runtime_status = %s,
                projection_payload = projection_payload
                    || jsonb_build_object(
                        'phase', %s::text, 'status', %s::text,
                        'stop_reason', CASE WHEN %s::boolean
                            THEN NULL ELSE 'fatal_error' END,
                        'failure_code', %s::text
                    ),
                updated_at = clock_timestamp()
            WHERE run_id = %s
            """,
            (
                phase,
                status,
                phase,
                status,
                completed,
                failure_code,
                run_id,
            ),
        )

    def get_view(self, operation_id: str) -> ForwardOperationView:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT o.schema_version, o.operation_id, o.run_id,
                           o.plan_hash, o.status, o.attempt_count, o.outcomes,
                           r.items, r.warnings, r.fresh_scan_required,
                           g.state, g.successor_run_id,
                           h.state, h.warning, o.operation_kind
                    FROM execution_operations_v2 AS o
                    LEFT JOIN execution_operation_results_v2 AS r
                      ON r.operation_id = o.operation_id
                    LEFT JOIN generation_requests_v2 AS g
                      ON g.operation_id = o.operation_id
                    LEFT JOIN folder_housekeeping_v2 AS h
                      ON h.operation_id = o.operation_id
                    WHERE o.operation_id = %s
                    """,
                    (operation_id,),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        if row is None:
            raise ForwardOperationError(
                ForwardOperationErrorCode.OPERATION_NOT_FOUND
            )
        operation = _operation_from_row(row)
        try:
            raw_items = () if row[7] is None else tuple(row[7])
            items = tuple(
                {
                    "source_id": str(item["source_id"]),
                    "outcome": str(item["outcome"]),
                    "diagnostic": (
                        None
                        if item.get("diagnostic") is None
                        else str(item["diagnostic"])
                    ),
                }
                for item in raw_items
            )
            warnings = list(
                () if row[8] is None else tuple(str(item) for item in row[8])
            )
            if row[12] == "warning" and row[13] is not None:
                warnings.append("housekeeping:" + str(row[13]))
            return ForwardOperationView(
                operation=operation,
                operation_kind=str(row[14]),
                items=items,
                warnings=tuple(sorted(set(warnings))),
                fresh_scan_required=(False if row[9] is None else bool(row[9])),
                rescan_state=None if row[10] is None else str(row[10]),
                successor_run_id=None if row[11] is None else str(row[11]),
            )
        except (KeyError, TypeError, ValueError):
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            ) from None

    def claim_rescan(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
        operation_id: str | None = None,
    ) -> ForwardRescanClaim | None:
        _validate_time(now)
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or len(worker_id.encode("utf-8")) > 128
            or not isinstance(lease_for, timedelta)
            or not timedelta(seconds=1) <= lease_for <= timedelta(hours=1)
            or (
                operation_id is not None
                and (not isinstance(operation_id, str) or not operation_id)
            )
        ):
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        UPDATE execution_rescan_outbox_v2
                        SET state = 'blocked', lease_owner = NULL,
                            lease_expires_at = NULL, dispatched_at = %s,
                            last_error = 'retry_exhausted', updated_at = %s
                        WHERE state IN ('queued', 'retry_wait', 'leased')
                          AND attempt_count >= 100
                          AND (state <> 'leased' OR lease_expires_at <= %s)
                        """,
                        (now, now, now),
                    )
                    row = connection.execute(
                        """
                        SELECT operation_id, run_id, attempt_count
                        FROM execution_rescan_outbox_v2
                        WHERE available_at <= %s
                          AND (%s::text IS NULL OR operation_id = %s)
                          AND (
                              state IN ('queued', 'retry_wait')
                              OR (state = 'leased' AND lease_expires_at <= %s)
                          )
                          AND attempt_count < 100
                        ORDER BY available_at, created_at, operation_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                        """,
                        (now, operation_id, operation_id, now),
                    ).fetchone()
                    if row is None:
                        return None
                    expires = now + lease_for
                    attempt = int(row[2]) + 1
                    connection.execute(
                        """
                        UPDATE execution_rescan_outbox_v2
                        SET state = 'leased', attempt_count = %s,
                            lease_owner = %s, lease_expires_at = %s,
                            last_error = NULL, updated_at = %s
                        WHERE operation_id = %s
                        """,
                        (attempt, worker_id, expires, now, str(row[0])),
                    )
                    return ForwardRescanClaim(
                        operation_id=str(row[0]),
                        run_id=str(row[1]),
                        worker_id=worker_id,
                        attempt_count=attempt,
                        lease_expires_at=expires,
                    )
        except ForwardOperationError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def requeue_rescan(
        self,
        *,
        run_id: str,
        plan_hash: str,
        now: datetime,
    ) -> None:
        """Re-read current state; never resurrect the terminal operation."""

        _validate_time(now)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT operation.operation_id, discovery.watch_id,
                               discovery.source_folder,
                               discovery.inventory_id,
                               request.state, request.successor_run_id,
                               operation.status
                        FROM execution_operations_v2 AS operation
                        JOIN run_lifecycle_controls_v2 AS control
                          ON control.operation_id = operation.operation_id
                        JOIN runs AS run ON run.run_id = operation.run_id
                        JOIN discoveries AS discovery USING (discovery_id)
                        LEFT JOIN generation_requests_v2 AS request
                          ON request.operation_id = operation.operation_id
                        WHERE operation.run_id = %s
                          AND operation.plan_hash = %s
                          AND operation.status IN (
                              'partial', 'stale', 'collision', 'unsafe',
                              'unavailable', 'completed'
                          )
                        FOR UPDATE OF operation, control
                        """,
                        (run_id, plan_hash),
                    ).fetchone()
                    if (
                        row is None
                        or row[2] is None
                        or row[3] is None
                        or row[5] is not None
                    ):
                        raise ForwardOperationError(
                            ForwardOperationErrorCode.OPERATION_CONFLICT
                        )
                    operation_id = str(row[0])
                    if row[6] == "completed" and row[4] != "blocked":
                        raise ForwardOperationError(
                            ForwardOperationErrorCode.OPERATION_CONFLICT
                        )
                    if row[4] in {"queued", "leased", "accepted"}:
                        return
                    if row[4] == "completed":
                        raise ForwardOperationError(
                            ForwardOperationErrorCode.OPERATION_CONFLICT
                        )
                    if row[4] == "blocked":
                        conflict = connection.execute(
                            """
                            SELECT 1
                            FROM generation_requests_v2
                            WHERE watch_id = %s AND source_folder = %s
                              AND state IN ('queued', 'leased', 'accepted')
                              AND operation_id IS DISTINCT FROM %s
                            LIMIT 1
                            """,
                            (str(row[1]), str(row[2]), operation_id),
                        ).fetchone()
                        if conflict is not None:
                            raise ForwardOperationError(
                                ForwardOperationErrorCode.OPERATION_CONFLICT
                            )
                        connection.execute(
                            """
                            UPDATE generation_requests_v2
                            SET state = 'queued', attempt_count = 0,
                                available_at = %s, warning = NULL,
                                accepted_at = NULL,
                                lease_owner = NULL,
                                lease_expires_at = NULL,
                                updated_at = %s
                            WHERE operation_id = %s AND state = 'blocked'
                            """,
                            (now, now, operation_id),
                        )
                        return
                    request_id, nonce = _generation_binding(operation_id)
                    connection.execute(
                        """
                        INSERT INTO generation_requests_v2
                            (request_id, request_kind, origin_run_id,
                             operation_id, watch_id, source_folder,
                             expected_inventory_id, generation_nonce)
                        VALUES (%s, 'operation_rescan', %s, %s,
                                %s, %s, %s, %s)
                        """,
                        (
                            request_id,
                            run_id,
                            operation_id,
                            str(row[1]),
                            str(row[2]),
                            str(row[3]),
                            nonce,
                        ),
                    )
        except ForwardOperationError:
            raise
        except UniqueViolation:
            raise ForwardOperationError(
                ForwardOperationErrorCode.OPERATION_CONFLICT
            ) from None
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def complete_rescan(
        self,
        claim: ForwardRescanClaim,
        *,
        now: datetime,
    ) -> None:
        self._finish_rescan(claim, now=now, state="completed")

    def retry_rescan(
        self,
        claim: ForwardRescanClaim,
        *,
        now: datetime,
        delay: timedelta,
        error: str,
    ) -> None:
        if (
            not isinstance(delay, timedelta)
            or not timedelta(seconds=1) <= delay <= timedelta(hours=1)
            or not isinstance(error, str)
            or not error
            or len(error.encode("utf-8")) > 128
        ):
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        self._finish_rescan(
            claim,
            now=now,
            state="retry_wait",
            available_at=now + delay,
            error=error,
        )

    def _finish_rescan(
        self,
        claim: ForwardRescanClaim,
        *,
        now: datetime,
        state: str,
        available_at: datetime | None = None,
        error: str | None = None,
    ) -> None:
        _validate_time(now)
        if not isinstance(claim, ForwardRescanClaim):
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE execution_rescan_outbox_v2
                        SET state = %s, lease_owner = NULL,
                            lease_expires_at = NULL,
                            available_at = COALESCE(%s, available_at),
                            dispatched_at = CASE
                                WHEN %s = 'completed' THEN %s
                                ELSE dispatched_at
                            END,
                            last_error = %s, updated_at = %s
                        WHERE operation_id = %s AND state = 'leased'
                          AND lease_owner = %s AND attempt_count = %s
                          AND lease_expires_at = %s
                          AND lease_expires_at > %s
                        RETURNING operation_id
                        """,
                        (
                            state,
                            available_at,
                            state,
                            now,
                            error,
                            now,
                            claim.operation_id,
                            claim.worker_id,
                            claim.attempt_count,
                            claim.lease_expires_at,
                            now,
                        ),
                    ).fetchone()
                    if row is None:
                        raise ForwardOperationError(
                            ForwardOperationErrorCode.LEASE_CONFLICT
                        )
        except ForwardOperationError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def get(self, operation_id: str) -> ExecutionOperation:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT schema_version, operation_id, run_id, plan_hash,
                           status, attempt_count, outcomes
                    FROM execution_operations_v2
                    WHERE operation_id = %s
                    """,
                    (operation_id,),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        if row is None:
            raise ForwardOperationError(
                ForwardOperationErrorCode.OPERATION_NOT_FOUND
            )
        return _operation_from_row(row)
