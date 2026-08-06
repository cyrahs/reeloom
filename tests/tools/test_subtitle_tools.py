from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from reeloom.kernel.candidates import (
    Candidate,
    CandidateId,
    CandidateKind,
    CandidateSnapshot,
)
from reeloom.kernel.naming import SeriesIdentity
from reeloom.kernel.subtitle_acquisition import (
    CURRENT_SUBTITLE_SEARCH_PARSER_VERSION,
    CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION,
    EmbeddedChineseStatus,
    EmbeddedSubtitleInspection,
    EmbeddedSubtitleProbeStatus,
    SubtitleArchiveFormat,
    SubtitleArchiveSetCapability,
    SubtitleArchiveSetId,
    SubtitleArchiveSetSummary,
    SubtitleReleaseId,
    SubtitleReleaseSummary,
    SubtitleSearchDiagnostics,
    SubtitleSearchFailureCode,
    SubtitleSearchFailureStage,
    SubtitleSearchPage,
    SubtitleSearchCursorId,
)
from reeloom.ports.subtitle_acquisition import (
    SubtitleSearchErrorCode,
    SubtitleSearchProviderError,
    SubtitleSearchRequest,
    SubtitleSearchResult,
)
from reeloom.kernel.tmdb import TmdbCandidateRef, TmdbWorkType
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.events import (
    CandidateSnapshotCreated,
    RunStarted,
    SeriesSelected,
    SubtitleAcquisitionConfigured,
    SubtitleSearchFailed,
    TmdbCandidatesObserved,
    TmdbSeasonCatalogObserved,
)
from reeloom.runtime.state import Phase, RunStatus, StopReason
from reeloom.runtime.policy import PhaseToolPolicy
from reeloom.runtime.store import InMemoryEventStore
from reeloom.runtime.tool_runtime import ToolRuntime
from reeloom.tools.candidates import SnapshotCandidateSource
from reeloom.tools.subtitles import (
    check_sub_from_video,
    search_sub,
    select_subtitle_release,
)


def _candidates(*, with_subtitle: bool = False) -> CandidateSnapshot:
    items = [
        Candidate(
            CandidateId(CandidateKind.VIDEO, number),
            CandidateKind.VIDEO,
            f"Episode {number:02d}.mkv",
        )
        for number in (1, 2)
    ]
    if with_subtitle:
        items.append(
            Candidate(
                CandidateId(CandidateKind.SUBTITLE, 1),
                CandidateKind.SUBTITLE,
                "Episode 01.ass",
            )
        )
    return CandidateSnapshot.create(items)


def _runtime(
    *,
    with_subtitle: bool = False,
    work_type: TmdbWorkType = TmdbWorkType.ANIME,
    title: str = "测试动画",
) -> tuple[ToolRuntime, SnapshotCandidateSource]:
    source = SnapshotCandidateSource(
        _candidates(with_subtitle=with_subtitle)
    )
    store = InMemoryEventStore()
    store.append(RunStarted("run-probe", work_type))
    store.append(
        SubtitleAcquisitionConfigured(
            enabled=work_type is TmdbWorkType.ANIME
        )
    )
    store.append(
        CandidateSnapshotCreated(
            source.snapshot_id,
            source.candidate_count,
            tuple(item.id for item in source.snapshot.candidates),
        )
    )
    store.append(
        TmdbCandidatesObserved((TmdbCandidateRef(work_type, 42),))
    )
    store.append(
        SeriesSelected(
            SeriesIdentity(title, 2026, 42),
            work_type,
        )
    )
    runtime = ToolRuntime(
        store=store,
        budget=RunBudget(max_tool_calls=20, max_failures=10),
        policy=PhaseToolPolicy(),
    )
    runtime.begin(call_id="season-1", tool_name="get_tmdb_season")
    store.append(
        TmdbSeasonCatalogObserved(
            "season-1",
            42,
            work_type,
            1,
            12,
        )
    )
    runtime.succeed(call_id="season-1", tool_name="get_tmdb_season")
    return runtime, source


@dataclass
class _Inspector:
    snapshot_id: str
    candidate_count: int
    probe_status: EmbeddedSubtitleProbeStatus = (
        EmbeddedSubtitleProbeStatus.ABSENT
    )
    calls: int = 0

    async def inspect(
        self,
        video_id: CandidateId,
        *,
        season_number: int,
    ) -> EmbeddedSubtitleInspection:
        self.calls += 1
        return EmbeddedSubtitleInspection(
            video_id,
            season_number,
            self.probe_status,
            (
                EmbeddedChineseStatus.ABSENT
                if self.probe_status is EmbeddedSubtitleProbeStatus.ABSENT
                else EmbeddedChineseStatus.UNKNOWN
            ),
            (),
        )


