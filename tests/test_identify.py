from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from reeloom.adapters.llm import ModelReply, ToolCall
from reeloom.agent.identify import AgentIdentifier
from reeloom.models import (
    FileKind,
    MediaType,
    MoveKind,
    Run,
    RunState,
    SnapshotFile,
    SubtitleVariant,
    WatchConfig,
)
from reeloom.server.worker import NeedsAttention
from tests.fakes import FakeTmdb, ScriptedModel, StubClients, call

SNAPSHOT = (
    SnapshotFile("V1", "Show - 01.mkv", FileKind.VIDEO, 100),
    SnapshotFile("V2", "Show - 02.mkv", FileKind.VIDEO, 100),
    SnapshotFile(
        "S1", "Show - 01.chs.ass", FileKind.SUBTITLE, 10, SubtitleVariant.CHS
    ),
    SnapshotFile("O1", "Show.torrent", FileKind.OTHER, 1),
)


def make_run(snapshot=SNAPSHOT, **kwargs) -> Run:
    return Run(
        id="run-1",
        config_id="config-1",
        folder_name="[Group] Show [1080p]",
        state=RunState.IDENTIFYING,
        snapshot=snapshot,
        **kwargs,
    )


def episodes(*pairs):
    return [
        {
            "candidate_id": candidate,
            "season": 1,
            "episode_start": episode,
            "episode_end": episode,
        }
        for candidate, episode in pairs
    ]


async def identify(config: WatchConfig, *replies, run=None, tmdb=None):
    tmdb = tmdb or FakeTmdb()
    model = ScriptedModel(*replies)
    identifier = AgentIdentifier(StubClients(model, tmdb))
    return await identifier.identify(run or make_run(), config), model, tmdb


async def test_happy_path_produces_library_destinations(
    config: WatchConfig,
) -> None:
    plan, _, tmdb = await identify(
        config,
        call("search_tmdb", query="Show"),
        call("get_series", tmdb_id=123),
        call("get_season", tmdb_id=123, season=1),
        call(
            "submit_plan",
            tmdb_id=123,
            entries=episodes(("V1", 1), ("V2", 2), ("S1", 1)),
            notes="season 1",
        ),
    )

    assert plan.identity.tmdb_id == 123
    assert [move.dest_path for move in plan.moves] == [
        "Show (2024) {tmdb-123}/S01/Show S01E01.mkv",
        "Show (2024) {tmdb-123}/S01/Show S01E02.mkv",
        "Show (2024) {tmdb-123}/S01/Show S01E01.chs.ass",
    ]
    assert plan.unmapped == ("O1",)
    assert plan.notes == "season 1"
    assert "get_season:123:1" in tmdb.calls


async def test_title_and_year_come_from_tmdb_not_from_the_model(
    config: WatchConfig,
) -> None:
    tmdb = FakeTmdb()
    tmdb.series = {**tmdb.series, "title": "Renamed", "year": 2020}

    plan, _, _ = await identify(
        config,
        call("submit_plan", tmdb_id=123, entries=episodes(("V1", 1))),
        tmdb=tmdb,
    )

    assert plan.identity.title == "Renamed"
    assert plan.moves[0].dest_path.startswith("Renamed (2020) {tmdb-123}/")


async def test_rejected_mapping_is_returned_to_the_model_for_correction(
    config: WatchConfig,
) -> None:
    plan, model, _ = await identify(
        config,
        call("submit_plan", tmdb_id=123, entries=episodes(("V1", 1), ("V2", 1))),
        call("submit_plan", tmdb_id=123, entries=episodes(("V1", 1), ("V2", 2))),
    )

    # The rejection reached the model as a tool observation, not an exception.
    rejection = model.seen[-1][-1]
    assert rejection["role"] == "tool"
    assert "destination_collision" in rejection["content"]
    assert len(plan.moves) == 2


async def test_unknown_candidate_is_rejected_not_invented(
    config: WatchConfig,
) -> None:
    plan, model, _ = await identify(
        config,
        call("submit_plan", tmdb_id=123, entries=episodes(("V9", 1))),
        call("submit_plan", tmdb_id=123, entries=episodes(("V1", 1))),
    )

    assert "unknown_candidate" in model.seen[-1][-1]["content"]
    assert len(plan.moves) == 1


async def test_report_problem_escalates_to_a_human(config: WatchConfig) -> None:
    with pytest.raises(NeedsAttention) as error:
        await identify(config, call("report_problem", reason="two series mixed"))

    assert error.value.code == "agent_reported_problem"
    assert error.value.context["reason"] == "two series mixed"


