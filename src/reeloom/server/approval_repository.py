from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from psycopg_pool import ConnectionPool

from reeloom.executor.errors import ApprovalError, ApprovalErrorCode
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope


def _now() -> datetime:
    return datetime.now(UTC)


class PostgresApprovalStore:
    """Immutable exact approvals with one append-only claim."""

    def __init__(
        self,
        pool: ConnectionPool,
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._pool = pool
        self._clock = clock

    def issue(self, approval: ApprovalRecord) -> None:
        if not isinstance(approval, ApprovalRecord) or not approval.verify_id():
            raise ApprovalError(ApprovalErrorCode.INVALID_RECORD)
        if approval.is_expired(self._clock()):
            raise ApprovalError(ApprovalErrorCode.EXPIRED)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        INSERT INTO approvals
                            (approval_id, run_id, plan_hash, scope,
                             expires_at, canonical_record)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (approval_id) DO NOTHING
                        RETURNING approval_id
                        """,
                        (
                            approval.approval_id,
                            approval.run_id,
                            approval.plan_hash,
                            approval.scope.value,
                            approval.expires_at,
                            approval.canonical_bytes(),
                        ),
                    ).fetchone()
                    if row is None:
                        existing = self._load(
                            connection,
                            approval.approval_id,
                        )
                        if (
                            existing.canonical_bytes()
                            != approval.canonical_bytes()
                        ):
                            raise ApprovalError(
                                ApprovalErrorCode.ALREADY_EXISTS
                            )
        except ApprovalError:
            raise
        except Exception:
            raise ApprovalError(
                ApprovalErrorCode.STORE_FAILURE
            ) from None

    def issue_or_reuse(
        self,
        approval: ApprovalRecord,
    ) -> ApprovalRecord:
        """Reuse one live unclaimed approval or append a replacement."""

        if not isinstance(approval, ApprovalRecord) or not approval.verify_id():
            raise ApprovalError(ApprovalErrorCode.INVALID_RECORD)
        if approval.is_expired(self._clock()):
            raise ApprovalError(ApprovalErrorCode.EXPIRED)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        SELECT pg_advisory_xact_lock(
                            hashtextextended(%s, 0)
                        )
                        """,
                        (approval.run_id,),
                    )
                    claimed = connection.execute(
                        """
                        SELECT approval_id FROM (
                            SELECT c.approval_id
                            FROM approval_claims AS c
                            WHERE c.run_id = %s AND c.plan_hash = %s
                            UNION ALL
                            SELECT o.approval_id
                            FROM execution_operations_v2 AS o
                            WHERE o.run_id = %s AND o.plan_hash = %s
                        ) AS consumed
                        LIMIT 1
                        """,
                        (
                            approval.run_id,
                            approval.plan_hash,
                            approval.run_id,
                            approval.plan_hash,
                        ),
                    ).fetchone()
                    if claimed is not None:
                        raise ApprovalError(
                            ApprovalErrorCode.ALREADY_CLAIMED,
                            context={"approval_id": str(claimed[0])},
                        )
                    row = connection.execute(
                        """
                        SELECT a.canonical_record
                        FROM approvals AS a
                        LEFT JOIN approval_claims AS c
                          ON c.approval_id = a.approval_id
                        LEFT JOIN execution_operations_v2 AS o
                          ON o.approval_id = a.approval_id
                        WHERE a.run_id = %s
                          AND a.plan_hash = %s
                          AND a.expires_at > %s
                          AND c.approval_id IS NULL
                          AND o.approval_id IS NULL
                        ORDER BY a.issued_at DESC, a.approval_id DESC
                        LIMIT 1
                        """,
                        (
                            approval.run_id,
                            approval.plan_hash,
                            self._clock(),
                        ),
                    ).fetchone()
                    if row is not None:
                        existing = ApprovalRecord.from_canonical_bytes(
                            bytes(row[0])
                        )
                        self._validate_binding(
                            existing,
                            run_id=approval.run_id,
                            plan_hash=approval.plan_hash,
                            scope=approval.scope,
                        )
                        if not existing.verify_id():
                            raise ApprovalError(
                                ApprovalErrorCode.INVALID_RECORD
                            )
                        return existing
                    connection.execute(
                        """
                        INSERT INTO approvals
                            (approval_id, run_id, plan_hash, scope,
                             expires_at, canonical_record)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            approval.approval_id,
                            approval.run_id,
                            approval.plan_hash,
                            approval.scope.value,
                            approval.expires_at,
                            approval.canonical_bytes(),
                        ),
                    )
                    return approval
        except ApprovalError:
            raise
        except Exception:
            raise ApprovalError(
                ApprovalErrorCode.STORE_FAILURE
            ) from None

    def claim(
        self,
        *,
        approval_id: str,
        run_id: str,
        plan_hash: str,
        scope: ApprovalScope,
    ) -> ApprovalRecord:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        SELECT pg_advisory_xact_lock(
                            hashtextextended(%s, 0)
                        )
                        """,
                        (run_id,),
                    )
                    approval = self._load(connection, approval_id)
                    self._validate_binding(
                        approval,
                        run_id=run_id,
                        plan_hash=plan_hash,
                        scope=scope,
                    )
                    if approval.is_expired(self._clock()):
                        raise ApprovalError(ApprovalErrorCode.EXPIRED)
                    v2_claim = connection.execute(
                        """
                        SELECT approval_id
                        FROM execution_operations_v2
                        WHERE run_id = %s AND plan_hash = %s
                        """,
                        (run_id, plan_hash),
                    ).fetchone()
                    if v2_claim is not None:
                        raise ApprovalError(
                            ApprovalErrorCode.ALREADY_CLAIMED
                        )
                    inserted = connection.execute(
                        """
                        INSERT INTO approval_claims
                            (approval_id, run_id, plan_hash)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (approval_id) DO NOTHING
                        RETURNING approval_id
                        """,
                        (approval_id, run_id, plan_hash),
                    ).fetchone()
                    if inserted is None:
                        raise ApprovalError(
                            ApprovalErrorCode.ALREADY_CLAIMED
                        )
                    return approval
        except ApprovalError:
            raise
        except Exception:
            raise ApprovalError(
                ApprovalErrorCode.STORE_FAILURE
            ) from None

    def require_claim(
        self,
        *,
        approval_id: str,
        run_id: str,
        plan_hash: str,
        scope: ApprovalScope,
    ) -> ApprovalRecord:
        resolved = self._resolve_claim(
            approval_id=approval_id,
            run_id=run_id,
            plan_hash=plan_hash,
            scope=scope,
        )
        if resolved is None:
            raise ApprovalError(ApprovalErrorCode.NOT_FOUND)
        return resolved

    def claimed_id(
        self,
        *,
        run_id: str,
        plan_hash: str,
    ) -> str | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT approval_id FROM approval_claims
                    WHERE run_id = %s AND plan_hash = %s
                    """,
                    (run_id, plan_hash),
                ).fetchone()
        except Exception:
            raise ApprovalError(
                ApprovalErrorCode.STORE_FAILURE
            ) from None
        return None if row is None else str(row[0])

    def _resolve_claim(
        self,
        *,
        approval_id: str,
        run_id: str,
        plan_hash: str,
        scope: ApprovalScope,
    ) -> ApprovalRecord | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT a.canonical_record
                    FROM approval_claims AS c
                    JOIN approvals AS a USING (approval_id)
                    WHERE c.approval_id = %s
                    """,
                    (approval_id,),
                ).fetchone()
        except Exception:
            return None
        if row is None:
            return None
        try:
            approval = ApprovalRecord.from_canonical_bytes(bytes(row[0]))
            self._validate_binding(
                approval,
                run_id=run_id,
                plan_hash=plan_hash,
                scope=scope,
            )
            return approval
        except Exception:
            return None

    @staticmethod
    def _load(
        connection: object,
        approval_id: str,
    ) -> ApprovalRecord:
        row = connection.execute(
            """
            SELECT canonical_record FROM approvals
            WHERE approval_id = %s
            """,
            (approval_id,),
        ).fetchone()
        if row is None:
            raise ApprovalError(ApprovalErrorCode.NOT_FOUND)
        try:
            return ApprovalRecord.from_canonical_bytes(bytes(row[0]))
        except Exception:
            raise ApprovalError(
                ApprovalErrorCode.INVALID_RECORD
            ) from None

    @staticmethod
    def _validate_binding(
        approval: ApprovalRecord,
        *,
        run_id: str,
        plan_hash: str,
        scope: ApprovalScope,
    ) -> None:
        if (
            approval.run_id != run_id
            or approval.plan_hash != plan_hash
            or approval.scope is not scope
        ):
            raise ApprovalError(ApprovalErrorCode.BINDING_MISMATCH)