def _search_result(
    query_aliases: tuple[str, ...] = ("测试动画",),
) -> SubtitleSearchResult:
    archive_id = SubtitleArchiveSetId(1)
    release_id = SubtitleReleaseId(1)
    return SubtitleSearchResult(
        SubtitleSearchPage(
            (
                SubtitleReleaseSummary(
                    release_id,
                    (
                        SubtitleArchiveSetSummary(
                            archive_id,
                            SubtitleArchiveFormat.SEVEN_Z,
                            1,
                            2048,
                        ),
                    ),
                    "测试动画 S01 字幕",
                    "回复楼层中的原生附件证据",
                    "S01 全季",
                    ("简体中文",),
                    ("测试字幕组",),
                    ("规范标题匹配", "季度匹配"),
                    (),
                    True,
                ),
            ),
            None,
            True,
        ),
        (
            SubtitleArchiveSetCapability(
                archive_id,
                release_id,
                SubtitleArchiveFormat.SEVEN_Z,
                10081,
                95257,
                (34768,),
                2048,
            ),
        ),
        SubtitleSearchDiagnostics(
            query_aliases,
            tuple(1 for _alias in query_aliases),
            1,
            1,
            1,
            1,
            1,
            1,
            1,
        ),
    )


@dataclass
class _SearchProvider:
    result: SubtitleSearchResult = _search_result()
    error: SubtitleSearchProviderError | None = None
    calls: int = 0
    requests: tuple[SubtitleSearchRequest, ...] = ()

    @property
    def provider_version(self) -> str:
        return (
            f"{CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION}+"
            f"{CURRENT_SUBTITLE_SEARCH_PARSER_VERSION}"
        )

    async def search(
        self, request: SubtitleSearchRequest
    ) -> SubtitleSearchResult:
        self.calls += 1
        self.requests = (*self.requests, request)
        if self.error is not None:
            raise self.error
        return self.result


class _PagedSearchProvider:
    provider_version = (
        f"{CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION}+"
        f"{CURRENT_SUBTITLE_SEARCH_PARSER_VERSION}"
    )

    async def search(
        self, request: SubtitleSearchRequest
    ) -> SubtitleSearchResult:
        ordinal = 1 if request.cursor is None else 2
        archive_id = SubtitleArchiveSetId(ordinal)
        release_id = SubtitleReleaseId(ordinal)
        next_cursor = (
            SubtitleSearchCursorId(1) if request.cursor is None else None
        )
        return SubtitleSearchResult(
            SubtitleSearchPage(
                (
                    SubtitleReleaseSummary(
                        release_id,
                        (
                            SubtitleArchiveSetSummary(
                                archive_id,
                                SubtitleArchiveFormat.ZIP,
                                1,
                                1000 + ordinal,
                                label_hint=f"candidate-{ordinal}.zip",
                            ),
                        ),
                        f"候选 {ordinal}",
                        "帖子证据",
                        "S01",
                        ("zh-hans",),
                        (),
                        ("title_alias",),
                        (),
                        True,
                    ),
                ),
                next_cursor,
                next_cursor is None,
            ),
            (
                SubtitleArchiveSetCapability(
                    archive_id,
                    release_id,
                    SubtitleArchiveFormat.ZIP,
                    10_000 + ordinal,
                    20_000 + ordinal,
                    (30_000 + ordinal,),
                    1000 + ordinal,
                ),
            ),
            SubtitleSearchDiagnostics(
                request.title_aliases,
                (2,),
                2,
                2,
                2,
                2,
                2,
                2,
                2,
            ),
        )


def _probe_absent(
    runtime: ToolRuntime,
    source: SnapshotCandidateSource,
    *,
    season_number: int = 1,
    video_id: str = "video:1",
    call_id: str = "probe-before-search",
) -> None:
    asyncio.run(
        check_sub_from_video(
            runtime,
            source,
            _Inspector(source.snapshot_id, source.candidate_count),
            call_id=call_id,
            video_id=video_id,
            season_number=season_number,
        )
    )


