from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from reeloom.server.database import (
    PostgresControlPlane,
    _validated_postgres_major,
)
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.migrations import EXPECTED_SCHEMA_VERSION


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


def test_database_close_releases_pool_when_unlock_fails() -> None:
    calls: list[str] = []

    class LockConnection:
        def execute(self, *_: object) -> None:
            calls.append("unlock")
            raise RuntimeError("unlock failed")

        def close(self) -> None:
            calls.append("lock")

    class Pool:
        def close(self) -> None:
            calls.append("pool")

    control = PostgresControlPlane("postgresql://reeloom@db/reeloom")
    control._lock_connection = LockConnection()  # type: ignore[assignment]
    control._pool = Pool()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="unlock failed"):
        control.close()

    assert calls == ["unlock", "lock", "pool"]
    assert control._lock_connection is None
    assert control._pool is None


@pytest.mark.parametrize("major", (16, 17, 18))
def test_health_accepts_supported_postgres_major(major: int) -> None:
    assert _validated_postgres_major(major * 10_000) == major


@pytest.mark.parametrize("major", (15, 19))
def test_health_rejects_unsupported_postgres_major(major: int) -> None:
    with pytest.raises(ServerError) as raised:
        _validated_postgres_major(major * 10_000)

    assert raised.value.code is ServerErrorCode.DATABASE_VERSION_MISMATCH


@pytest.mark.postgres
def test_empty_database_migration_is_idempotent_and_healthy() -> None:
    control = PostgresControlPlane(_dsn())
    try:
        control.open()
        control.migrate()
        control.migrate()
        health = control.health()
        assert health.schema_version == EXPECTED_SCHEMA_VERSION
        assert health.postgres_major in {16, 17, 18}
        with control.pool.connection() as connection:
            immutable = connection.execute(
                """
                SELECT count(*)
                FROM pg_trigger
                WHERE tgname = 'plan_reviews_immutable'
                  AND NOT tgisinternal
                """
            ).fetchone()
        assert immutable is not None and int(immutable[0]) == 1
    finally:
        control.close()


@pytest.mark.postgres
def test_advisory_lock_and_boot_registration() -> None:
    first = PostgresControlPlane(_dsn())
    second = PostgresControlPlane(_dsn())
    try:
        first.open()
        first.migrate()
        first.acquire_instance_lock()
        boot_id = first.register_boot(uuid.uuid4().hex)
        assert boot_id

        second.open()
        with pytest.raises(ServerError) as raised:
            second.acquire_instance_lock()
        assert raised.value.code is ServerErrorCode.INSTANCE_ALREADY_RUNNING
    finally:
        second.close()
        first.close()


@pytest.mark.postgres
def test_concurrent_migrations_serialize_and_validate() -> None:
    first = PostgresControlPlane(_dsn())
    second = PostgresControlPlane(_dsn())
    try:
        first.open()
        second.open()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(
                executor.map(
                    lambda control: (
                        control.migrate(),
                        control.health().schema_version,
                    )[1],
                    (first, second),
                )
            )
        assert results == (
            EXPECTED_SCHEMA_VERSION,
            EXPECTED_SCHEMA_VERSION,
        )
    finally:
        second.close()
        first.close()
