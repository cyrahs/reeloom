from __future__ import annotations

import json

from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.subtitle_acquisition import (
    CURRENT_SUBTITLE_SEARCH_PARSER_VERSION,
    CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION,
    MAX_SEARCH_RESULTS_PER_PAGE,
    EmbeddedChineseStatus,
    EmbeddedSubtitleInspection,
    SubtitleArchiveSetId,
    SubtitleSearchCursorId,
    SubtitleSearchPage,
    SubtitleSearchRecord,
    SubtitleSelection,
    SubtitleSelectionDecision,
)
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.ports.subtitle_acquisition import (
    SubtitleSearchProvider,
    SubtitleSearchProviderError,
    SubtitleSearchRequest,
    SubtitleSearchResult,
    VideoSubtitleInspector,
)
from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.events import (
    EmbeddedSubtitlesInspected,
    RunStopped,
    SubtitleSearchFailed,
    SubtitleSearchObserved,
    SubtitleSelectionSubmitted,
)
from reeloom.runtime.state import StopReason
from reeloom.runtime.subtitle_workflow import project_subtitle_workflow
from reeloom.runtime.tool_runtime import ToolRuntime
from reeloom.tools.candidates import SnapshotCandidateSource


def _error(code: str, *, retryable: bool) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": {"code": code, "retryable": retryable},
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _reject(
    runtime: ToolRuntime,
    *,
    call_id: str,
    code: str,
    retryable: bool,
    tool_name: str = "check_sub_from_video",
) -> str:
    runtime.reject(
        call_id=call_id,
        tool_name=tool_name,
        code=code,
        retryable=retryable,
    )
    return _error(code, retryable=retryable)


