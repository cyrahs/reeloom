from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from enum import StrEnum

from psycopg_pool import ConnectionPool

from reeloom.kernel.errors import DomainError
from reeloom.kernel.forward_execution import (
    CURRENT_EXECUTION_OPERATION_SCHEMA_VERSION,
    ExecutionItemOutcome,
    ExecutionOperation,
    ExecutionOperationLease,
    ExecutionOperationStatus,
)
from reeloom.server.errors import ServerError, ServerErrorCode

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PLAN_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


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
                            hashtextextended(%s, 0)
                        )
                        """,
                        (operation.run_id,),
                    )
                    inserted = connection.execute(
                        """
                        INSERT INTO execution_operations_v2
                            (operation_id, schema_version, run_id, plan_hash,
                             approval_id, status, attempt_count, outcomes,
                             authorized_at, updated_at)
                        SELECT %s, 2, a.run_id, a.plan_hash, a.approval_id,
                               'authorized', 0, '[]'::jsonb, %s, %s
                        FROM approvals AS a
                        JOIN plan_lineage AS p
                          ON p.run_id = a.run_id
                         AND p.plan_hash = a.plan_hash
                        WHERE a.approval_id = %s
                          AND a.run_id = %s
                          AND a.plan_hash = %s
                          AND a.scope = 'apply'
                          AND a.expires_at > %s
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
                            now,
                            now,
                            approval_id,
                            operation.run_id,
                            operation.plan_hash,
                            now,
                        ),
                    ).fetchone()
                    if inserted is not None:
                        return _operation_from_row(inserted)
                    existing = connection.execute(
                        """
                        SELECT schema_version, operation_id, run_id,
                               plan_hash, status, attempt_count, outcomes,
                               approval_id
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
                    ):
                        return _operation_from_row(existing)
                    approval = connection.execute(
                        """
                        SELECT 1 FROM approvals
                        WHERE approval_id = %s AND run_id = %s
                          AND plan_hash = %s AND scope = 'apply'
                          AND expires_at > %s
                        """,
                        (
                            approval_id,
                            operation.run_id,
                            operation.plan_hash,
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
            worker_id=worker_id,
            now=now,
            lease_for=lease_for,
        )

    def claim(
        self,
        operation_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> ExecutionOperationLease | None:
        if not isinstance(operation_id, str) or not operation_id:
            raise ForwardOperationError(
                ForwardOperationErrorCode.INVALID_OPERATION
            )
        return self._claim(
            operation_id=operation_id,
            worker_id=worker_id,
            now=now,
            lease_for=lease_for,
        )

    def _claim(
        self,
        *,
        operation_id: str | None,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> ExecutionOperationLease | None:
        _validate_time(now)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        UPDATE execution_operations_v2
                        SET status = 'unavailable',
                            outcomes = '["unavailable"]'::jsonb,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            updated_at = %s
                        WHERE status = 'running'
                          AND lease_expires_at <= %s
                          AND attempt_count >= 100
                        """,
                        (now, now),
                    )
                    row = connection.execute(
                        """
                        SELECT schema_version, operation_id, run_id,
                               plan_hash, status, attempt_count, outcomes
                        FROM execution_operations_v2
                        WHERE attempt_count < 100
                          AND (%s IS NULL OR operation_id = %s)
                          AND (
                              status = 'authorized'
                              OR (
                                  status = 'running'
                                  AND lease_expires_at <= %s
                              )
                          )
                        ORDER BY authorized_at, operation_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                        """,
                        (operation_id, operation_id, now),
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
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

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
