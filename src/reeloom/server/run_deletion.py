from __future__ import annotations

import uuid
from datetime import datetime

from psycopg_pool import ConnectionPool

from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.run_deletion_policy import RUN_DELETION_READY_SQL


class PostgresRunDeletionService:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def delete(self, run_id: str) -> dict[str, object]:
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
                        f"""
                        SELECT deletion.deleted_at,
                               ({RUN_DELETION_READY_SQL}) AS deletion_ready
                        FROM runs AS r
                        JOIN discoveries AS d
                          ON d.discovery_id = r.discovery_id
                        LEFT JOIN run_deletions AS deletion
                          ON deletion.run_id = r.run_id
                        WHERE r.run_id = %s
                        FOR UPDATE OF r
                        """,
                        (run_id,),
                    ).fetchone()
                    if row is None:
                        raise ServerError(ServerErrorCode.RUN_NOT_FOUND)
                    if row[0] is not None:
                        return self._result(run_id, row[0])
                    if not bool(row[1]):
                        raise ServerError(
                            ServerErrorCode.RUN_DELETE_CONFLICT
                        )
                    connection.execute(
                        """
                        DELETE FROM watch_folder_observations AS observed
                        USING discoveries AS discovery
                        WHERE discovery.discovery_id =
                              observed.discovery_id
                          AND discovery.discovery_id = (
                              SELECT r.discovery_id
                              FROM runs AS r
                              WHERE r.run_id = %s
                          )
                          AND observed.status = 'blocked'
                          AND observed.blocked_reason =
                              'source_folder_missing'
                        """,
                        (run_id,),
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
