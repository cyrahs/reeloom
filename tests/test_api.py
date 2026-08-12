from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

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
    Run,
    RunResult,
    RunState,
    SnapshotFile,
    WatchConfig,
)
from reeloom.server.api import create_app
from reeloom.server.worker import IntakeFolder
from tests.fakes import FakeDatabase

TOKEN = "test-admin-token-1234567890"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

IDENTITY = MediaIdentity(MediaType.ANIME, 123, "Show", 2024)
PLAN = Plan(
    identity=IDENTITY,
    moves=(
        Move(
            kind=MoveKind.MEDIA,
            source_root=Root.INBOUND,
            source_path="Drop/ep01.mkv",
            dest_root=Root.LIBRARY,
            dest_path="Show (2024) {tmdb-123}/S01/Show S01E01.mkv",
            candidate_id="V1",
        ),
    ),
    unmapped=("O1",),
)


class StubAnswerer:
    def __init__(self) -> None:
        self.questions: list[str] = []
        self.histories: list[list[dict]] = []

    async def answer(self, run, config, question, history=None):
        self.questions.append(question)
        self.histories.append(list(history or []))
        return "It is season 1."


class StubWorker:
    def __init__(self) -> None:
        self.woken = 0
        self.intake: list[IntakeFolder] = []

    def wake(self) -> None:
        self.woken += 1

    def intake_status(self) -> list[IntakeFolder]:
        return self.intake


@pytest.fixture
def database(config: WatchConfig) -> FakeDatabase:
    return FakeDatabase([config])


@pytest.fixture
def answerer() -> StubAnswerer:
    return StubAnswerer()


@pytest.fixture
def worker() -> StubWorker:
    return StubWorker()


