"""Full-stack offline journey.

Real scanner, real agent loop, real plan compiler, real executor, real API —
only the model, TMDB and the forum are substituted.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from reeloom.agent.identify import AgentIdentifier
from reeloom.executor import FilesystemExecutor
from reeloom.models import MediaType, RunState, WatchConfig
from reeloom.scanner import StabilityTracker
from reeloom.server.api import create_app
from reeloom.server.worker import Worker
from tests.conftest import make_files
from tests.fakes import FakeDatabase, FakeTmdb, ScriptedModel, StubClients, call

TOKEN = "journey-token-1234567890"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FOLDER = "[Group] Show S01 [1080p]"


def episodes(*pairs, season=1):
    return [
        {
            "candidate_id": candidate,
            "season": season,
            "episode_start": episode,
            "episode_end": episode,
        }
        for candidate, episode in pairs
    ]


class Harness:
    def __init__(self, database, worker, model) -> None:
        self.database = database
        self.worker = worker
        self.model = model

    async def drain(self, limit: int = 20) -> None:
        for _ in range(limit):
            if not await self.worker.tick():
                return


@pytest.fixture
def harness(config: WatchConfig, roots: tuple[Path, Path]) -> Harness:
    inbound, _ = roots
    make_files(
        inbound / FOLDER,
        "[Group] Show - 01 [1080p].mkv",
        "[Group] Show - 02 [1080p].mkv",
        "[Group] Show - 01 [CHS].ass",
        "Show.torrent",
    )
    database = FakeDatabase([config])
    model = ScriptedModel(
        call("search_tmdb", query="Show"),
        call("get_series", tmdb_id=123),
        call("get_season", tmdb_id=123, season=1),
        call(
            "submit_plan",
            tmdb_id=123,
            entries=episodes(("V1", 1), ("V2", 2), ("S1", 1)),
            notes="season 1 batch",
        ),
    )
    worker = Worker(
        database,
        identifier=AgentIdentifier(StubClients(model, FakeTmdb())),
        executor=FilesystemExecutor(database),
        tracker=StabilityTracker(clock=lambda: 5000.0),
    )
    return Harness(database, worker, model)


@pytest_asyncio.fixture
async def client(harness: Harness):
    app = create_app(
        database=harness.database, admin_token=TOKEN, worker=harness.worker
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_drop_a_folder_and_it_organizes_itself(
    harness: Harness, roots: tuple[Path, Path], client
) -> None:
    inbound, library = roots

    await harness.drain()

    season = library / "Show (2024) {tmdb-123}" / "S01"
    assert (season / "Show S01E01.mkv").is_file()
    assert (season / "Show S01E02.mkv").is_file()
    assert (season / "Show S01E01.chs.ass").is_file()
    # The torrent file was not deleted, just parked.
    assert (inbound / "archive" / FOLDER / "Show.torrent").is_file()
    assert not (inbound / FOLDER).exists()

    listing = (await client.get("/api/runs", headers=AUTH)).json()["runs"]
    assert listing[0]["state"] == "done"
    assert listing[0]["title"] == "Show"
    assert listing[0]["result"]["moved"] == 3
    assert listing[0]["result"]["archived"] == 1


async def test_a_second_drop_of_the_same_episodes_never_overwrites(
    harness: Harness, roots: tuple[Path, Path], config: WatchConfig
) -> None:
    inbound, library = roots
    await harness.drain()
    season = library / "Show (2024) {tmdb-123}" / "S01"
    original = (season / "Show S01E01.mkv").read_bytes()

    # The same release turns up again under a different folder name.
    make_files(
        inbound / "Show S01 REPACK",
        "[Group] Show - 01 [1080p].mkv",
        size=999,
    )
    harness.model.replies = [
        call("submit_plan", tmdb_id=123, entries=episodes(("V1", 1)))
    ]
    await harness.drain()

    assert (season / "Show S01E01.mkv").read_bytes() == original
    assert (inbound / "fail" / "Show S01 REPACK" / "[Group] Show - 01 [1080p].mkv").is_file()
    states = {run.folder_name: run.result for run in harness.database.runs.values()}
    assert states["Show S01 REPACK"].duplicates == ("[Group] Show - 01 [1080p].mkv",)


async def test_a_user_revision_moves_the_season_and_keeps_nothing_stale(
    harness: Harness, roots: tuple[Path, Path], client
) -> None:
    _, library = roots
    await harness.drain()
    run_id = next(iter(harness.database.runs))

    harness.model.replies = [
        call(
            "submit_plan",
            tmdb_id=123,
            entries=episodes(("V1", 1), ("V2", 2), ("S1", 1), season=2),
        )
    ]
    response = await client.post(
        f"/api/runs/{run_id}/revise",
        headers=AUTH,
        json={"message": "这是第二季"},
    )
    assert response.json()["state"] == "identifying"

    await harness.drain()

    root = library / "Show (2024) {tmdb-123}"
    assert (root / "S02/Show S02E01.mkv").is_file()
    assert (root / "S02/Show S02E01.chs.ass").is_file()
    assert not (root / "S01/Show S01E01.mkv").exists()
    assert harness.database.runs[run_id].state is RunState.DONE


async def test_a_second_season_joins_the_existing_folder(
    harness: Harness, roots: tuple[Path, Path]
) -> None:
    inbound, library = roots
    await harness.drain()

    make_files(inbound / "[Group] Show S02", "[Group] Show - 01 [1080p].mkv")
    harness.model.replies = [
        call("submit_plan", tmdb_id=123, entries=episodes(("V1", 1), season=2))
    ]
    await harness.drain()

    root = library / "Show (2024) {tmdb-123}"
    assert (root / "S01/Show S01E01.mkv").is_file()
    assert (root / "S02/Show S02E01.mkv").is_file()
    assert len([path for path in library.iterdir() if path.is_dir()]) == 1


async def test_a_legacy_folder_gains_its_tmdb_id(
    harness: Harness, roots: tuple[Path, Path]
) -> None:
    _, library = roots
    make_files(library / "Show" / "S01", "old episode.mkv")

    await harness.drain()

    assert not (library / "Show").exists()
    root = library / "Show (2024) {tmdb-123}"
    assert (root / "S01/old episode.mkv").is_file()
    assert (root / "S01/Show S01E01.mkv").is_file()


async def test_an_unidentifiable_folder_waits_for_a_human_and_can_be_discarded(
    harness: Harness, roots: tuple[Path, Path], client
) -> None:
    inbound, library = roots
    harness.model.replies = [
        call("report_problem", reason="folder holds two different series")
    ]

    await harness.drain()

    run_id = next(iter(harness.database.runs))
    run = harness.database.runs[run_id]
    assert run.state is RunState.NEEDS_ATTENTION
    assert run.error["reason"] == "folder holds two different series"
    # Nothing was touched while waiting.
    assert (inbound / FOLDER / "[Group] Show - 01 [1080p].mkv").is_file()

    await client.post(f"/api/runs/{run_id}/discard", headers=AUTH)
    await harness.drain()

    assert harness.database.runs[run_id].state is RunState.DISCARDED
    assert (
        inbound / "fail" / FOLDER / "[Group] Show - 01 [1080p].mkv"
    ).is_file()
    assert not any(library.iterdir())


async def test_a_movie_watch_produces_a_flat_layout(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    inbound, library = roots
    make_files(inbound / "Feature.2016.1080p", "movie.mkv", "trailer.mkv")
    movie_config = replace(config, media_type=MediaType.MOVIE)
    database = FakeDatabase([movie_config])
    worker = Worker(
        database,
        identifier=AgentIdentifier(
            StubClients(
                ScriptedModel(
                    call("search_tmdb", query="Feature"),
                    call("get_movie", tmdb_id=456),
                    call(
                        "submit_plan",
                        tmdb_id=456,
                        entries=[{"candidate_id": "V1"}],
                    ),
                ),
                FakeTmdb(),
            )
        ),
        executor=FilesystemExecutor(database),
        tracker=StabilityTracker(clock=lambda: 5000.0),
    )
    for _ in range(20):
        if not await worker.tick():
            break

    assert (library / "Feature (2016) {tmdb-456}" / "Feature (2016).mkv").is_file()
    assert (
        inbound / "archive" / "Feature.2016.1080p" / "trailer.mkv"
    ).is_file()


async def test_the_run_survives_a_restart_midway_through_execution(
    harness: Harness, roots: tuple[Path, Path]
) -> None:
    inbound, library = roots
    # Identify only, then stop as if the process died before executing.
    await harness.worker.scan()
    run_id = next(iter(harness.database.runs))
    await harness.worker.advance()  # pending -> identifying
    await harness.worker.advance()  # identifying -> executing

    assert harness.database.runs[run_id].state is RunState.EXECUTING

    await harness.worker.recover()
    await harness.drain()

    season = library / "Show (2024) {tmdb-123}" / "S01"
    assert (season / "Show S01E01.mkv").is_file()
    assert (season / "Show S01E02.mkv").is_file()
    assert harness.database.runs[run_id].state is RunState.DONE
