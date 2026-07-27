from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.migrations import (
    EXPECTED_SCHEMA_VERSION,
    MIGRATIONS,
    validate_migration_history,
)

_MIGRATION_LOCK_ID = 7_303_418_801
_INSTANCE_LOCK_ID = 7_303_418_802
_SUPPORTED_POSTGRES_MAJORS = frozenset({16, 17, 18})


def _validated_postgres_major(version_number: int) -> int:
    major = version_number // 10_000
    if major not in _SUPPORTED_POSTGRES_MAJORS:
        raise ServerError(ServerErrorCode.DATABASE_VERSION_MISMATCH)
    return major


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    postgres_major: int
    schema_version: int


class PostgresControlPlane:
    """Own pool, schema lifecycle, and one lifetime advisory lock."""

    def __init__(
        self,
        dsn: str,
        *,
        migration_dsn: str | None = None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn:
            raise ServerError(ServerErrorCode.INVALID_SETTINGS)
        self._dsn = dsn
        self._migration_dsn = migration_dsn
        self._pool: ConnectionPool | None = None
        self._lock_connection: psycopg.Connection | None = None

    def open(self) -> None:
        if self._pool is not None:
            return
        pool = ConnectionPool(
            conninfo=self._dsn,
            min_size=1,
            max_size=4,
            open=False,
            timeout=5,
            kwargs={"autocommit": False},
        )
        try:
            pool.open(wait=True, timeout=5)
        except Exception:
            pool.close()
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        self._pool = pool

    def migrate(self) -> None:
        try:
            if self._migration_dsn is None:
                with self._require_pool().connection() as connection:
                    self._migrate_connection(connection)
            else:
                with psycopg.connect(
                    self._migration_dsn,
                    autocommit=False,
                    connect_timeout=5,
                ) as connection:
                    self._migrate_connection(connection)
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    @staticmethod
    def _migrate_connection(
        connection: psycopg.Connection,
    ) -> None:
        with connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_MIGRATION_LOCK_ID,),
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version integer PRIMARY KEY CHECK (version > 0),
                    name text NOT NULL,
                    checksum character(64) NOT NULL,
                    applied_at timestamptz NOT NULL
                        DEFAULT clock_timestamp()
                )
                """
            )
            rows = connection.execute(
                """
                SELECT version, checksum
                FROM schema_migrations
                ORDER BY version
                """
            ).fetchall()
            applied = validate_migration_history(
                migrations=MIGRATIONS,
                applied=tuple(
                    (int(row[0]), str(row[1])) for row in rows
                ),
            )
            for migration in MIGRATIONS[applied:]:
                connection.execute(sql.SQL(migration.sql))
                connection.execute(
                    """
                    INSERT INTO schema_migrations
                        (version, name, checksum)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                    ),
                )

    def health(self) -> DatabaseHealth:
        pool = self._require_pool()
        try:
            with pool.connection() as connection:
                version_number = int(
                    connection.execute(
                        "SHOW server_version_num"
                    ).fetchone()[0]
                )
                row = connection.execute(
                    "SELECT COALESCE(max(version), 0) FROM schema_migrations"
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        postgres_major = _validated_postgres_major(version_number)
        schema_version = int(row[0])
        if schema_version != EXPECTED_SCHEMA_VERSION:
            raise ServerError(ServerErrorCode.SCHEMA_MISMATCH)
        return DatabaseHealth(
            postgres_major=postgres_major,
            schema_version=schema_version,
        )

    def acquire_instance_lock(self) -> None:
        if self._lock_connection is not None:
            return
        self._require_pool()
        try:
            connection = psycopg.connect(
                self._dsn,
                autocommit=True,
                connect_timeout=5,
            )
            acquired = bool(
                connection.execute(
                    "SELECT pg_try_advisory_lock(%s)",
                    (_INSTANCE_LOCK_ID,),
                ).fetchone()[0]
            )
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        if not acquired:
            connection.close()
            raise ServerError(
                ServerErrorCode.INSTANCE_ALREADY_RUNNING
            )
        self._lock_connection = connection

    def register_boot(self, boot_id: str) -> str:
        if (
            not isinstance(boot_id, str)
            or not boot_id
            or len(boot_id.encode("utf-8")) > 128
        ):
            raise ServerError(ServerErrorCode.INVALID_SETTINGS)
        pool = self._require_pool()
        try:
            with pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO service_boots
                            (boot_id, process_id)
                        VALUES (%s, %s)
                        """,
                        (boot_id, os.getpid()),
                    )
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        return boot_id

    def stop_boot(self, boot_id: str) -> None:
        pool = self._require_pool()
        try:
            with pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE service_boots
                        SET stopped_at = clock_timestamp()
                        WHERE boot_id = %s AND stopped_at IS NULL
                        RETURNING boot_id
                        """,
                        (boot_id,),
                    ).fetchone()
                    if row is None:
                        raise RuntimeError
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def close(self) -> None:
        try:
            if self._lock_connection is not None:
                try:
                    self._lock_connection.execute(
                        "SELECT pg_advisory_unlock(%s)",
                        (_INSTANCE_LOCK_ID,),
                    )
                finally:
                    try:
                        self._lock_connection.close()
                    finally:
                        self._lock_connection = None
        finally:
            if self._pool is not None:
                try:
                    self._pool.close()
                finally:
                    self._pool = None

    @property
    def pool(self) -> ConnectionPool:
        return self._require_pool()

    def _require_pool(self) -> ConnectionPool:
        if self._pool is None:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE)
        return self._pool
