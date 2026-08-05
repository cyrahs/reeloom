from __future__ import annotations

from argparse import Namespace
from typing import NoReturn, cast

import pytest

from reeloom.adapters.openai_model import OpenAIModelProvider
from reeloom.agents.organizer import run_episode_organizer
from reeloom.agents.scripted_model import ScriptedModel, ToolCallStep
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.subtitle_acquisition import SubtitleSearchCursorId
from reeloom.ports.subtitle_acquisition import (
    SubtitleSearchProviderError,
    SubtitleSearchRequest,
)
from scripts import openai_m13_live_smoke


def _unexpected(*_: object, **__: object) -> NoReturn:
    pytest.fail("disabled M13 live smoke accessed configuration or network")


def test_m13_smoke_does_not_load_dotenv_without_live_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        openai_m13_live_smoke,
        "_args",
        lambda: Namespace(live=False),
    )
    monkeypatch.setattr(
        openai_m13_live_smoke,
        "load_openai_live_configuration",
        _unexpected,
    )
    monkeypatch.setattr(openai_m13_live_smoke.asyncio, "run", _unexpected)

    assert openai_m13_live_smoke.main() == 2


def test_m13_smoke_does_not_load_dotenv_with_invalid_run_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        openai_m13_live_smoke,
        "_args",
        lambda: Namespace(live=True, runs=0),
    )
    monkeypatch.setattr(
        openai_m13_live_smoke,
        "load_openai_live_configuration",
        _unexpected,
    )

    assert openai_m13_live_smoke.main() == 2


def test_m13_smoke_accepts_model_options_from_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _run(*_: object, **kwargs: object) -> list[dict[str, object]]:
        captured.update(kwargs)
        return [{"passed": True}]

    monkeypatch.setattr(
        openai_m13_live_smoke,
        "_args",
        lambda: Namespace(
            live=True,
            model=None,
            runs=1,
            timeout_seconds=60,
            max_retries=2,
            reasoning_effort=None,
            verbosity=None,
        ),
    )
    live_configuration = openai_m13_live_smoke.OpenAILiveConfiguration(
        api_key="secret",
        base_url="https://gateway.example/v1",
        model_name="dotenv-model",
        reasoning_effort="xhigh",
    )
    monkeypatch.setattr(
        openai_m13_live_smoke,
        "load_openai_live_configuration",
        lambda **_: live_configuration,
    )
    monkeypatch.setattr(openai_m13_live_smoke, "_run", _run)

    assert openai_m13_live_smoke.main() == 0
    assert captured["model_name"] == "dotenv-model"
    assert captured["reasoning_effort"] == "xhigh"


def test_m13_smoke_runs_every_subtitle_scenario_and_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    async def _run_once(
        _provider: OpenAIModelProvider,
        scenario: openai_m13_live_smoke._Scenario,
        ordinal: int,
    ) -> dict[str, object]:
        name = scenario.name
        calls.append((name, ordinal))
        return {"passed": True, "scenario": name}

    monkeypatch.setattr(openai_m13_live_smoke, "_run_once", _run_once)
    results = openai_m13_live_smoke.asyncio.run(
        openai_m13_live_smoke._run_scenarios(
            cast(OpenAIModelProvider, object()),
            Namespace(scenario=None, runs=2),
        )
    )

    expected_names = [
        scenario.name for scenario in openai_m13_live_smoke._SCENARIOS
    ]
    assert [item["scenario"] for item in results] == [
        name for name in expected_names for _ in range(2)
    ]
    assert calls == [
        (name, ordinal)
        for name in expected_names
        for ordinal in range(2)
    ]


