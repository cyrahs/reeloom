from __future__ import annotations

import uuid
from datetime import datetime

from psycopg_pool import ConnectionPool

from reeloom.server.errors import ServerError, ServerErrorCode

_TERMINAL_STATUSES = frozenset({"completed", "failed", "rolled_back"})


class PostgresRunDeletionService:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def delete(self, run_id: str) -> dict[str, object]:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT r.status,
                               deletion.deleted_at,
                               EXISTS (
                                   SELECT 1 FROM run_operations
                                   WHERE run_id = r.run_id
                               ),
                               EXISTS (
                                   SELECT 1 FROM interactions
                                   WHERE run_id = r.run_id
                                     AND status = 'active'
                               ),
                               EXISTS (
                                   SELECT 1
                                   FROM approval_claims AS claim
                                   LEFT JOIN approval_settlements AS settled
                                     ON settled.approval_id =
                                        claim.approval_id
                                   WHERE claim.run_id = r.run_id
                                     AND settled.approval_id IS NULL
                               ),
                               EXISTS (
                                   SELECT 1
                                   FROM folder_disposition_approvals AS approval
                                   JOIN folder_disposition_claims AS claim
                                     ON claim.approval_id =
                                        approval.approval_id
                                   LEFT JOIN folder_disposition_settlements
                                        AS settled
                                     ON settled.approval_id =
                                        claim.approval_id
                                   WHERE approval.run_id = r.run_id
                                     AND settled.approval_id IS NULL
                               ),
                               d.folder_generation_id,
                               folder.status
                        FROM runs AS r
                        JOIN discoveries AS d
                          ON d.discovery_id = r.discovery_id
                        LEFT JOIN run_deletions AS deletion
                          ON deletion.run_id = r.run_id
                        LEFT JOIN watch_folder_observations AS folder
                          ON folder.discovery_id = d.discovery_id
                        WHERE r.run_id = %s
                        FOR UPDATE OF r
                        """,
                        (run_id,),
                    ).fetchone()
                    if row is None:
                        raise ServerError(ServerErrorCode.RUN_NOT_FOUND)
                    if row[1] is not None:
                        return self._result(run_id, row[1])
                    if (
                        str(row[0]) not in _TERMINAL_STATUSES
                        or bool(row[2])
                        or bool(row[3])
                        or bool(row[4])
                        or bool(row[5])
                        or (
                            row[6] is not None
                            and (
                                row[7] is None
                                or str(row[7]) != "settled"
                            )
                        )
                    ):
                        raise ServerError(
                            ServerErrorCode.RUN_DELETE_CONFLICT
                        )
                    inserted = connection.execute(
                        """
                        INSERT INTO run_operations
                            (run_id, operation_id, operation_kind)
                        VALUES (%s, %s, 'delete')
                        ON CONFLICT (run_id) DO NOTHING
                        RETURNING operation_id
                        """,
                        (run_id, f"delete-{uuid.uuid4().hex}"),
                    ).fetchone()
                    if inserted is None:
                        raise ServerError(ServerErrorCode.RUN_BUSY)
                    deleted_at = connection.execute(
                        """
                        INSERT INTO run_deletions (run_id)
                        VALUES (%s)
                        RETURNING deleted_at
                        """,
                        (run_id,),
                    ).fetchone()[0]
                    return self._result(run_id, deleted_at)
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def get(self, run_id: str) -> dict[str, object] | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT deleted_at
                    FROM run_deletions
                    WHERE run_id = %s
                    """,
                    (run_id,),
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        return None if row is None else self._result(run_id, row[0])

    @staticmethod
    def _result(run_id: str, deleted_at: datetime) -> dict[str, object]:
        return {
            "run_id": run_id,
            "deleted_at": deleted_at.isoformat(),
        }