def _observation(inspection: EmbeddedSubtitleInspection) -> str:
    return json.dumps(
        {
            "chinese_status": inspection.chinese_status.value,
            "ok": True,
            "probe_status": inspection.probe_status.value,
            "season_number": inspection.season_number,
            "tracks": [
                {
                    "codec": item.codec.value,
                    "default": item.default,
                    "forced": item.forced,
                    "language": item.language.value,
                    "track_id": str(item.track_id),
                }
                for item in inspection.tracks
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


async def check_sub_from_video(
    runtime: ToolRuntime,
    candidates: SnapshotCandidateSource | None,
    inspector: VideoSubtitleInspector | None,
    *,
    call_id: str,
    video_id: str,
    season_number: int,
) -> str:
    tool_name = "check_sub_from_video"
    try:
        runtime.begin(call_id=call_id, tool_name=tool_name)
    except RuntimeDomainError as error:
        if error.code in {
            RuntimeErrorCode.TOOL_NOT_ALLOWED,
            RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE,
        }:
            return _error(
                error.code.value,
                retryable=(
                    error.code is RuntimeErrorCode.TOOL_NOT_ALLOWED
                ),
            )
        raise
    try:
        candidate_id = CandidateId.parse(video_id)
    except DomainError:
        return _reject(
            runtime,
            call_id=call_id,
            code=RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value,
            retryable=True,
        )
    state = runtime.state
    if (
        candidate_id.kind is not CandidateKind.VIDEO
        or type(season_number) is not int
        or not 0 <= season_number <= 999
    ):
        return _reject(
            runtime,
            call_id=call_id,
            code=RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value,
            retryable=True,
        )
    if (
        state.work_type is not TmdbWorkType.ANIME
        or state.subtitle_acquisition_enabled is not True
        or state.selected_work_type is not TmdbWorkType.ANIME
        or state.selected_series is None
    ):
        return _reject(
            runtime,
            call_id=call_id,
            code=RuntimeErrorCode.WORK_TYPE_NOT_AUTHORIZED.value,
            retryable=False,
        )
    if season_number not in {
        season for season, _count in state.episode_catalog_counts
    }:
        return _reject(
            runtime,
            call_id=call_id,
            code=RuntimeErrorCode.EPISODE_CATALOG_UNAVAILABLE.value,
            retryable=True,
        )
    if (
        candidates is None
        or not isinstance(candidates, SnapshotCandidateSource)
        or candidates.snapshot_id != state.candidate_snapshot_id
        or candidates.candidate_count != state.candidate_count
        or inspector is None
        or inspector.snapshot_id != state.candidate_snapshot_id
        or inspector.candidate_count != state.candidate_count
    ):
        return _reject(
            runtime,
            call_id=call_id,
            code=RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE.value,
            retryable=False,
        )
    snapshot_candidates = candidates.snapshot.candidates
    if any(
        item.kind is CandidateKind.SUBTITLE
        for item in snapshot_candidates
    ):
        return _reject(
            runtime,
            call_id=call_id,
            code=RuntimeErrorCode.EXTERNAL_SUBTITLES_PRESENT.value,
            retryable=False,
        )
    if candidate_id not in {item.id for item in snapshot_candidates}:
        return _reject(
            runtime,
            call_id=call_id,
            code=ErrorCode.UNKNOWN_CANDIDATE_ID.value,
            retryable=True,
        )
    for previous in state.embedded_subtitle_inspections:
        if previous.season_number == season_number:
            if previous.video_id == candidate_id:
                runtime.succeed(call_id=call_id, tool_name=tool_name)
                return _observation(previous)
            return _reject(
                runtime,
                call_id=call_id,
                code=RuntimeErrorCode.SEASON_ALREADY_PROBED.value,
                retryable=False,
            )
        if previous.video_id == candidate_id:
            return _reject(
                runtime,
                call_id=call_id,
                code=RuntimeErrorCode.SEASON_ALREADY_PROBED.value,
                retryable=False,
            )
    try:
        inspection = await inspector.inspect(
            candidate_id,
            season_number=season_number,
        )
        if (
            not isinstance(inspection, EmbeddedSubtitleInspection)
            or inspection.video_id != candidate_id
            or inspection.season_number != season_number
        ):
            raise DomainError(ErrorCode.INVALID_EMBEDDED_SUBTITLE_DATA)
    except DomainError:
        return _reject(
            runtime,
            call_id=call_id,
            code=RuntimeErrorCode.VIDEO_SUBTITLE_PROBE_FAILED.value,
            retryable=False,
        )
    runtime.store.append(
        EmbeddedSubtitlesInspected(
            call_id=call_id,
            inspection=inspection,
        )
    )
    runtime.succeed(call_id=call_id, tool_name=tool_name)
    return _observation(inspection)


def _search_observation(page: SubtitleSearchPage) -> str:
    return json.dumps(
        {
            "items": [
                {
                    "archive_sets": [
                        {
                            "archive_set_id": str(archive.archive_set_id),
                            "declared_size": archive.declared_size,
                            "format": archive.format.value,
                            "label_hint": archive.label_hint,
                            "coverage_hint": archive.coverage_hint,
                            "language_hints": list(archive.language_hints),
                            "release_group_hints": list(
                                archive.release_group_hints
                            ),
                            "volume_count": archive.volume_count,
                            "warnings": list(archive.warnings),
                        }
                        for archive in release.archive_sets
                    ],
                    "coverage_hint": release.coverage_hint,
                    "evidence_complete": release.evidence_complete,
                    "language_hints": list(release.language_hints),
                    "match_reasons": list(release.match_reasons),
                    "post_excerpt": release.post_excerpt,
                    "release_group_hints": list(
                        release.release_group_hints
                    ),
                    "release_id": str(release.release_id),
                    "title": release.title,
                    "warnings": list(release.warnings),
                }
                for release in page.items
            ],
            "next_cursor": (
                None if page.next_cursor is None else str(page.next_cursor)
            ),
            "complete": page.complete,
            "ok": True,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _search_reject(
    runtime: ToolRuntime,
    *,
    call_id: str,
    code: RuntimeErrorCode,
    retryable: bool,
) -> str:
    return _reject(
        runtime,
        call_id=call_id,
        code=code.value,
        retryable=retryable,
        tool_name="search_sub",
    )


def _search_unavailable(
    runtime: ToolRuntime,
    *,
    call_id: str,
    season_number: int,
    code: RuntimeErrorCode,
    retryable: bool,
) -> str:
    runtime.store.append(
        SubtitleSearchFailed(
            call_id=call_id,
            season_number=season_number,
            reason_code="subtitle_search_unavailable",
        )
    )
    return _search_reject(
        runtime,
        call_id=call_id,
        code=code,
        retryable=retryable,
    )


async def search_sub(
    runtime: ToolRuntime,
    provider: SubtitleSearchProvider | None,
    *,
    call_id: str,
    season_number: int,
    cursor: str | None,
) -> str:
    tool_name = "search_sub"
    try:
        runtime.begin(call_id=call_id, tool_name=tool_name)
    except RuntimeDomainError as error:
        if error.code in {
            RuntimeErrorCode.TOOL_NOT_ALLOWED,
            RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE,
        }:
            return _error(error.code.value, retryable=True)
        raise
    state = runtime.state
    if type(season_number) is not int or not 0 <= season_number <= 999:
        return _search_reject(
            runtime,
            call_id=call_id,
            code=RuntimeErrorCode.INVALID_TOOL_ARGUMENTS,
            retryable=True,
        )
    try:
        parsed_cursor = (
            None if cursor is None else SubtitleSearchCursorId.parse(cursor)
        )
    except DomainError:
        return _search_reject(
            runtime,
            call_id=call_id,
            code=RuntimeErrorCode.SUBTITLE_SEARCH_CURSOR_INVALID,
            retryable=True,
        )
    if (
        state.work_type is not TmdbWorkType.ANIME
        or state.subtitle_acquisition_enabled is not True
        or state.selected_work_type is not TmdbWorkType.ANIME
        or state.selected_series is None
        or state.candidate_ids is None
        or any(item.kind is CandidateKind.SUBTITLE for item in state.candidate_ids)
    ):
        return _search_reject(
            runtime,
            call_id=call_id,
            code=RuntimeErrorCode.SUBTITLE_SEARCH_NOT_ALLOWED,
            retryable=False,
        )
    workflow = project_subtitle_workflow(state)
    if (
        not workflow.all_catalog_seasons_inspected
        or workflow.ambiguous_seasons
        or season_number not in workflow.absent_seasons
        or season_number in workflow.failed_search_seasons
    ):
        return _search_reject(
            runtime,
            call_id=call_id,
            code=RuntimeErrorCode.SUBTITLE_SEARCH_NOT_ALLOWED,
            retryable=False,
        )
    inspection = next(
        (
            item
            for item in state.embedded_subtitle_inspections
            if item.season_number == season_number
        ),
        None,
    )
    if (
        inspection is None
        or inspection.chinese_status is not EmbeddedChineseStatus.ABSENT
    ):
        return _search_reject(
            runtime,
            call_id=call_id,
            code=RuntimeErrorCode.SUBTITLE_SEARCH_NOT_ALLOWED,
            retryable=False,
        )
    prior = tuple(
        item
        for item in state.subtitle_search_records
        if item.season_number == season_number
    )
    replay = next((item for item in prior if item.cursor == parsed_cursor), None)
    if replay is not None:
        runtime.succeed(call_id=call_id, tool_name=tool_name)
        return _search_observation(replay.page)
    expected_cursor = None if not prior else prior[-1].page.next_cursor
    if (prior and expected_cursor is None) or parsed_cursor != expected_cursor:
        return _search_reject(
            runtime,
            call_id=call_id,
            code=RuntimeErrorCode.SUBTITLE_SEARCH_CURSOR_INVALID,
            retryable=True,
        )
    expected_version = (
        f"{CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION}+"
        f"{CURRENT_SUBTITLE_SEARCH_PARSER_VERSION}"
    )
    if provider is None or provider.provider_version != expected_version:
        return _search_unavailable(
            runtime,
            call_id=call_id,
            season_number=season_number,
            code=RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE,
            retryable=False,
        )
    try:
        result = await provider.search(
            SubtitleSearchRequest(
                title_aliases=(state.selected_series.title_zh_cn,),
                season_number=season_number,
                cursor=parsed_cursor,
                limit=MAX_SEARCH_RESULTS_PER_PAGE,
            )
        )
        if not isinstance(result, SubtitleSearchResult):
            raise DomainError(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)
    except SubtitleSearchProviderError as error:
        return _search_unavailable(
            runtime,
            call_id=call_id,
            season_number=season_number,
            code=RuntimeErrorCode.SUBTITLE_SEARCH_FAILED,
            retryable=error.retryable,
        )
    except (DomainError, ValueError):
        return _search_unavailable(
            runtime,
            call_id=call_id,
            season_number=season_number,
            code=RuntimeErrorCode.SUBTITLE_SEARCH_FAILED,
            retryable=False,
        )
    runtime.store.append(
        SubtitleSearchObserved(
            call_id=call_id,
            record=SubtitleSearchRecord(
                season_number=season_number,
                cursor=parsed_cursor,
                page=result.page,
            ),
            capabilities=result.capabilities,
        )
    )
    runtime.succeed(call_id=call_id, tool_name=tool_name)
    return _search_observation(result.page)


_ATTENTION_REASONS = frozenset(
    {
        "subtitle_evidence_ambiguous",
        "subtitle_no_candidates",
        "subtitle_search_unavailable",
    }
)


async def select_subtitle_release(
    runtime: ToolRuntime,
    *,
    call_id: str,
    selections: list[dict[str, object]],
    needs_attention_reason: str | None = None,
) -> str:
    tool_name = "select_subtitle_release"
    try:
        runtime.begin(call_id=call_id, tool_name=tool_name)
    except RuntimeDomainError as error:
        if error.code in {
            RuntimeErrorCode.TOOL_NOT_ALLOWED,
            RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE,
        }:
            return _error(error.code.value, retryable=True)
        raise
    if (
        not isinstance(selections, list)
        or any(not isinstance(item, dict) for item in selections)
        or (bool(selections) == bool(needs_attention_reason))
        or (
            needs_attention_reason is not None
            and needs_attention_reason not in _ATTENTION_REASONS
        )
    ):
        return _reject(
            runtime,
            call_id=call_id,
            code=RuntimeErrorCode.SUBTITLE_SELECTION_INVALID.value,
            retryable=True,
            tool_name=tool_name,
        )
    try:
        if needs_attention_reason is not None:
            decision = SubtitleSelectionDecision.needs_attention(
                needs_attention_reason
            )
        else:
            parsed: list[SubtitleSelection] = []
            for item in selections:
                if set(item) != {"season_number", "archive_set_id"}:
                    raise DomainError(ErrorCode.INVALID_SUBTITLE_SELECTION)
                season = item["season_number"]
                if type(season) is not int:
                    raise DomainError(ErrorCode.INVALID_SUBTITLE_SELECTION)
                parsed.append(
                    SubtitleSelection(
                        season,
                        SubtitleArchiveSetId.parse(item["archive_set_id"]),
                    )
                )
            decision = SubtitleSelectionDecision.selected(tuple(parsed))
        runtime.store.append(
            SubtitleSelectionSubmitted(call_id=call_id, decision=decision)
        )
    except (DomainError, RuntimeDomainError, ValueError):
        return _reject(
            runtime,
            call_id=call_id,
            code=RuntimeErrorCode.SUBTITLE_SELECTION_INVALID.value,
            retryable=True,
            tool_name=tool_name,
        )
    runtime.succeed(call_id=call_id, tool_name=tool_name)
    if needs_attention_reason is not None:
        runtime.store.append(RunStopped(StopReason.NEEDS_ATTENTION))
    return json.dumps(
        {
            "ok": True,
            "status": decision.status.value,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