async def test_prose_alone_cannot_settle_a_run(config: WatchConfig) -> None:
    with pytest.raises(NeedsAttention) as error:
        await identify(
            config,
            ModelReply(content="This looks like Show season 1."),
            ModelReply(content="I am confident about that."),
        )

    assert error.value.code == "agent_stopped"


async def test_malformed_tool_arguments_are_reported_and_survivable(
    config: WatchConfig,
) -> None:
    broken = ModelReply(
        tool_calls=(
            ToolCall(id="c1", name="submit_plan", arguments="{not json"),
        )
    )
    plan, model, _ = await identify(
        config,
        broken,
        call("submit_plan", tmdb_id=123, entries=episodes(("V1", 1))),
    )

    assert "malformed_tool_arguments" in model.seen[-1][-1]["content"]
    assert len(plan.moves) == 1


async def test_a_looping_agent_eventually_escalates(config: WatchConfig) -> None:
    with pytest.raises(NeedsAttention) as error:
        await identify(
            config, *[call("search_tmdb", query="Show") for _ in range(30)]
        )

    assert error.value.code == "max_turns_exhausted"


async def test_movie_config_exposes_movie_tools_and_no_episodes(
    config: WatchConfig,
) -> None:
    movie_config = replace(config, media_type=MediaType.MOVIE)
    plan, model, _ = await identify(
        movie_config,
        call("search_tmdb", query="Feature"),
        call("get_movie", tmdb_id=456),
        call("submit_plan", tmdb_id=456, entries=[{"candidate_id": "V1"}]),
    )

    names = {schema["function"]["name"] for schema in model.tools}
    assert names == {"search_tmdb", "get_movie", "submit_plan", "report_problem"}
    assert plan.moves[0].dest_path == (
        "Feature (2016) {tmdb-456}/Feature (2016).mkv"
    )


async def test_oversized_folder_goes_straight_to_a_human(
    config: WatchConfig,
) -> None:
    snapshot = tuple(
        SnapshotFile(f"V{index}", f"ep{index}.mkv", FileKind.VIDEO, 1)
        for index in range(1, 600)
    )
    with pytest.raises(NeedsAttention) as error:
        await identify(
            config,
            call("submit_plan", tmdb_id=123, entries=episodes(("V1", 1))),
            run=make_run(snapshot=snapshot),
        )

    assert error.value.code == "folder_too_large"


async def test_new_season_joins_an_existing_library_folder(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    _, library = roots
    (library / "Show (2024) {tmdb-123}" / "S01").mkdir(parents=True)

    plan, _, _ = await identify(
        config,
        call(
            "submit_plan",
            tmdb_id=123,
            entries=[
                {
                    "candidate_id": "V1",
                    "season": 2,
                    "episode_start": 1,
                    "episode_end": 1,
                }
            ],
        ),
    )

    assert [move.kind for move in plan.moves] == [MoveKind.MEDIA]
    assert plan.moves[0].dest_path == (
        "Show (2024) {tmdb-123}/S02/Show S02E01.mkv"
    )


async def test_legacy_folder_is_renamed_to_carry_the_tmdb_id(
    config: WatchConfig, roots: tuple[Path, Path]
) -> None:
    _, library = roots
    (library / "Show" / "S01").mkdir(parents=True)

    plan, _, _ = await identify(
        config,
        call(
            "submit_plan",
            tmdb_id=123,
            entries=[
                {
                    "candidate_id": "V1",
                    "season": 2,
                    "episode_start": 1,
                    "episode_end": 1,
                }
            ],
        ),
    )

    rename, media = plan.moves
    assert rename.kind is MoveKind.FOLDER_RENAME
    assert (rename.source_path, rename.dest_path) == (
        "Show",
        "Show (2024) {tmdb-123}",
    )
    assert media.dest_path.startswith("Show (2024) {tmdb-123}/S02/")


async def test_revision_feedback_is_carried_into_the_conversation(
    config: WatchConfig,
) -> None:
    run = make_run(extra={"revisions": ["these are season 2, not season 1"]})

    _, model, _ = await identify(
        config,
        call("submit_plan", tmdb_id=123, entries=episodes(("V1", 1))),
        run=run,
    )

    prompts = [
        message["content"]
        for message in model.seen[0]
        if message["role"] == "user"
    ]
    assert any("season 2, not season 1" in text for text in prompts)