def test_check_sub_from_video_records_one_snapshot_bound_sample() -> None:
    runtime, source = _runtime()
    inspector = _Inspector(source.snapshot_id, source.candidate_count)

    payload = json.loads(
        asyncio.run(
            check_sub_from_video(
                runtime,
                source,
                inspector,
                call_id="probe-1",
                video_id="video:1",
                season_number=1,
            )
        )
    )

    assert payload == {
        "chinese_status": "absent",
        "ok": True,
        "probe_status": "absent",
        "season_number": 1,
        "tracks": [],
    }
    assert runtime.state.embedded_subtitle_inspections == (
        EmbeddedSubtitleInspection(
            CandidateId(CandidateKind.VIDEO, 1),
            1,
            EmbeddedSubtitleProbeStatus.ABSENT,
            EmbeddedChineseStatus.ABSENT,
            (),
        ),
    )


def test_check_sub_from_video_exact_argument_replay_uses_cache() -> None:
    runtime, source = _runtime()
    inspector = _Inspector(source.snapshot_id, source.candidate_count)

    first = asyncio.run(
        check_sub_from_video(
            runtime,
            source,
            inspector,
            call_id="probe-1",
            video_id="video:1",
            season_number=1,
        )
    )
    replay = asyncio.run(
        check_sub_from_video(
            runtime,
            source,
            inspector,
            call_id="probe-replay",
            video_id="video:1",
            season_number=1,
        )
    )

    assert replay == first
    assert inspector.calls == 1


def test_check_sub_from_video_rejects_second_video_for_same_season() -> None:
    runtime, source = _runtime()
    inspector = _Inspector(source.snapshot_id, source.candidate_count)
    asyncio.run(
        check_sub_from_video(
            runtime,
            source,
            inspector,
            call_id="probe-1",
            video_id="video:1",
            season_number=1,
        )
    )

    rejected = json.loads(
        asyncio.run(
            check_sub_from_video(
                runtime,
                source,
                inspector,
                call_id="probe-2",
                video_id="video:2",
                season_number=1,
            )
        )
    )

    assert rejected == {
        "error": {"code": "season_already_probed", "retryable": False},
        "ok": False,
    }
    assert inspector.calls == 1


def test_check_sub_from_video_rejects_external_subtitle_snapshot() -> None:
    runtime, source = _runtime(with_subtitle=True)
    inspector = _Inspector(source.snapshot_id, source.candidate_count)

    rejected = json.loads(
        asyncio.run(
            check_sub_from_video(
                runtime,
                source,
                inspector,
                call_id="probe-1",
                video_id="video:1",
                season_number=1,
            )
        )
    )

    assert rejected["error"] == {
        "code": "external_subtitles_present",
        "retryable": False,
    }
    assert inspector.calls == 0


def test_check_sub_from_video_rejects_capability_and_unknown_season() -> None:
    runtime, source = _runtime()
    mismatched = _Inspector("other-snapshot", source.candidate_count)

    capability = json.loads(
        asyncio.run(
            check_sub_from_video(
                runtime,
                source,
                mismatched,
                call_id="probe-capability",
                video_id="video:1",
                season_number=1,
            )
        )
    )
    unknown_season = json.loads(
        asyncio.run(
            check_sub_from_video(
                runtime,
                source,
                _Inspector(source.snapshot_id, source.candidate_count),
                call_id="probe-season",
                video_id="video:1",
                season_number=2,
            )
        )
    )

    assert capability["error"]["code"] == "capability_not_available"
    assert unknown_season["error"]["code"] == "episode_catalog_unavailable"


def test_check_sub_from_video_is_anime_only() -> None:
    runtime, source = _runtime(work_type=TmdbWorkType.TV_SERIES)
    inspector = _Inspector(source.snapshot_id, source.candidate_count)

    rejected = json.loads(
        asyncio.run(
            check_sub_from_video(
                runtime,
                source,
                inspector,
                call_id="probe-tv",
                video_id="video:1",
                season_number=1,
            )
        )
    )

    assert rejected["error"]["code"] == "work_type_not_authorized"
    assert inspector.calls == 0


def test_search_sub_records_only_bounded_observation_and_stable_capability() -> None:
    runtime, source = _runtime()
    _probe_absent(runtime, source)
    provider = _SearchProvider()

    payload = json.loads(
        asyncio.run(
            search_sub(
                runtime,
                provider,
                call_id="search-sub-1",
                season_number=1,
                cursor=None,
            )
        )
    )

    assert payload["items"][0]["archive_sets"] == [
        {
            "archive_set_id": "subarchive:1",
            "declared_size": 2048,
            "format": "7z",
            "label_hint": "",
            "coverage_hint": "",
            "language_hints": [],
            "release_group_hints": [],
            "volume_count": 1,
            "warnings": [],
        }
    ]
    assert payload["complete"] is True
    assert "http" not in json.dumps(payload).lower()
    assert provider.requests[0].title_aliases == ("测试动画",)
    assert provider.requests[0].season_number == 1
    assert runtime.state.phase is Phase.MAP_EPISODES
    assert runtime.state.subtitle_archive_capabilities[0].thread_id == 10081
    assert runtime.state.subtitle_selection_decision is None


