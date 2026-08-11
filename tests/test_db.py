"""Repository tests against a real PostgreSQL.

Run with ``REELOOM_TEST_POSTGRES_DSN`` pointing at a throwaway database.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from reeloom.db import Database
from reeloom.models import (
    ExecutedMove,
    FileKind,
    MediaIdentity,
    MediaType,
    Move,
    MoveKind,
    MoveOutcome,
    Plan,
    Root,
    RunResult,
    RunState,
    SnapshotFile,
    WatchConfig,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def database(postgres_dsn: str) -> AsyncIterator[Database]:
    async with Database.connect(postgres_dsn) as database:
        # Every test starts from an empty control plane.
        async with database._connection() as connection:  # noqa: SLF001
            await connection.execute(
                "truncate watch_config, run, run_log, interaction cascade"
            )
        yield database


@pytest_asyncio.fixture
async def watch(database: Database) -> WatchConfig:
    config = WatchConfig(
        id=str(uuid.uuid4()),
        name="anime",
        inbound_root="/tmp/inbound",
        library_root="/tmp/library",
        media_type=MediaType.ANIME,
    )
    return await database.create_config(config)


SNAPSHOT = (
    SnapshotFile("V1", "ep01.mkv", FileKind.VIDEO, 100),
    SnapshotFile("S1", "ep01.chs.srt", FileKind.SUBTITLE, 10),
)


async def test_migrate_is_idempotent(database: Database) -> None:
    await database.migrate()
    await database.migrate()


async def test_config_round_trip(database: Database, watch: WatchConfig) -> None:
    assert await database.get_config(watch.id) == watch

    await database.update_config(watch.id, {"name": "renamed", "enabled": False})
    stored = await database.get_config(watch.id)
    assert stored is not None and stored.name == "renamed"
    assert await database.list_configs(enabled_only=True) == []

    await database.delete_config(watch.id)
    assert await database.get_config(watch.id) is None


async def test_run_round_trip(database: Database, watch: WatchConfig) -> None:
    run = await database.create_run(
        config_id=watch.id, folder_name="Show", snapshot=SNAPSHOT
    )
    assert run is not None and run.snapshot == SNAPSHOT

    plan = Plan(
        identity=MediaIdentity(MediaType.ANIME, 1, "Show", 2024),
        moves=(
            Move(
                kind=MoveKind.MEDIA,
                source_root=Root.INBOUND,
                source_path="Show/ep01.mkv",
                dest_root=Root.LIBRARY,
                dest_path="Show (2024) {tmdb-1}/S01/Show S01E01.mkv",
                candidate_id="V1",
            ),
        ),
        unmapped=("O1",),
    )
    await database.set_plan(run.id, plan)
    await database.set_result(run.id, RunResult(moved=1, duplicates=("x",)))
    await database.set_state(run.id, RunState.DONE)

    stored = await database.get_run(run.id)
    assert stored is not None
    assert stored.plan == plan
    assert stored.result == RunResult(moved=1, duplicates=("x",))
    assert stored.state is RunState.DONE


async def test_only_one_open_run_per_folder(
    database: Database, watch: WatchConfig
) -> None:
    first = await database.create_run(
        config_id=watch.id, folder_name="Show", snapshot=SNAPSHOT
    )
    assert first is not None
    assert (
        await database.create_run(
            config_id=watch.id, folder_name="Show", snapshot=SNAPSHOT
        )
        is None
    )

    await database.set_state(first.id, RunState.DONE)
    second = await database.create_run(
        config_id=watch.id, folder_name="Show", snapshot=SNAPSHOT
    )
    assert second is not None and second.id != first.id


async def test_executed_moves_append_in_order(
    database: Database, watch: WatchConfig
) -> None:
    run = await database.create_run(
        config_id=watch.id, folder_name="Show", snapshot=SNAPSHOT
    )
    assert run is not None
    for index in range(3):
        await database.append_executed(
            run.id,
            ExecutedMove(
                move=Move(
                    kind=MoveKind.MEDIA,
                    source_root=Root.INBOUND,
                    source_path=f"Show/ep{index}.mkv",
                    dest_root=Root.LIBRARY,
                    dest_path=f"dest/ep{index}.mkv",
                ),
                outcome=MoveOutcome.MOVED,
            ),
        )

    stored = await database.get_run(run.id)
    assert stored is not None
    assert [item.move.source_path for item in stored.executed_moves] == [
        "Show/ep0.mkv",
        "Show/ep1.mkv",
        "Show/ep2.mkv",
    ]

    await database.clear_executed(run.id)
    stored = await database.get_run(run.id)
    assert stored is not None and stored.executed_moves == ()


async def test_last_snapshot_returns_newest_run(
    database: Database, watch: WatchConfig
) -> None:
    assert await database.last_snapshot(watch.id, "Show") is None
    run = await database.create_run(
        config_id=watch.id, folder_name="Show", snapshot=SNAPSHOT
    )
    assert run is not None
    assert await database.last_snapshot(watch.id, "Show") == SNAPSHOT


async def test_next_active_run_ignores_terminal_runs(
    database: Database, watch: WatchConfig
) -> None:
    run = await database.create_run(
        config_id=watch.id, folder_name="Show", snapshot=SNAPSHOT
    )
    assert run is not None
    assert (await database.next_active_run()) is not None

    await database.set_state(run.id, RunState.DONE)
    assert await database.next_active_run() is None


async def test_next_active_run_picks_up_a_discarding_run(
    database: Database, watch: WatchConfig
) -> None:
    # 'discarding' was once missing from the query and such runs hung forever.
    run = await database.create_run(
        config_id=watch.id, folder_name="Show", snapshot=SNAPSHOT
    )
    assert run is not None
    await database.set_state(run.id, RunState.DISCARDING)

    picked = await database.next_active_run()
    assert picked is not None and picked.id == run.id


async def test_settings_update_ignores_unknown_keys(database: Database) -> None:
    await database.update_settings({"tmdb_api_key": "k", "evil": "x"})
    settings = await database.get_settings()
    assert settings["tmdb_api_key"] == "k"
    assert "evil" not in settings


async def test_logs_and_interactions(
    database: Database, watch: WatchConfig
) -> None:
    run = await database.create_run(
        config_id=watch.id, folder_name="Show", snapshot=SNAPSHOT
    )
    assert run is not None
    await database.log(run.id, "first")
    await database.log(run.id, "second", level="warning", data={"a": 1})
    await database.add_interaction(run.id, "user", "wrong season")

    logs = await database.list_logs(run.id)
    assert [item["message"] for item in logs] == ["first", "second"]
    interactions = await database.list_interactions(run.id)
    assert interactions[0]["content"] == "wrong season"