def test_m13_smoke_can_filter_scenarios(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _run_once(
        _provider: OpenAIModelProvider,
        scenario: openai_m13_live_smoke._Scenario,
        _ordinal: int,
    ) -> dict[str, object]:
        calls.append(scenario.name)
        return {"passed": True, "scenario": scenario.name}

    monkeypatch.setattr(openai_m13_live_smoke, "_run_once", _run_once)
    openai_m13_live_smoke.asyncio.run(
        openai_m13_live_smoke._run_scenarios(
            cast(OpenAIModelProvider, object()),
            Namespace(
                scenario=[
                    "indeterminate_probe_attention",
                    "search_unavailable_attention",
                ],
                runs=1,
            ),
        )
    )

    assert calls == [
        "indeterminate_probe_attention",
        "search_unavailable_attention",
    ]


def _search_request(
    season_number: int,
    cursor: SubtitleSearchCursorId | None = None,
) -> SubtitleSearchRequest:
    return SubtitleSearchRequest(
        title_aliases=("Correct Anime",),
        season_number=season_number,
        cursor=cursor,
        limit=10,
    )


def test_paged_search_fixture_requires_all_pages_and_seasons() -> None:
    provider = openai_m13_live_smoke._SearchProvider(
        openai_m13_live_smoke._SearchMode.PAGED_CANDIDATES
    )

    season_one_first = openai_m13_live_smoke.asyncio.run(
        provider.search(_search_request(1))
    )
    season_one_second = openai_m13_live_smoke.asyncio.run(
        provider.search(
            _search_request(1, season_one_first.page.next_cursor)
        )
    )
    season_two = openai_m13_live_smoke.asyncio.run(
        provider.search(_search_request(2))
    )

    assert season_one_first.page.complete is False
    assert season_one_first.page.next_cursor == SubtitleSearchCursorId(1)
    assert season_one_second.page.complete is True
    assert season_one_second.page.next_cursor is None
    assert {
        str(item.archive_set_id)
        for release in season_one_second.page.items
        for item in release.archive_sets
    } == {"subarchive:2", "subarchive:3"}
    assert {
        str(item.archive_set_id)
        for release in season_two.page.items
        for item in release.archive_sets
    } == {"subarchive:4", "subarchive:5"}


def test_empty_and_unavailable_search_fixtures_are_distinct() -> None:
    empty = openai_m13_live_smoke._SearchProvider(
        openai_m13_live_smoke._SearchMode.EMPTY
    )
    unavailable = openai_m13_live_smoke._SearchProvider(
        openai_m13_live_smoke._SearchMode.UNAVAILABLE
    )

    empty_result = openai_m13_live_smoke.asyncio.run(
        empty.search(_search_request(1))
    )

    assert empty_result.page.complete is True
    assert empty_result.page.items == ()
    with pytest.raises(SubtitleSearchProviderError):
        openai_m13_live_smoke.asyncio.run(
            unavailable.search(_search_request(1))
        )


@pytest.mark.parametrize(
    ("mode", "probe_status", "chinese_status", "track_count"),
    (
        ("absent", "absent", "absent", 0),
        ("present_chinese", "present", "present", 1),
        ("indeterminate", "indeterminate", "unknown", 0),
    ),
)
def test_probe_fixtures_cover_subtitle_states(
    mode: str,
    probe_status: str,
    chinese_status: str,
    track_count: int,
) -> None:
    inspection = openai_m13_live_smoke.asyncio.run(
        openai_m13_live_smoke._Inspector(
            "snapshot:test",
            1,
            openai_m13_live_smoke._ProbeMode(mode),
        ).inspect(
            CandidateId(CandidateKind.VIDEO, 1),
            season_number=1,
        )
    )

    assert inspection.probe_status.value == probe_status
    assert inspection.chinese_status.value == chinese_status
    assert len(inspection.tracks) == track_count


def test_mapping_scenario_injects_completed_empty_archive_browser() -> None:
    scenario = openai_m13_live_smoke._SCENARIOS_BY_NAME[
        "embedded_chinese_mapping"
    ]
    context = openai_m13_live_smoke._create_scenario_context(scenario, 0)

    assert isinstance(
        context.archive_browser,
        openai_m13_live_smoke._ArchiveBrowser,
    )
    result = openai_m13_live_smoke.asyncio.run(
        context.archive_browser.search(
            work_type=openai_m13_live_smoke.TmdbWorkType.ANIME,
            tmdb_id=200,
            mode="selected_tmdb_id",
            name=None,
            cursor=0,
            limit=50,
        )
    )

    assert result == ((), None, True, "tmdb-200")


def test_mapping_scenario_completes_with_scripted_agent() -> None:
    scenario = openai_m13_live_smoke._SCENARIOS_BY_NAME[
        "embedded_chinese_mapping"
    ]
    context = openai_m13_live_smoke._create_scenario_context(scenario, 0)
    model = ScriptedModel(
        (
            ToolCallStep(
                "list_candidates",
                {"kind": "video", "cursor": 0, "limit": 10},
                "list-video",
            ),
            ToolCallStep(
                "search_tmdb",
                {"query": "Correct Anime", "work_type": "anime"},
                "search-tmdb",
            ),
            ToolCallStep(
                "select_series",
                {"tmdb_id": 200, "work_type": "anime"},
                "select-series",
            ),
            ToolCallStep(
                "get_tmdb_season",
                {
                    "tmdb_id": 200,
                    "work_type": "anime",
                    "season_number": 1,
                    "language": "zh-CN",
                },
                "get-season",
            ),
            ToolCallStep(
                "check_sub_from_video",
                {"video_id": "video:1", "season_number": 1},
                "probe-video",
            ),
            ToolCallStep(
                "search_dir",
                {
                    "mode": "selected_tmdb_id",
                    "name": None,
                    "cursor": 0,
                    "limit": 50,
                },
                "search-archive",
            ),
            ToolCallStep(
                "submit_mapping",
                {
                    "videos": [
                        {
                            "video_id": "video:1",
                            "season": 1,
                            "episode_start": 1,
                            "episode_end": 1,
                        }
                    ],
                    "subtitles": [],
                },
                "submit-mapping",
            ),
        )
    )

    result = openai_m13_live_smoke.asyncio.run(
        run_episode_organizer(
            context=context,
            model=model,
            prompt="Complete the configured Anime workflow.",
            finalize_plan=False,
        )
    )

    assert result.state.phase.value == "build_plan"
    assert result.state.mapping_draft is not None
    assert len(result.state.archive_searches) == 1
    assert result.state.failures == 0
