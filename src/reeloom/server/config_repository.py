from __future__ import annotations

from psycopg_pool import ConnectionPool

from reeloom.server.config import ConfigRevision
from reeloom.server.errors import ServerError, ServerErrorCode

CONFIG_LOCK_ID = 7_303_418_811


class PostgresConfigRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def compare_and_append(
        self,
        *,
        expected_revision: int,
        revision: ConfigRevision,
    ) -> ConfigRevision:
        if revision.revision != expected_revision + 1:
            raise ServerError(ServerErrorCode.INVALID_CONFIG)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (CONFIG_LOCK_ID,),
                    )
                    row = connection.execute(
                        """
                        SELECT revision
                        FROM config_heads
                        WHERE singleton = true
                        FOR UPDATE
                        """
                    ).fetchone()
                    current = 0 if row is None else int(row[0])
                    if current != expected_revision:
                        raise ServerError(
                            ServerErrorCode.CONFIG_CONFLICT
                        )
                    connection.execute(
                        """
                        INSERT INTO config_revisions
                            (revision_id, revision, payload, created_at)
                        VALUES (%s, %s, %s::jsonb, %s)
                        """,
                        (
                            revision.revision_id,
                            revision.revision,
                            revision.to_json(),
                            revision.created_at,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO config_heads (singleton, revision)
                        VALUES (true, %s)
                        ON CONFLICT (singleton)
                        DO UPDATE SET revision = EXCLUDED.revision
                        """,
                        (revision.revision,),
                    )
            return revision
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def get(self, revision: int) -> ConfigRevision:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT payload::text
                    FROM config_revisions
                    WHERE revision = %s
                    """,
                    (revision,),
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        if row is None:
            raise ServerError(ServerErrorCode.CONFIG_NOT_FOUND)
        return ConfigRevision.from_json(str(row[0]))

    def head(self) -> ConfigRevision | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT revision
                    FROM config_heads
                    WHERE singleton = true
                    """
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        return None if row is None else self.get(int(row[0]))
