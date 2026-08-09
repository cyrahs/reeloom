from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from reeloom.server.errors import ServerError, ServerErrorCode


@dataclass(frozen=True, slots=True)
class PostgresSubtitleLineageGate:
    """One-way guard against acquire -> successor -> acquire loops."""

    pool: ConnectionPool

    def lineage_allows_automatic_acquisition(self, run_id: str) -> bool:
        try:
            with self.pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT subtitle_acquisition_lineage_key
                    FROM runs WHERE run_id = %s
                    """,
                    (run_id,),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        if row is None:
            raise ServerError(ServerErrorCode.RUN_NOT_FOUND)
        return row[0] is None
