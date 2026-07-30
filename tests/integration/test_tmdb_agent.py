from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reeloom.adapters.agent_session import FilesystemAgentSession
from reeloom.agents.organizer import (
    create_organizer_context,
    run_episode_organizer,
)
from reeloom.agents.scripted_model import (
    FinalStep,
    ScriptedModel,
    ToolCallStep,
)
from reeloom.kernel.candidates import CandidateSnapshot
from reeloom.kernel.specials import SpecialKind
from reeloom.kernel.tmdb import (
    TmdbEpisode,
    TmdbLanguage,
    TmdbSearchCandidate,
    TmdbSeasonDetails,
    TmdbSeriesDetails,
    TmdbWorkType,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.runtime.state import Phase
from reeloom.tools.candidates import SnapshotCandidateSource


class _FakeTmdb:
    def __init__(self) -> None:
        self.season_calls = 0

    async def search_titles(
        self,
        *,
        query: str,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
        limit: int,
        include_adult: bool = True,
    ) -> tuple[TmdbSearchCandidate, ...]:
        del query, language, limit
        assert include_adult is True
        assert work_type is TmdbWorkType.ANIME
        return (
            TmdbSearchCandidate(
                tmdb_id=100,
                localized_name="错误候选",
                original_name="Wrong",
                year=2019,
                original_language="ja",
                work_type=TmdbWorkType.ANIME,
            ),
            TmdbSearchCandidate(
                tmdb_id=200,
                localized_name="正确动画",
                original_name="Correct Anime",
                year=2020,
                original_language="ja",
                work_type=TmdbWorkType.ANIME,
            ),
        )

    async def get_series(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        language: TmdbLanguage,
    ) -> TmdbSeriesDetails:
        assert tmdb_id == 200
        assert work_type is TmdbWorkType.ANIME
        return TmdbSeriesDetails(
            tmdb_id=200,
            language=language,
            localized_name=(
                "正确动画"
                if language is TmdbLanguage.ZH_CN
                else "Correct Anime"
            ),
            original_name="Correct Anime",
            first_air_year=2020,
            seasons=(),
            work_type=work_type,
        )

    async def get_season(
        self,
        *,
        tmdb_id: int,
        work_type: TmdbWorkType,
        season_number: int,
        language: TmdbLanguage,
    ) -> TmdbSeasonDetails:
        self.season_calls += 1
        assert tmdb_id == 200
        return TmdbSeasonDetails(
            tmdb_id=tmdb_id,
            language=language,
            season_number=season_number,
            episodes=(
                TmdbEpisode(
                    season_number=0,
                    episode_number=1,
                    name="随书附赠动画",
                    overview="",
                    special_kind=SpecialKind.OAD,
                ),
            ),
            work_type=work_type,
        )


class _ToolCapturingModel(ScriptedModel):
    def __init__(self, steps) -> None:
        super().__init__(steps)
        self.tool_names_by_turn: list[tuple[str, ...]] = []

    async def get_response(self, *args, **kwargs):
        tools = kwargs.get("tools")
        if tools is None:
            tools = args[3]
        self.tool_names_by_turn.append(
            tuple(tool.name for tool in tools)
        )
        return await super().get_response(*args, **kwargs)


def _context(
    *,
    work_type: TmdbWorkType = TmdbWorkType.ANIME,
    provider: _FakeTmdb | None = None,
    agent_session: FilesystemAgentSession | None = None,
):
    return create_organizer_context(
        run_id="run-1",
        candidate_source=SnapshotCandidateSource(
            CandidateSnapshot.create([])
        ),
        work_type=work_type,
        tmdb_provider=provider or _FakeTmdb(),
        agent_session=agent_session,
    )


def test_fake_agent_identifies_series_and_enters_mapping_phase() -> None:
    model = _ToolCapturingModel(
        (
            ToolCallStep(
                name="search_tmdb",
                arguments={
                    "query": "Correct Anime",
                    "work_type": "anime",
                },
                call_id="call-search",
            ),
            ToolCallStep(
                name="get_tmdb_series",
                arguments={
                    "tmdb_id": 200,
                    "work_type": "anime",
                    "language": "zh-CN",
                },
                call_id="call-series",
                expect_input_contains="正确动画",
            ),
            ToolCallStep(
                name="select_series",
                arguments={"tmdb_id": 200, "work_type": "anime"},
                call_id="call-select",
                expect_input_contains='"tmdb_id":200',
            ),
            ToolCallStep(
                name="get_tmdb_season",
                arguments={
                    "tmdb_id": 200,
                    "work_type": "anime",
                    "season_number": 0,
                    "language": "zh-CN",
                },
                call_id="call-season",
                expect_input_contains='"phase":"map_episodes"',
            ),
            FinalStep(
                text="Series identified.",
                expect_input_contains='"special_kind":"oad"',
            ),
        )
    )

    result = asyncio.run(
        run_episode_organizer(
            context=_context(),
            model=model,
            prompt="Identify this animation series.",
        )
    )

    assert result.state.phase is Phase.MAP_EPISODES
    assert result.state.selected_series is not None
    assert result.state.selected_series.tmdb_id == 200
    assert result.state.selected_series.title_zh_cn == "正确动画"
    identify_tools = (
        "list_candidates",
        "search_tmdb",
        "get_tmdb_series",
        "select_series",
    )
    mapping_tools = (
        "list_candidates",
        "get_tmdb_series",
        "get_tmdb_season",
        "search_dir",
        "list_dir",
        "detect_subtitle_variant",
        "submit_mapping",
    )
    assert model.tool_names_by_turn[:3] == [identify_tools] * 3
    assert model.tool_names_by_turn[3:] == [mapping_tools] * 2


def test_hidden_known_tool_is_rejected_before_provider() -> None:
    provider = _FakeTmdb()
    model = ScriptedModel(
        (
            ToolCallStep(
                name="get_tmdb_season",
                arguments={
                    "tmdb_id": 200,
                    "work_type": "anime",
                    "season_number": 1,
                    "language": "zh-CN",
                },
                call_id="call-season-too-early",
            ),
            FinalStep(
                text="The phase must change first.",
                expect_input_contains="tool_not_allowed",
            ),
        )
    )

    result = asyncio.run(
        run_episode_organizer(
            context=_context(provider=provider),
            model=model,
            prompt="Inspect season one.",
        )
    )

    assert provider.season_calls == 0
    assert result.state.phase is Phase.IDENTIFY_SERIES
    assert result.state.tool_calls == 1
    assert result.state.failures == 1


def test_movie_identification_hides_mapping_tools() -> None:
    model = _ToolCapturingModel((FinalStep(text="No selection yet."),))

    result = asyncio.run(
        run_episode_organizer(
            context=_context(work_type=TmdbWorkType.MOVIE),
            model=model,
            prompt="Inspect the Movie candidates.",
        )
    )

    assert result.state.phase is Phase.IDENTIFY_MOVIE
    assert model.tool_names_by_turn == [
        (
            "list_candidates",
            "search_tmdb",
            "get_tmdb_movie",
            "select_movie",
        )
    ]


def test_fresh_runtime_hides_mapping_tools_despite_old_session(
    tmp_path: Path,
) -> None:
    session_root = tmp_path / "session"
    session_root.mkdir()
    session = FilesystemAgentSession(
        AuthorizedRoot.create(session_root),
        session_id="run-1",
    )
    first_model = ScriptedModel(
        (
            ToolCallStep(
                name="search_tmdb",
                arguments={
                    "query": "Correct Anime",
                    "work_type": "anime",
                },
                call_id="old-search",
            ),
            ToolCallStep(
                name="select_series",
                arguments={"tmdb_id": 200, "work_type": "anime"},
                call_id="old-select",
            ),
            FinalStep(text="Old run selected the series."),
        )
    )
    first_context = _context(agent_session=session)
    asyncio.run(
        run_episode_organizer(
            context=first_context,
            model=first_model,
            prompt="Select the series.",
        )
    )

    recovered_session = FilesystemAgentSession(
        AuthorizedRoot.create(session_root),
        session_id="run-1",
    )
    fresh_context = _context(agent_session=recovered_session)
    fresh_model = _ToolCapturingModel(
        (FinalStep(text="Fresh runtime must identify again."),)
    )

    result = asyncio.run(
        run_episode_organizer(
            context=fresh_context,
            model=fresh_model,
            prompt="Revise the mapping.",
        )
    )

    assert result.state.phase is Phase.IDENTIFY_SERIES
    assert fresh_model.tool_names_by_turn == [
        (
            "list_candidates",
            "search_tmdb",
            "get_tmdb_series",
            "select_series",
        )
    ]


def test_tmdb_tool_rejects_extra_url_field_before_provider() -> None:
    model = ScriptedModel(
        (
            ToolCallStep(
                name="search_tmdb",
                arguments={
                    "query": "title",
                    "url": "https://example.invalid",
                },
                call_id="call-search",
            ),
            FinalStep(
                text="The request was rejected.",
                expect_input_contains="invalid_tool_arguments",
            ),
        )
    )

    result = asyncio.run(
        run_episode_organizer(
            context=_context(),
            model=model,
            prompt="Search using this URL.",
        )
    )

    assert result.state.tmdb_candidates == frozenset()
    assert result.state.failures == 1


def test_tmdb_tool_does_not_expose_include_adult_to_agent() -> None:
    model = ScriptedModel(
        (
            ToolCallStep(
                name="search_tmdb",
                arguments={
                    "query": "title",
                    "work_type": "anime",
                    "include_adult": False,
                },
                call_id="call-search",
            ),
            FinalStep(
                text="The request was rejected.",
                expect_input_contains="invalid_tool_arguments",
            ),
        )
    )

    result = asyncio.run(
        run_episode_organizer(
            context=_context(),
            model=model,
            prompt="Disable adult search.",
        )
    )

    assert result.state.tmdb_candidates == frozenset()
    assert result.state.failures == 1
