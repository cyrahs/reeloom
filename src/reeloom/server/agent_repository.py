from __future__ import annotations

from psycopg_pool import ConnectionPool

from reeloom.server.agent_definition import AgentDefinitionRevision
from reeloom.server.errors import ServerError, ServerErrorCode


class PostgresAgentDefinitionRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def register_and_bind(
        self,
        *,
        run_id: str,
        definition: AgentDefinitionRevision,
        session_id: str,
    ) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO agent_definitions
                            (definition_hash, payload)
                        VALUES (%s, %s::jsonb)
                        ON CONFLICT (definition_hash) DO NOTHING
                        """,
                        (
                            definition.definition_hash,
                            definition.to_json(),
                        ),
                    )
                    stored = connection.execute(
                        """
                        SELECT payload FROM agent_definitions
                        WHERE definition_hash = %s
                        """,
                        (definition.definition_hash,),
                    ).fetchone()
                    if (
                        stored is None
                        or AgentDefinitionRevision.from_value(stored[0])
                        != definition
                    ):
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    row = connection.execute(
                        """
                        SELECT agent_definition_hash, session_id
                        FROM runs
                        WHERE run_id = %s
                        FOR UPDATE
                        """,
                        (run_id,),
                    ).fetchone()
                    if row is None:
                        raise ServerError(
                            ServerErrorCode.DISCOVERY_NOT_FOUND
                        )
                    if row[0] is not None and (
                        str(row[0]) != definition.definition_hash
                        or str(row[1]) != session_id
                    ):
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    connection.execute(
                        """
                        UPDATE runs
                        SET agent_definition_hash = %s, session_id = %s
                        WHERE run_id = %s
                        """,
                        (
                            definition.definition_hash,
                            session_id,
                            run_id,
                        ),
                    )
        except ServerError:
            raise
        except ValueError:
            raise ServerError(
                ServerErrorCode.INTERACTION_CONFLICT
            ) from None
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def load_bound(
        self,
        *,
        run_id: str,
    ) -> tuple[AgentDefinitionRevision, str]:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT r.agent_definition_hash, r.session_id,
                           d.payload
                    FROM runs AS r
                    LEFT JOIN agent_definitions AS d
                      ON d.definition_hash = r.agent_definition_hash
                    WHERE r.run_id = %s
                    """,
                    (run_id,),
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        if (
            row is None
            or row[0] is None
            or row[1] is None
            or row[2] is None
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        try:
            definition = AgentDefinitionRevision.from_value(row[2])
        except ValueError:
            raise ServerError(
                ServerErrorCode.INTERACTION_CONFLICT
            ) from None
        if definition.definition_hash != str(row[0]):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        return definition, str(row[1])