def test_search_sub_compiles_internal_punctuation_as_discuz_and_terms() -> None:
    runtime, source = _runtime(title="空之色，水之色")
    _probe_absent(runtime, source)
    expected_aliases = ("空之色,水之色", "空之色 水之色")
    provider = _SearchProvider(result=_search_result(expected_aliases))

    payload = json.loads(
        asyncio.run(
            search_sub(
                runtime,
                provider,
                call_id="search-punctuation",
                season_number=1,
                cursor=None,
            )
        )
    )

    assert payload["ok"] is True
    assert provider.requests[0].title_aliases == expected_aliases


def test_search_sub_exact_replay_uses_cached_page() -> None:
    runtime, source = _runtime()
    _probe_absent(runtime, source)
    provider = _SearchProvider()

    first = asyncio.run(
        search_sub(
            runtime,
            provider,
            call_id="search-sub-1",
            season_number=1,
            cursor=None,
        )
    )
    replay = asyncio.run(
        search_sub(
            runtime,
            provider,
            call_id="search-sub-replay",
            season_number=1,
            cursor=None,
        )
    )

    assert replay == first
    assert provider.calls == 1


def test_search_sub_requires_definitive_absent_chinese_sample() -> None:
    runtime, _source = _runtime()
    provider = _SearchProvider()

    rejected = json.loads(
        asyncio.run(
            search_sub(
                runtime,
                provider,
                call_id="search-without-probe",
                season_number=1,
                cursor=None,
            )
        )
    )

    assert rejected["error"] == {
        "code": "subtitle_search_not_allowed",
        "retryable": False,
    }
    assert provider.calls == 0


def test_search_sub_fail_closed_on_provider_challenge() -> None:
    runtime, source = _runtime()
    _probe_absent(runtime, source)
    provider = _SearchProvider(
        error=SubtitleSearchProviderError(
            SubtitleSearchErrorCode.CHALLENGE_OR_LOGIN,
            retryable=False,
            stage=SubtitleSearchFailureStage.FORUM_SEARCH,
            query_alias_index=0,
            http_response_count=3,
            received_html_bytes=12_345,
            http_status=200,
        )
    )

    rejected = json.loads(
        asyncio.run(
            search_sub(
                runtime,
                provider,
                call_id="search-challenge",
                season_number=1,
                cursor=None,
            )
        )
    )

    assert rejected["error"] == {
        "code": "subtitle_search_failed",
        "retryable": False,
    }
    failure = next(
        stored.event
        for stored in runtime.store.events
        if isinstance(stored.event, SubtitleSearchFailed)
    )
    assert failure.diagnostics is not None
    assert failure.diagnostics.error_code is (
        SubtitleSearchFailureCode.CHALLENGE_OR_LOGIN
    )
    assert failure.diagnostics.stage is SubtitleSearchFailureStage.FORUM_SEARCH
    assert failure.diagnostics.query_aliases == ("测试动画",)
    assert failure.diagnostics.query_alias_index == 0
    assert failure.diagnostics.http_response_count == 3
    assert failure.diagnostics.received_html_bytes == 12_345
    assert failure.diagnostics.http_status == 200


def test_single_search_result_still_requires_explicit_selection() -> None:
    runtime, source = _runtime()
    _probe_absent(runtime, source)
    asyncio.run(
        search_sub(
            runtime,
            _SearchProvider(),
            call_id="search-one",
            season_number=1,
            cursor=None,
        )
    )

    assert runtime.state.phase is Phase.MAP_EPISODES
    assert runtime.state.subtitle_selection_decision is None

    selected = json.loads(
        asyncio.run(
            select_subtitle_release(
                runtime,
                call_id="select-one",
                selections=[
                    {
                        "season_number": 1,
                        "archive_set_id": "subarchive:1",
                    }
                ],
            )
        )
    )

    assert selected == {"ok": True, "status": "selected"}
    assert runtime.state.phase is Phase.BUILD_SUBTITLE_ACQUISITION_PLAN
    assert runtime.state.status is RunStatus.RUNNING