@pytest_asyncio.fixture
async def client(database, answerer, worker):
    app = create_app(
        database=database, admin_token=TOKEN, worker=worker, answerer=answerer
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client


async def add_run(database: FakeDatabase, config: WatchConfig, **kwargs) -> Run:
    run = Run(
        id="run-1",
        config_id=config.id,
        folder_name="Drop",
        state=kwargs.pop("state", RunState.DONE),
        snapshot=(SnapshotFile("V1", "ep01.mkv", FileKind.VIDEO, 10),),
        **kwargs,
    )
    database.runs[run.id] = run
    return run


# ---- auth ---------------------------------------------------------------


async def test_every_api_route_needs_the_admin_token(client) -> None:
    for method, path in [
        ("get", "/api/runs"),
        ("get", "/api/intake"),
        ("get", "/api/configs"),
        ("get", "/api/settings"),
        ("get", "/api/fs/dirs"),
        ("post", "/api/configs"),
    ]:
        response = await getattr(client, method)(path)
        assert response.status_code == 401, path


async def test_a_wrong_token_is_rejected(client) -> None:
    response = await client.get(
        "/api/runs", headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 401


async def test_health_needs_no_token(client) -> None:
    assert (await client.get("/api/health")).status_code == 200


# ---- runs ---------------------------------------------------------------


async def test_run_list_and_detail(client, database, config) -> None:
    await add_run(database, config, plan=PLAN, result=RunResult(moved=1))

    listing = await client.get("/api/runs", headers=AUTH)
    assert listing.json()["runs"][0]["title"] == "Show"
    assert listing.json()["runs"][0]["move_count"] == 1

    detail = await client.get("/api/runs/run-1", headers=AUTH)
    body = detail.json()
    assert body["plan"]["moves"][0]["dest_path"].endswith("Show S01E01.mkv")
    assert body["snapshot"][0]["candidate_id"] == "V1"
    assert body["result"]["moved"] == 1


async def test_unknown_run_is_404(client) -> None:
    assert (await client.get("/api/runs/missing", headers=AUTH)).status_code == 404


async def test_intake_reports_the_workers_pending_folders(
    client, worker, config
) -> None:
    worker.intake.append(
        IntakeFolder(
            config_id=config.id,
            config_name=config.name,
            folder_name="Incoming",
            file_count=2,
            total_bytes=100,
            status="settling",
            remaining_seconds=42.0,
            scanned_at=1_000.0,
        )
    )

    response = await client.get("/api/intake", headers=AUTH)

    body = response.json()
    assert body["now"] > 0
    [folder] = body["folders"]
    assert folder["folder_name"] == "Incoming"
    assert folder["status"] == "settling"
    assert folder["remaining_seconds"] == 42.0
    assert folder["scanned_at"] == 1_000.0


async def test_ask_is_read_only_and_recorded(
    client, database, config, answerer
) -> None:
    await add_run(database, config, plan=PLAN)

    response = await client.post(
        "/api/runs/run-1/ask", headers=AUTH, json={"message": "which season?"}
    )

    assert response.json()["answer"] == "It is season 1."
    assert answerer.questions == ["which season?"]
    assert database.runs["run-1"].state is RunState.DONE
    assert [item["role"] for item in database.interactions["run-1"]] == [
        "user",
        "agent",
    ]


async def test_ask_replays_the_stored_chat_as_context(
    client, database, config, answerer
) -> None:
    await add_run(database, config, plan=PLAN)
    await database.add_interaction("run-1", "user", "which season?")
    await database.add_interaction("run-1", "agent", "It is season 1.")

    await client.post(
        "/api/runs/run-1/ask", headers=AUTH, json={"message": "and the year?"}
    )

    assert [item["content"] for item in answerer.histories[-1]] == [
        "which season?",
        "It is season 1.",
    ]


async def test_revising_a_finished_run_reidentifies_it(
    client, database, config, worker
) -> None:
    await add_run(database, config, plan=PLAN, state=RunState.DONE)

    response = await client.post(
        "/api/runs/run-1/revise", headers=AUTH, json={"message": "season 2"}
    )

    assert response.json()["state"] == "identifying"
    run = database.runs["run-1"]
    assert run.state is RunState.IDENTIFYING
    assert database.interactions["run-1"] == [
        {"role": "revision", "content": "season 2"}
    ]
    assert worker.woken == 1


async def test_revising_a_busy_run_is_refused(client, database, config) -> None:
    await add_run(database, config, state=RunState.EXECUTING)

    response = await client.post(
        "/api/runs/run-1/revise", headers=AUTH, json={"message": "x"}
    )

    assert response.status_code == 409


async def test_discard_hands_the_run_to_the_worker(
    client, database, config, worker
) -> None:
    await add_run(database, config, state=RunState.NEEDS_ATTENTION)

    response = await client.post("/api/runs/run-1/discard", headers=AUTH)

    assert response.json()["state"] == "discarding"
    assert database.runs["run-1"].state is RunState.DISCARDING
    assert worker.woken == 1


async def test_retry_resumes_an_executed_run_from_its_ledger(
    client, database, config
) -> None:
    await add_run(
        database,
        config,
        state=RunState.FAILED,
        plan=PLAN,
        executed_moves=(
            ExecutedMove(PLAN.moves[0], MoveOutcome.MOVED),
        ),
    )

    response = await client.post("/api/runs/run-1/retry", headers=AUTH)

    assert response.json()["state"] == "executing"


async def test_retry_reidentifies_a_run_that_never_executed(
    client, database, config
) -> None:
    await add_run(database, config, state=RunState.FAILED)

    response = await client.post("/api/runs/run-1/retry", headers=AUTH)

    assert response.json()["state"] == "pending"


async def test_a_busy_run_cannot_be_deleted(client, database, config) -> None:
    await add_run(database, config, state=RunState.EXECUTING)
    assert (
        await client.delete("/api/runs/run-1", headers=AUTH)
    ).status_code == 409


async def test_a_settled_run_can_be_deleted(client, database, config) -> None:
    await add_run(database, config, state=RunState.DONE)
    assert (await client.delete("/api/runs/run-1", headers=AUTH)).status_code == 200


# ---- replacement confirmations ------------------------------------------


async def add_parked_replacement(database: FakeDatabase, config: WatchConfig) -> Run:
    return await add_run(
        database,
        config,
        state=RunState.NEEDS_ATTENTION,
        error={"code": "replace_confirmation", "groups": []},
        extra={"replace": {"groups": [], "resolution": None}},
    )


async def test_resolving_a_parked_replacement(client, database, config) -> None:
    await add_parked_replacement(database, config)

    response = await client.post(
        "/api/runs/run-1/replace", headers=AUTH, json={"action": "replace"}
    )

    assert response.status_code == 200
    assert response.json() == {"state": "comparing"}
    run = database.runs["run-1"]
    assert run.state is RunState.COMPARING
    assert run.extra["replace"]["resolution"] == "replace"


async def test_resolving_needs_a_parked_confirmation(
    client, database, config
) -> None:
    await add_run(database, config, state=RunState.DONE)
    response = await client.post(
        "/api/runs/run-1/replace", headers=AUTH, json={"action": "keep_both"}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "not_awaiting_replace_confirmation"


async def test_resolving_a_busy_run_is_refused(client, database, config) -> None:
    await add_run(database, config, state=RunState.EXECUTING)
    response = await client.post(
        "/api/runs/run-1/replace", headers=AUTH, json={"action": "replace"}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "run_is_busy"


async def test_unknown_resolution_action_is_refused(
    client, database, config
) -> None:
    await add_parked_replacement(database, config)
    response = await client.post(
        "/api/runs/run-1/replace", headers=AUTH, json={"action": "merge"}
    )
    assert response.status_code == 422


async def test_run_detail_carries_the_replace_decision(
    client, database, config
) -> None:
    await add_parked_replacement(database, config)
    response = await client.get("/api/runs/run-1", headers=AUTH)
    assert response.json()["replace_decision"] == {
        "groups": [],
        "resolution": None,
    }


# ---- configuration ------------------------------------------------------


async def test_config_crud(client, tmp_path: Path) -> None:
    created = await client.post(
        "/api/configs",
        headers=AUTH,
        json={
            "name": "anime",
            "inbound_root": str(tmp_path / "in"),
            "library_root": str(tmp_path / "lib"),
            "media_type": "anime",
        },
    )
    assert created.status_code == 201
    config_id = created.json()["id"]
    assert created.json()["stability_seconds"] == 120
    assert created.json()["subtitle_variant"] == "chs"
    assert created.json()["replace_enabled"] is False
    assert created.json()["replace_extra_dirs"] == []
    assert created.json()["replace_auto_ratio"] == 1.2

    patched = await client.patch(
        f"/api/configs/{config_id}", headers=AUTH, json={"enabled": False}
    )
    assert patched.json()["enabled"] is False

    patched = await client.patch(
        f"/api/configs/{config_id}", headers=AUTH, json={"subtitle_variant": "cht"}
    )
    assert patched.json()["subtitle_variant"] == "cht"

    # chi is a detection outcome, not a preference.
    refused = await client.patch(
        f"/api/configs/{config_id}", headers=AUTH, json={"subtitle_variant": "chi"}
    )
    assert refused.status_code == 422

    patched = await client.patch(
        f"/api/configs/{config_id}",
        headers=AUTH,
        json={
            "replace_enabled": True,
            "replace_extra_dirs": [str(tmp_path / "anirss")],
            "replace_auto_ratio": 1.5,
        },
    )
    assert patched.json()["replace_enabled"] is True
    assert patched.json()["replace_extra_dirs"] == [str(tmp_path / "anirss")]
    assert patched.json()["replace_auto_ratio"] == 1.5

    assert (
        await client.delete(f"/api/configs/{config_id}", headers=AUTH)
    ).status_code == 200


async def test_extra_dirs_must_be_absolute_and_disjoint(
    client, tmp_path: Path
) -> None:
    def body(dirs: list[str]) -> dict:
        return {
            "name": "anime",
            "inbound_root": str(tmp_path / "in"),
            "library_root": str(tmp_path / "lib"),
            "media_type": "anime",
            "replace_extra_dirs": dirs,
        }

    for dirs in (
        ["relative/dir"],
        [str(tmp_path / "lib")],  # equals a root
        [str(tmp_path / "lib" / "nested")],  # inside a root
        [str(tmp_path)],  # contains both roots
        [str(tmp_path / "a"), str(tmp_path / "a")],  # duplicate
    ):
        response = await client.post("/api/configs", headers=AUTH, json=body(dirs))
        assert response.status_code == 422, dirs


async def test_extra_dirs_are_checked_against_the_stored_roots(
    client, tmp_path: Path
) -> None:
    created = await client.post(
        "/api/configs",
        headers=AUTH,
        json={
            "name": "anime",
            "inbound_root": str(tmp_path / "in"),
            "library_root": str(tmp_path / "lib"),
            "media_type": "anime",
        },
    )
    config_id = created.json()["id"]
    # A patch that only sets extra dirs still validates against the roots
    # the config already has.
    refused = await client.patch(
        f"/api/configs/{config_id}",
        headers=AUTH,
        json={"replace_extra_dirs": [str(tmp_path / "lib")]},
    )
    assert refused.status_code == 422


async def test_relative_roots_are_refused(client) -> None:
    response = await client.post(
        "/api/configs",
        headers=AUTH,
        json={
            "name": "anime",
            "inbound_root": "relative/in",
            "library_root": "/abs/lib",
            "media_type": "anime",
        },
    )
    assert response.status_code == 422


async def test_unknown_media_type_is_refused(client, tmp_path: Path) -> None:
    response = await client.post(
        "/api/configs",
        headers=AUTH,
        json={
            "name": "x",
            "inbound_root": str(tmp_path),
            "library_root": str(tmp_path),
            "media_type": "documentary",
        },
    )
    assert response.status_code == 422


# ---- filesystem ---------------------------------------------------------


async def test_dir_listing_shows_visible_subdirectories(
    client, tmp_path: Path
) -> None:
    base = tmp_path / "browse"
    (base / "Bangumi").mkdir(parents=True)
    (base / "anime").mkdir()
    (base / ".hidden").mkdir()
    (base / "loose-file.mkv").write_bytes(b"x")

    response = await client.get(
        "/api/fs/dirs", headers=AUTH, params={"path": str(base)}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["path"] == str(base.resolve())
    assert body["parent"] == str(base.resolve().parent)
    assert body["dirs"] == ["anime", "Bangumi"]


async def test_dir_listing_of_the_root_has_no_parent(client) -> None:
    response = await client.get(
        "/api/fs/dirs", headers=AUTH, params={"path": "/"}
    )
    assert response.status_code == 200
    assert response.json()["parent"] is None


async def test_dir_listing_refuses_relative_paths(client) -> None:
    response = await client.get(
        "/api/fs/dirs", headers=AUTH, params={"path": "relative/path"}
    )
    assert response.status_code == 422


async def test_dir_listing_of_a_missing_path_is_404(
    client, tmp_path: Path
) -> None:
    response = await client.get(
        "/api/fs/dirs", headers=AUTH, params={"path": str(tmp_path / "nope")}
    )
    assert response.status_code == 404


# ---- settings -----------------------------------------------------------


async def test_secrets_are_never_echoed_back(client, database) -> None:
    await client.put(
        "/api/settings",
        headers=AUTH,
        json={"tmdb_api_key": "secret-key", "llm_model": "gpt-5"},
    )

    body = (await client.get("/api/settings", headers=AUTH)).json()

    assert body["tmdb_api_key_set"] is True
    assert body["llm_model"] == "gpt-5"
    assert "secret-key" not in str(body)
    assert "tmdb_api_key" not in body


async def test_reasoning_effort_roundtrips_including_reset(client) -> None:
    await client.put(
        "/api/settings", headers=AUTH, json={"llm_reasoning_effort": "high"}
    )
    body = (await client.get("/api/settings", headers=AUTH)).json()
    assert body["llm_reasoning_effort"] == "high"

    # An empty string is a real value: back to the provider default.
    await client.put(
        "/api/settings", headers=AUTH, json={"llm_reasoning_effort": ""}
    )
    body = (await client.get("/api/settings", headers=AUTH)).json()
    assert body["llm_reasoning_effort"] == ""


async def test_trash_retention_roundtrips(client) -> None:
    response = await client.get("/api/settings", headers=AUTH)
    assert response.json()["trash_retention_days"] == 3

    await client.put(
        "/api/settings", headers=AUTH, json={"trash_retention_days": 0}
    )
    response = await client.get("/api/settings", headers=AUTH)
    assert response.json()["trash_retention_days"] == 0

    refused = await client.put(
        "/api/settings", headers=AUTH, json={"trash_retention_days": 999}
    )
    assert refused.status_code == 422


async def test_reasoning_effort_rejects_unknown_values(client) -> None:
    response = await client.put(
        "/api/settings", headers=AUTH, json={"llm_reasoning_effort": "ultra"}
    )
    assert response.status_code == 422
