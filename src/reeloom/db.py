"""PostgreSQL access.

The control plane is plain state tables: a run's current state is one column,
and history is append-only logging that nothing reads back to rebuild state.
Crash recovery comes from replaying the plan against the filesystem, not from
replaying rows.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from reeloom.models import (
    ExecutedMove,
    MediaType,
    Plan,
    Run,
    RunResult,
    RunState,
    SnapshotFile,
    SubtitleVariant,
    WatchConfig,
)

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA_SQL = """
create table if not exists schema_version (
    version integer primary key
);

create table if not exists settings (
    id boolean primary key default true check (id),
    tmdb_api_key text not null default '',
    llm_base_url text not null default '',
    llm_api_key text not null default '',
    llm_model text not null default '',
    llm_reasoning_effort text not null default '',
    telegram_bot_token text not null default '',
    telegram_chat_id text not null default '',
    telegram_pin_alerts boolean not null default true,
    trash_retention_days integer not null default 3,
    updated_at timestamptz not null default now()
);

create table if not exists watch_config (
    id uuid primary key,
    name text not null,
    inbound_root text not null,
    library_root text not null,
    media_type text not null,
    enabled boolean not null default true,
    stability_seconds integer not null default 120,
    acquire_subtitles boolean not null default false,
    subtitle_variant text not null default 'chs',
    notify boolean not null default true,
    replace_enabled boolean not null default false,
    replace_extra_dirs jsonb not null default '[]'::jsonb,
    replace_auto_ratio double precision not null default 1.2,
    created_at timestamptz not null default now()
);

