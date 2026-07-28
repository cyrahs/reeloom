from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from reeloom.server.database import PostgresControlPlane
from reeloom.server.idempotency import PostgresIdempotencyService


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


@pytest.mark.postgres
def test_immutable_history_rejects_update_and_delete() -> None:
    control = PostgresControlPlane(_dsn())
    try:
        control.open()
        control.migrate()
        with control.pool.connection() as connection:
            for statement in (
                "UPDATE schema_migrations SET name = name WHERE version = 1",
                "DELETE FROM schema_migrations WHERE version = 1",
            ):
                with pytest.raises(psycopg.Error):
                    with connection.transaction():
                        connection.execute(statement)
    finally:
        control.close()


@pytest.mark.postgres
def test_terminal_api_mutation_is_immutable() -> None:
    control = PostgresControlPlane(_dsn())
    scope = f"history-{uuid.uuid4().hex}"
    try:
        control.open()
        control.migrate()
        PostgresIdempotencyService(control.pool).run(
            scope=scope,
            subject_id="subject",
            idempotency_key="terminal",
            request={"value": 1},
            execute=lambda: {"status": "done"},
        )
        with control.pool.connection() as connection:
            with pytest.raises(psycopg.Error):
                with connection.transaction():
                    connection.execute(
                        """
                        UPDATE api_mutations
                        SET result = result
                        WHERE scope = %s
                        """,
                        (scope,),
                    )
    finally:
        control.close()
