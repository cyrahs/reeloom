from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Protocol

from agents.items import TResponseInputItem
from agents.memory import SessionSettings
from psycopg_pool import ConnectionPool

from reeloom.adapters.agent_session import (
    AgentSessionError,
    AgentSessionErrorCode,
)

_MAX_ITEMS = 10_000
_MAX_BATCH_BYTES = 4 * 1024 * 1024


def _copy(items: list[TResponseInputItem]) -> list[TResponseInputItem]:
    try:
        encoded = json.dumps(
            items,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        copied = json.loads(encoded)
    except (TypeError, ValueError):
        raise AgentSessionError(AgentSessionErrorCode.FAILURE) from None
    if (
        len(encoded.encode("ascii")) > _MAX_BATCH_BYTES
        or not isinstance(copied, list)
        or any(not isinstance(item, dict) for item in copied)
    ):
        raise AgentSessionError(
            AgentSessionErrorCode.LIMIT_EXCEEDED
        )
    return copied


@dataclass(frozen=True, slots=True)
class SessionProjection:
    revision: int
    items: tuple[TResponseInputItem, ...]


class SessionRepository(Protocol):
    def load(
        self,
        *,
        run_id: str,
        session_id: str,
    ) -> SessionProjection: ...

    def compare_and_append(
        self,
        *,
        run_id: str,
        session_id: str,
        expected_revision: int,
        operation: str,
        batch_items: list[TResponseInputItem],
        projected_items: list[TResponseInputItem],
    ) -> SessionProjection: ...


class InMemorySessionRepository:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_id: str | None = None
        self._session_id: str | None = None
        self._revision = 0
        self._items: list[TResponseInputItem] = []
        self.batch_count = 0

    @property
    def revision(self) -> int:
        return self._revision

    def load(self, *, run_id: str, session_id: str) -> SessionProjection:
        with self._lock:
            if self._run_id not in {None, run_id} or self._session_id not in {
                None,
                session_id,
            }:
                raise AgentSessionError(AgentSessionErrorCode.CONFLICT)
            self._run_id = run_id
            self._session_id = session_id
            return SessionProjection(
                self._revision,
                tuple(_copy(self._items)),
            )

    def compare_and_append(
        self,
        *,
        run_id: str,
        session_id: str,
        expected_revision: int,
        operation: str,
        batch_items: list[TResponseInputItem],
        projected_items: list[TResponseInputItem],
    ) -> SessionProjection:
        del batch_items, operation
        with self._lock:
            if (
                self._run_id != run_id
                or self._session_id != session_id
                or self._revision != expected_revision
            ):
                raise AgentSessionError(AgentSessionErrorCode.CONFLICT)
            self._revision += 1
            self._items = _copy(projected_items)
            self.batch_count += 1
            return SessionProjection(
                self._revision,
                tuple(_copy(self._items)),
            )


class PostgresSessionRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def load(self, *, run_id: str, session_id: str) -> SessionProjection:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT revision, items
                        FROM agent_sessions
                        WHERE session_id = %s
                        """,
                        (session_id,),
                    ).fetchone()
                    if row is None:
                        connection.execute(
                            """
                            INSERT INTO agent_sessions
                                (session_id, run_id, revision, items)
                            VALUES (%s, %s, 0, '[]'::jsonb)
                            """,
                            (session_id, run_id),
                        )
                        return SessionProjection(0, ())
                    owner = connection.execute(
                        """
                        SELECT run_id FROM agent_sessions
                        WHERE session_id = %s
                        """,
                        (session_id,),
                    ).fetchone()
                    if str(owner[0]) != run_id:
                        raise AgentSessionError(
                            AgentSessionErrorCode.CONFLICT
                        )
                    items = _copy(list(row[1]))
                    return SessionProjection(int(row[0]), tuple(items))
        except AgentSessionError:
            raise
        except Exception:
            raise AgentSessionError(
                AgentSessionErrorCode.FAILURE
            ) from None

    def compare_and_append(
        self,
        *,
        run_id: str,
        session_id: str,
        expected_revision: int,
        operation: str,
        batch_items: list[TResponseInputItem],
        projected_items: list[TResponseInputItem],
    ) -> SessionProjection:
        batch_json = json.dumps(
            _copy(batch_items),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        projected_json = json.dumps(
            _copy(projected_items),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT run_id, revision
                        FROM agent_sessions
                        WHERE session_id = %s
                        FOR UPDATE
                        """,
                        (session_id,),
                    ).fetchone()
                    if (
                        row is None
                        or str(row[0]) != run_id
                        or int(row[1]) != expected_revision
                    ):
                        raise AgentSessionError(
                            AgentSessionErrorCode.CONFLICT
                        )
                    revision = expected_revision + 1
                    connection.execute(
                        """
                        INSERT INTO agent_session_batches
                            (session_id, revision, operation, items)
                        VALUES (%s, %s, %s, %s::jsonb)
                        """,
                        (
                            session_id,
                            revision,
                            operation,
                            batch_json,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE agent_sessions
                        SET revision = %s, items = %s::jsonb
                        WHERE session_id = %s
                        """,
                        (revision, projected_json, session_id),
                    )
                    return SessionProjection(
                        revision,
                        tuple(_copy(projected_items)),
                    )
        except AgentSessionError:
            raise
        except Exception:
            raise AgentSessionError(
                AgentSessionErrorCode.FAILURE
            ) from None


class RepositoryAgentSession:
    session_settings: SessionSettings | None = None

    def __init__(
        self,
        *,
        repository: SessionRepository,
        run_id: str,
        session_id: str,
    ) -> None:
        self._repository = repository
        self._run_id = run_id
        self.session_id = session_id
        projection = repository.load(
            run_id=run_id,
            session_id=session_id,
        )
        self._revision = projection.revision
        self._items = list(projection.items)
        self._lock = threading.Lock()

    async def get_items(
        self,
        limit: int | None = None,
    ) -> list[TResponseInputItem]:
        if limit is not None and (type(limit) is not int or limit < 0):
            raise AgentSessionError(AgentSessionErrorCode.FAILURE)
        selected = (
            self._items
            if limit is None
            else self._items[-limit:] if limit else []
        )
        return _copy(list(selected))

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        copied = _copy(items)
        if copied:
            self._append("add", copied)

    async def pop_item(self) -> TResponseInputItem | None:
        if not self._items:
            return None
        popped = _copy([self._items[-1]])[0]
        self._append("pop", [])
        return popped

    async def clear_session(self) -> None:
        if self._items:
            self._append("clear", [])

    def _append(
        self,
        operation: str,
        batch: list[TResponseInputItem],
    ) -> None:
        with self._lock:
            projected = _copy(self._items)
            if operation == "add":
                projected.extend(batch)
            elif operation == "pop":
                projected.pop()
            elif operation == "clear":
                projected.clear()
            if len(projected) > _MAX_ITEMS:
                raise AgentSessionError(
                    AgentSessionErrorCode.LIMIT_EXCEEDED
                )
            result = self._repository.compare_and_append(
                run_id=self._run_id,
                session_id=self.session_id,
                expected_revision=self._revision,
                operation=operation,
                batch_items=batch,
                projected_items=projected,
            )
            self._revision = result.revision
            self._items = list(result.items)


class BufferedAgentSession:
    """SDK session that buffers one interaction without database writes."""

    session_settings: SessionSettings | None = None

    def __init__(
        self,
        *,
        repository: SessionRepository,
        run_id: str,
        session_id: str,
        expected_revision: int,
    ) -> None:
        projection = repository.load(
            run_id=run_id,
            session_id=session_id,
        )
        if projection.revision != expected_revision:
            raise AgentSessionError(AgentSessionErrorCode.CONFLICT)
        self.session_id = session_id
        self.revision = projection.revision
        self._items = list(projection.items)
        self._batch: list[TResponseInputItem] = []

    @property
    def projected_items(self) -> list[TResponseInputItem]:
        return _copy(self._items)

    @property
    def batch_items(self) -> list[TResponseInputItem]:
        return _copy(self._batch)

    async def get_items(
        self,
        limit: int | None = None,
    ) -> list[TResponseInputItem]:
        if limit is not None and (type(limit) is not int or limit < 0):
            raise AgentSessionError(AgentSessionErrorCode.FAILURE)
        selected = (
            self._items
            if limit is None
            else self._items[-limit:] if limit else []
        )
        return _copy(list(selected))

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        copied = _copy(items)
        if not copied:
            return
        if len(self._items) + len(copied) > _MAX_ITEMS:
            raise AgentSessionError(AgentSessionErrorCode.LIMIT_EXCEEDED)
        self._items.extend(copied)
        self._batch.extend(copied)

    async def pop_item(self) -> TResponseInputItem | None:
        raise AgentSessionError(AgentSessionErrorCode.FAILURE)

    async def clear_session(self) -> None:
        raise AgentSessionError(AgentSessionErrorCode.FAILURE)