create table if not exists run (
    id uuid primary key,
    config_id uuid not null references watch_config(id) on delete cascade,
    folder_name text not null,
    state text not null,
    snapshot jsonb not null default '[]'::jsonb,
    plan jsonb,
    executed_moves jsonb not null default '[]'::jsonb,
    result jsonb,
    error jsonb,
    attempts integer not null default 0,
    extra jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create unique index if not exists run_open_folder_key
    on run (config_id, folder_name)
    where state not in ('done', 'discarded', 'failed');

create index if not exists run_state_key on run (state, updated_at);

create table if not exists run_log (
    id bigserial primary key,
    run_id uuid not null references run(id) on delete cascade,
    ts timestamptz not null default now(),
    level text not null,
    message text not null,
    data jsonb
);

create index if not exists run_log_run_key on run_log (run_id, id);

create table if not exists interaction (
    id bigserial primary key,
    run_id uuid not null references run(id) on delete cascade,
    role text not null,
    content text not null,
    ts timestamptz not null default now()
);

create index if not exists interaction_run_key on interaction (run_id, id);

alter table watch_config
    add column if not exists subtitle_variant text not null default 'chs';

alter table settings
    add column if not exists llm_reasoning_effort text not null default '';

alter table watch_config
    add column if not exists replace_enabled boolean not null default false;

alter table watch_config
    add column if not exists replace_extra_dirs jsonb not null default '[]'::jsonb;

alter table watch_config
    add column if not exists replace_auto_ratio double precision not null default 1.2;

alter table settings
    add column if not exists trash_retention_days integer not null default 3;

alter table settings
    add column if not exists telegram_pin_alerts boolean not null default true;
"""


class Database:
    """Thin async repository over a connection pool."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @classmethod
    @asynccontextmanager
    async def connect(cls, database_url: str) -> AsyncIterator[Database]:
        pool = AsyncConnectionPool(
            database_url,
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        await pool.open(wait=True, timeout=30)
        try:
            database = cls(pool)
            await database.migrate()
            yield database
        finally:
            await pool.close()

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self._pool.connection() as connection:
            yield connection

    async def migrate(self) -> None:
        async with self._connection() as connection:
            await connection.execute(SCHEMA_SQL)
            await connection.execute(
                "insert into schema_version (version) values (%s)"
                " on conflict do nothing",
                (SCHEMA_VERSION,),
            )
            await connection.execute(
                "insert into settings (id) values (true) on conflict do nothing"
            )
        _LOGGER.info("schema ready version=%s", SCHEMA_VERSION)

    # ---- settings -----------------------------------------------------

    async def get_settings(self) -> dict[str, Any]:
        async with self._connection() as connection:
            cursor = await connection.execute("select * from settings limit 1")
            row = await cursor.fetchone()
        return dict(row or {})

    async def update_settings(self, values: dict[str, Any]) -> None:
        allowed = {
            "tmdb_api_key",
            "llm_base_url",
            "llm_api_key",
            "llm_model",
            "llm_reasoning_effort",
            "telegram_bot_token",
            "telegram_chat_id",
            "telegram_pin_alerts",
            "trash_retention_days",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = %({key})s" for key in updates)
        async with self._connection() as connection:
            await connection.execute(
                f"update settings set {assignments}, updated_at = now()",
                updates,
            )

    # ---- watch configs ------------------------------------------------

    async def list_configs(self, *, enabled_only: bool = False) -> list[WatchConfig]:
        query = "select * from watch_config"
        if enabled_only:
            query += " where enabled"
        query += " order by created_at"
        async with self._connection() as connection:
            cursor = await connection.execute(query)
            rows = await cursor.fetchall()
        return [_watch_config(row) for row in rows]

    async def get_config(self, config_id: str) -> WatchConfig | None:
        async with self._connection() as connection:
            cursor = await connection.execute(
                "select * from watch_config where id = %s", (config_id,)
            )
            row = await cursor.fetchone()
        return _watch_config(row) if row else None

    async def create_config(self, config: WatchConfig) -> WatchConfig:
        async with self._connection() as connection:
            await connection.execute(
                """
                insert into watch_config (
                    id, name, inbound_root, library_root, media_type,
                    enabled, stability_seconds, acquire_subtitles,
                    subtitle_variant, notify, replace_enabled,
                    replace_extra_dirs, replace_auto_ratio
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
                )
                """,
                (
                    config.id,
                    config.name,
                    config.inbound_root,
                    config.library_root,
                    config.media_type.value,
                    config.enabled,
                    config.stability_seconds,
                    config.acquire_subtitles,
                    config.subtitle_variant.value,
                    config.notify,
                    config.replace_enabled,
                    json.dumps(list(config.replace_extra_dirs)),
                    config.replace_auto_ratio,
                ),
            )
        return config

    async def update_config(self, config_id: str, values: dict[str, Any]) -> None:
        allowed = {
            "name",
            "inbound_root",
            "library_root",
            "media_type",
            "enabled",
            "stability_seconds",
            "acquire_subtitles",
            "subtitle_variant",
            "notify",
            "replace_enabled",
            "replace_extra_dirs",
            "replace_auto_ratio",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return
        if "replace_extra_dirs" in updates:
            updates["replace_extra_dirs"] = json.dumps(
                list(updates["replace_extra_dirs"])
            )
        assignments = ", ".join(
            f"{key} = %({key})s::jsonb" if key == "replace_extra_dirs"
            else f"{key} = %({key})s"
            for key in updates
        )
        async with self._connection() as connection:
            await connection.execute(
                f"update watch_config set {assignments} where id = %(id)s",
                {**updates, "id": config_id},
            )

    async def delete_config(self, config_id: str) -> None:
        async with self._connection() as connection:
            await connection.execute(
                "delete from watch_config where id = %s", (config_id,)
            )

    # ---- runs ---------------------------------------------------------

    async def create_run(
        self,
        *,
        config_id: str,
        folder_name: str,
        snapshot: Sequence[SnapshotFile],
    ) -> Run | None:
        """Open a run for a folder, or return None if one is already open."""

        run_id = str(uuid.uuid4())
        payload = json.dumps([item.to_json() for item in snapshot])
        async with self._connection() as connection:
            cursor = await connection.execute(
                """
                insert into run (id, config_id, folder_name, state, snapshot)
                values (%s, %s, %s, 'pending', %s::jsonb)
                on conflict do nothing
                returning *
                """,
                (run_id, config_id, folder_name, payload),
            )
            row = await cursor.fetchone()
        return _run(row) if row else None

    async def get_run(self, run_id: str) -> Run | None:
        async with self._connection() as connection:
            cursor = await connection.execute(
                "select * from run where id = %s", (run_id,)
            )
            row = await cursor.fetchone()
        return _run(row) if row else None

    async def list_runs(
        self, *, states: Sequence[RunState] | None = None, limit: int = 100
    ) -> list[Run]:
        query = "select * from run"
        params: list[Any] = []
        if states:
            query += " where state = any(%s)"
            params.append([state.value for state in states])
        query += " order by updated_at desc limit %s"
        params.append(limit)
        async with self._connection() as connection:
            cursor = await connection.execute(query, params)
            rows = await cursor.fetchall()
        return [_run(row) for row in rows]

    async def open_folder_names(self, config_id: str) -> set[str]:
        """Folder names that already have a run which is not terminal."""

        async with self._connection() as connection:
            cursor = await connection.execute(
                """
                select folder_name from run
                where config_id = %s
                  and state not in ('done', 'discarded', 'failed')
                """,
                (config_id,),
            )
            rows = await cursor.fetchall()
        return {row["folder_name"] for row in rows}

    async def last_snapshot(
        self, config_id: str, folder_name: str
    ) -> tuple[SnapshotFile, ...] | None:
        """Snapshot of the newest run for this folder, if there was one.

        A folder left behind by a failed run must not silently spawn an
        identical run on every scan; comparing content is what tells a genuine
        re-download apart from untouched leftovers.
        """

        async with self._connection() as connection:
            cursor = await connection.execute(
                "select snapshot from run where config_id = %s"
                " and folder_name = %s order by created_at desc limit 1",
                (config_id, folder_name),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return tuple(SnapshotFile.from_json(item) for item in row["snapshot"] or ())

    async def next_active_run(self) -> Run | None:
        # Derived from the enum: a hand-written copy of this list once
        # dropped 'discarding' and left those runs stuck forever.
        active = [state.value for state in RunState if state.is_active]
        async with self._connection() as connection:
            cursor = await connection.execute(
                """
                select * from run
                where state = any(%s)
                order by updated_at
                limit 1
                """,
                (active,),
            )
            row = await cursor.fetchone()
        return _run(row) if row else None

    async def set_state(
        self,
        run_id: str,
        state: RunState,
        *,
        error: dict[str, Any] | None = None,
        bump_attempts: bool = False,
    ) -> None:
        async with self._connection() as connection:
            await connection.execute(
                """
                update run
                set state = %s,
                    error = %s::jsonb,
                    attempts = attempts + %s,
                    updated_at = now()
                where id = %s
                """,
                (
                    state.value,
                    json.dumps(error) if error is not None else None,
                    1 if bump_attempts else 0,
                    run_id,
                ),
            )

    async def set_plan(self, run_id: str, plan: Plan | None) -> None:
        async with self._connection() as connection:
            await connection.execute(
                "update run set plan = %s::jsonb, updated_at = now()"
                " where id = %s",
                (json.dumps(plan.to_json()) if plan else None, run_id),
            )

    async def set_extra(self, run_id: str, extra: dict[str, Any]) -> None:
        async with self._connection() as connection:
            await connection.execute(
                "update run set extra = %s::jsonb, updated_at = now()"
                " where id = %s",
                (json.dumps(extra), run_id),
            )

    async def set_result(self, run_id: str, result: RunResult) -> None:
        async with self._connection() as connection:
            await connection.execute(
                "update run set result = %s::jsonb, updated_at = now()"
                " where id = %s",
                (json.dumps(result.to_json()), run_id),
            )

    async def set_snapshot(
        self, run_id: str, snapshot: Sequence[SnapshotFile]
    ) -> None:
        async with self._connection() as connection:
            await connection.execute(
                "update run set snapshot = %s::jsonb, updated_at = now()"
                " where id = %s",
                (json.dumps([item.to_json() for item in snapshot]), run_id),
            )

    async def append_executed(self, run_id: str, executed: ExecutedMove) -> None:
        """Record one completed move.

        Written per move so a crash leaves an accurate ledger for replay and
        for reapply's revert step.
        """

        async with self._connection() as connection:
            await connection.execute(
                """
                update run
                set executed_moves = executed_moves || %s::jsonb,
                    updated_at = now()
                where id = %s
                """,
                (json.dumps([executed.to_json()]), run_id),
            )

    async def clear_executed(self, run_id: str) -> None:
        async with self._connection() as connection:
            await connection.execute(
                "update run set executed_moves = '[]'::jsonb,"
                " updated_at = now() where id = %s",
                (run_id,),
            )

    async def delete_run(self, run_id: str) -> None:
        async with self._connection() as connection:
            await connection.execute("delete from run where id = %s", (run_id,))

    # ---- logs and interactions ----------------------------------------

    async def log(
        self,
        run_id: str,
        message: str,
        *,
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> None:
        async with self._connection() as connection:
            await connection.execute(
                "insert into run_log (run_id, level, message, data)"
                " values (%s, %s, %s, %s::jsonb)",
                (run_id, level, message, json.dumps(data) if data else None),
            )

    async def list_logs(self, run_id: str, limit: int = 200) -> list[dict[str, Any]]:
        async with self._connection() as connection:
            cursor = await connection.execute(
                "select ts, level, message, data from run_log"
                " where run_id = %s order by id desc limit %s",
                (run_id, limit),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in reversed(rows)]

    async def add_interaction(self, run_id: str, role: str, content: str) -> None:
        async with self._connection() as connection:
            await connection.execute(
                "insert into interaction (run_id, role, content)"
                " values (%s, %s, %s)",
                (run_id, role, content),
            )

    async def list_interactions(self, run_id: str) -> list[dict[str, Any]]:
        async with self._connection() as connection:
            cursor = await connection.execute(
                "select role, content, ts from interaction"
                " where run_id = %s order by id",
                (run_id,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]


def _watch_config(row: dict[str, Any]) -> WatchConfig:
    return WatchConfig(
        id=str(row["id"]),
        name=row["name"],
        inbound_root=row["inbound_root"],
        library_root=row["library_root"],
        media_type=MediaType(row["media_type"]),
        enabled=row["enabled"],
        stability_seconds=row["stability_seconds"],
        acquire_subtitles=row["acquire_subtitles"],
        subtitle_variant=SubtitleVariant(row["subtitle_variant"]),
        notify=row["notify"],
        replace_enabled=row["replace_enabled"],
        replace_extra_dirs=tuple(row["replace_extra_dirs"] or ()),
        replace_auto_ratio=row["replace_auto_ratio"],
    )


def _run(row: dict[str, Any]) -> Run:
    return Run(
        id=str(row["id"]),
        config_id=str(row["config_id"]),
        folder_name=row["folder_name"],
        state=RunState(row["state"]),
        snapshot=tuple(
            SnapshotFile.from_json(item) for item in row["snapshot"] or ()
        ),
        plan=Plan.from_json(row["plan"]) if row["plan"] else None,
        executed_moves=tuple(
            ExecutedMove.from_json(item) for item in row["executed_moves"] or ()
        ),
        result=RunResult.from_json(row["result"]) if row["result"] else None,
        error=row["error"],
        attempts=row["attempts"],
        extra=row["extra"] or {},
    )