def test_select_subtitle_release_rejects_unsearched_archive() -> None:
    runtime, source = _runtime()
    _probe_absent(runtime, source)

    rejected = json.loads(
        asyncio.run(
            select_subtitle_release(
                runtime,
                call_id="select-unknown",
                selections=[
                    {
                        "season_number": 1,
                        "archive_set_id": "subarchive:99",
                    }
                ],
            )
        )
    )

    assert rejected["error"]["code"] == "subtitle_selection_invalid"
    assert runtime.state.phase is Phase.MAP_EPISODES


def test_select_subtitle_release_can_submit_structured_needs_attention() -> None:
    runtime, source = _runtime()
    _probe_absent(runtime, source)
    asyncio.run(
        search_sub(
            runtime,
            _SearchProvider(
                error=SubtitleSearchProviderError(
                    SubtitleSearchErrorCode.CHALLENGE_OR_LOGIN,
                    retryable=False,
                )
            ),
            call_id="search-unavailable",
            season_number=1,
            cursor=None,
        )
    )

    result = json.loads(
        asyncio.run(
            select_subtitle_release(
                runtime,
                call_id="select-attention",
                selections=[],
                needs_attention_reason="subtitle_search_unavailable",
            )
        )
    )

    assert result == {"ok": True, "status": "needs_attention"}
    assert runtime.state.status is RunStatus.STOPPED
    assert runtime.state.stop_reason is StopReason.NEEDS_ATTENTION


def test_selection_is_rejected_until_search_pagination_is_complete() -> None:
    runtime, source = _runtime()
    _probe_absent(runtime, source)
    provider = _PagedSearchProvider()
    first = json.loads(
        asyncio.run(
            search_sub(
                runtime,
                provider,
                call_id="search-page-1",
                season_number=1,
                cursor=None,
            )
        )
    )
    assert first["complete"] is False
    assert first["next_cursor"] == "subcursor:1"

    premature = json.loads(
        asyncio.run(
            select_subtitle_release(
                runtime,
                call_id="select-before-page-2",
                selections=[
                    {
                        "season_number": 1,
                        "archive_set_id": "subarchive:1",
                    }
                ],
            )
        )
    )
    assert premature["error"]["code"] == "subtitle_selection_invalid"

    second = json.loads(
        asyncio.run(
            search_sub(
                runtime,
                provider,
                call_id="search-page-2",
                season_number=1,
                cursor="subcursor:1",
            )
        )
    )
    assert second["complete"] is True
    accepted = json.loads(
        asyncio.run(
            select_subtitle_release(
                runtime,
                call_id="select-after-page-2",
                selections=[
                    {
                        "season_number": 1,
                        "archive_set_id": "subarchive:2",
                    }
                ],
            )
        )
    )
    assert accepted == {"ok": True, "status": "selected"}


def test_multi_season_search_and_selection_require_complete_evidence() -> None:
    runtime, source = _runtime()
    runtime.begin(call_id="season-2", tool_name="get_tmdb_season")
    runtime.store.append(
        TmdbSeasonCatalogObserved(
            "season-2",
            42,
            TmdbWorkType.ANIME,
            2,
            12,
        )
    )
    runtime.succeed(call_id="season-2", tool_name="get_tmdb_season")
    _probe_absent(runtime, source)
    provider = _SearchProvider()

    premature = json.loads(
        asyncio.run(
            search_sub(
                runtime,
                provider,
                call_id="search-before-all-probes",
                season_number=1,
                cursor=None,
            )
        )
    )
    assert premature["error"]["code"] == "subtitle_search_not_allowed"
    assert provider.calls == 0

    _probe_absent(
        runtime,
        source,
        season_number=2,
        video_id="video:2",
        call_id="probe-season-2",
    )
    for season_number in (1, 2):
        asyncio.run(
            search_sub(
                runtime,
                provider,
                call_id=f"search-season-{season_number}",
                season_number=season_number,
                cursor=None,
            )
        )

    incomplete = json.loads(
        asyncio.run(
            select_subtitle_release(
                runtime,
                call_id="select-only-season-1",
                selections=[
                    {
                        "season_number": 1,
                        "archive_set_id": "subarchive:1",
                    }
                ],
            )
        )
    )
    assert incomplete["error"]["code"] == "subtitle_selection_invalid"

    accepted = json.loads(
        asyncio.run(
            select_subtitle_release(
                runtime,
                call_id="select-both-seasons",
                selections=[
                    {
                        "season_number": season_number,
                        "archive_set_id": "subarchive:1",
                    }
                    for season_number in (1, 2)
                ],
            )
        )
    )
    assert accepted == {"ok": True, "status": "selected"}
