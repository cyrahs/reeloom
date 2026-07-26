from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable

from psycopg_pool import ConnectionPool

from reeloom.server.errors import ServerError, ServerErrorCode

_MAX_RESULT_BYTES = 128 * 1024


class PostgresIdempotencyService:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def run(
        self,
        *,
        scope: str,
        subject_id: str,
        idempotency_key: str,
        request: dict[str, object],
        execute: Callable[[], dict[str, object]],
        resolve: Callable[[], dict[str, object] | None] | None = None,
    ) -> dict[str, object]:
        request_hash = self._request_hash(request)
        terminal, failed = self._reserve(
            scope=scope,
            subject_id=subject_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if terminal is not None:
            return terminal
        if failed:
            resolved = None if resolve is None else resolve()
            if resolved is None:
                raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
            return self._result(
                json.loads(self._encode_result(resolved))
            )
        mutation_id = self._mutation_id(
            scope,
            subject_id,
            idempotency_key,
        )
        try:
            result = execute()
            return self._finalize(
                mutation_id=mutation_id,
                result=result,
            )
        except Exception as error:
            try:
                self._fail(mutation_id=mutation_id)
            except Exception as cleanup_error:
                error.add_note(
                    "failed to persist idempotency failure: "
                    f"{type(cleanup_error).__name__}"
                )
            raise

    def reconcile_active(self) -> int:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    rows = connection.execute(
                        """
                        UPDATE api_mutations
                        SET status = 'failed',
                            finished_at = clock_timestamp()
                        WHERE status = 'active'
                        RETURNING mutation_id
                        """
                    ).fetchall()
                    return len(rows)
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def _reserve(
        self,
        *,
        scope: str,
        subject_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[dict[str, object] | None, bool]:
        if (
            not isinstance(scope, str)
            or not scope
            or len(scope.encode("utf-8")) > 64
            or not isinstance(subject_id, str)
            or not subject_id
            or len(subject_id.encode("utf-8")) > 128
            or not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key.encode("utf-8")) > 256
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        mutation_id = self._mutation_id(
            scope,
            subject_id,
            idempotency_key,
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
                        (mutation_id,),
                    )
                    row = connection.execute(
                        """
                        SELECT request_hash, status, result
                        FROM api_mutations
                        WHERE scope = %s AND subject_id = %s
                          AND idempotency_key = %s
                        FOR UPDATE
                        """,
                        (scope, subject_id, idempotency_key),
                    ).fetchone()
                    if row is not None:
                        if str(row[0]) != request_hash:
                            raise ServerError(
                                ServerErrorCode.INTERACTION_CONFLICT
                            )
                        if str(row[1]) == "active":
                            raise ServerError(ServerErrorCode.RUN_BUSY)
                        if str(row[1]) == "failed":
                            return None, True
                        if str(row[1]) == "completed":
                            return self._result(row[2]), False
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    connection.execute(
                        """
                        INSERT INTO api_mutations
                            (mutation_id, scope, subject_id,
                             idempotency_key, request_hash, status)
                        VALUES (%s, %s, %s, %s, %s, 'active')
                        """,
                        (
                            mutation_id,
                            scope,
                            subject_id,
                            idempotency_key,
                            request_hash,
                        ),
                    )
                    return None, False
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def _finalize(
        self,
        *,
        mutation_id: str,
        result: dict[str, object],
    ) -> dict[str, object]:
        encoded = self._encode_result(result)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE api_mutations
                        SET status = 'completed', result = %s::jsonb,
                            finished_at = clock_timestamp()
                        WHERE mutation_id = %s AND status = 'active'
                        RETURNING result
                        """,
                        (encoded, mutation_id),
                    ).fetchone()
                    if row is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    return self._result(row[0])
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def _fail(self, *, mutation_id: str) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        UPDATE api_mutations
                        SET status = 'failed',
                            finished_at = clock_timestamp()
                        WHERE mutation_id = %s AND status = 'active'
                        """,
                        (mutation_id,),
                    )
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    @staticmethod
    def _mutation_id(
        scope: str,
        subject_id: str,
        idempotency_key: str,
    ) -> str:
        return "mutation-" + uuid.uuid5(
            uuid.NAMESPACE_URL,
            "\x00".join((scope, subject_id, idempotency_key)),
        ).hex

    @staticmethod
    def _request_hash(request: dict[str, object]) -> str:
        try:
            encoded = json.dumps(
                request,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        except (TypeError, UnicodeError, ValueError):
            raise ServerError(
                ServerErrorCode.INTERACTION_CONFLICT
            ) from None
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _encode_result(result: dict[str, object]) -> str:
        try:
            encoded = json.dumps(
                result,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, UnicodeError, ValueError):
            raise ServerError(
                ServerErrorCode.INTERACTION_CONFLICT
            ) from None
        if len(encoded.encode("ascii")) > _MAX_RESULT_BYTES:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        return encoded

    @staticmethod
    def _result(value: object) -> dict[str, object]:
        raw = value if isinstance(value, dict) else json.loads(str(value))
        if not isinstance(raw, dict) or not all(
            isinstance(key, str) for key in raw
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        return raw
