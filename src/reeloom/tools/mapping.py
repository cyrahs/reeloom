from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime

from reeloom.kernel.archive_directory import (
    ArchiveDirectoryListing,
    ArchiveSearchRecord,
)
from reeloom.kernel.candidates import (
    CandidateId,
    CandidateKind,
)
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.inventory import (
    ExistingInventory,
    parse_episode_filename,
)
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.movie import MovieMappingDraft
from reeloom.kernel.plan_review import normalize_plan_review
from reeloom.kernel.subtitles import (
    MAX_SUBTITLE_SAMPLE_BYTES,
    detect_subtitle_variant as classify_subtitle_variant,
)
from reeloom.ports.archive_directory import (
    ArchiveDirectoryBrowser,
    ArchiveDirectoryError,
)
from reeloom.ports.subtitles import SubtitleSample, SubtitleSampleProvider
from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.events import (
    ArchiveDirectoryListed,
    ArchiveSearchObserved,
    MappingRejected,
    MappingReviewCaptured,
    MappingSubmitted,
    MovieMappingSubmitted,
    SubtitleVariantDetected,
)
from reeloom.runtime.state import MappingValidationIssue, Phase, RunState
from reeloom.runtime.tool_runtime import ToolRuntime
from reeloom.tools.candidates import SnapshotCandidateSource

_MAX_OBSERVATION_BYTES = 64 * 1024
_MAX_CONTEXT_TEXT_BYTES = 160
_SAFE_VALIDATION_CONTEXT_KEYS = frozenset(
    {
        "candidate_id",
        "declared_kind",
        "episode",
        "episode_count",
        "episode_end",
        "episode_start",
        "expected",
        "expected_kind",
        "field",
        "keys",
        "season",
        "subtitle_id",
        "video_id",
        "video_ids",
    }
)


def _serialize(payload: object) -> str | None:
    observation = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(observation.encode("utf-8")) > _MAX_OBSERVATION_BYTES:
        return None
    return observation


def _error(code: str, *, retryable: bool) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": {"code": code, "retryable": retryable},
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _begin(
    runtime: ToolRuntime,
    *,
    call_id: str,
    tool_name: str,
) -> str | None:
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
    return None


def _reject(
    runtime: ToolRuntime,
    *,
    call_id: str,
    tool_name: str,
    code: str,
    retryable: bool,
) -> str:
    runtime.reject(
        call_id=call_id,
        tool_name=tool_name,
        code=code,
        retryable=retryable,
    )
    return _error(code, retryable=retryable)


def _selected_identity(runtime: ToolRuntime) -> tuple[int, object] | None:
    state = runtime.state
    identity = state.selected_movie or state.selected_series
    if (
        identity is None
        or state.phase not in {Phase.MAP_EPISODES, Phase.MAP_MOVIE}
        or state.selected_work_type is not state.work_type
    ):
        return None
    return identity.tmdb_id, identity


def _valid_search_name(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = unicodedata.normalize("NFKC", value).strip()
    return (
        2 <= len(normalized)
        and len(normalized.encode("utf-8")) <= 256
        and not any(
            character in "/\\*?[]\x00"
            or unicodedata.category(character).startswith("C")
            for character in normalized
        )
    )


async def search_dir(
    runtime: ToolRuntime,
    browser: ArchiveDirectoryBrowser | None,
    *,
    call_id: str,
    mode: str,
    name: str | None,
    cursor: int | None,
    limit: int,
) -> str:
    tool_name = "search_dir"
    rejection = _begin(runtime, call_id=call_id, tool_name=tool_name)
    if rejection is not None:
        return rejection
    selected = _selected_identity(runtime)
    if browser is None or selected is None:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE.value,
            retryable=False,
        )
    if (
        mode not in {"selected_tmdb_id", "name"}
        or (
            cursor is not None
            and (type(cursor) is not int or not 0 <= cursor <= 50)
        )
        or type(limit) is not int
        or not 1 <= limit <= 50
        or (
            mode == "selected_tmdb_id"
            and name is not None
        )
        or (
            mode == "name"
            and not _valid_search_name(name)
        )
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value,
            retryable=True,
        )
    tmdb_id, _ = selected
    offset = 0 if cursor is None else cursor
    normalized_name = (
        unicodedata.normalize("NFKC", name).strip()
        if mode == "name" and name is not None
        else None
    )
    query = (
        f"tmdb-{tmdb_id}"
        if mode == "selected_tmdb_id"
        else str(normalized_name)
    )
    previous = next(
        (
            item
            for item in reversed(runtime.state.archive_searches)
            if item.mode == mode
            and item.query == query
            and item.tmdb_id == tmdb_id
            and item.work_type is runtime.state.work_type
        ),
        None,
    )
    if offset != 0 and (
        previous is None or previous.next_cursor != offset
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value,
            retryable=True,
        )
    try:
        capabilities, next_cursor, complete, observed_query = (
            await browser.search(
                work_type=runtime.state.work_type,
                tmdb_id=tmdb_id,
                mode=mode,
                name=normalized_name,
                cursor=offset,
                limit=limit,
            )
        )
    except ArchiveDirectoryError as error:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=error.code,
            retryable=error.retryable,
        )
    record = ArchiveSearchRecord(
        call_id=call_id,
        mode=mode,  # type: ignore[arg-type]
        query=observed_query,
        tmdb_id=tmdb_id,
        work_type=runtime.state.work_type,
        directory_ids=tuple(
            item.directory_id for item in capabilities
        ),
        cursor=offset,
        next_cursor=next_cursor,
        complete=complete,
        observed_at=datetime.now(UTC),
    )
    observation = _serialize(
        {
            "ok": True,
            "matches": [
                {
                    "directory_id": item.directory_id,
                    "name": item.name,
                    "matched_by": mode,
                }
                for item in capabilities
            ],
            "next_cursor": next_cursor,
            "complete": complete,
        }
    )
    if observation is None:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.TOOL_OBSERVATION_TOO_LARGE.value,
            retryable=False,
        )
    runtime.store.append(
        ArchiveSearchObserved(
            search=record,
            capabilities=capabilities,
        )
    )
    runtime.succeed(call_id=call_id, tool_name=tool_name)
    return observation


async def list_dir(
    runtime: ToolRuntime,
    browser: ArchiveDirectoryBrowser | None,
    *,
    call_id: str,
    directory_id: str,
    cursor: int | None,
    limit: int,
) -> str:
    tool_name = "list_dir"
    rejection = _begin(runtime, call_id=call_id, tool_name=tool_name)
    if rejection is not None:
        return rejection
    selected = _selected_identity(runtime)
    if browser is None or selected is None:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE.value,
            retryable=False,
        )
    if (
        not isinstance(directory_id, str)
        or not directory_id
        or len(directory_id.encode("utf-8")) > 128
        or (
            cursor is not None
            and (type(cursor) is not int or not 0 <= cursor <= 2_256)
        )
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value,
            retryable=True,
        )
    capability = next(
        (
            item
            for item in runtime.state.archive_directory_capabilities
            if item.directory_id == directory_id
        ),
        None,
    )
    if capability is None:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code="unknown_directory_id",
            retryable=False,
        )
    offset = 0 if cursor is None else cursor
    previous = next(
        (
            item
            for item in reversed(
                runtime.state.archive_directory_listings
            )
            if item.directory_id == directory_id
        ),
        None,
    )
    if offset != 0 and (
        previous is None or previous.next_cursor != offset
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value,
            retryable=True,
        )
    try:
        children, videos, next_cursor, complete = await browser.list(
            directory_id=directory_id,
            cursor=offset,
            limit=limit,
        )
    except ArchiveDirectoryError as error:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=error.code,
            retryable=error.retryable,
        )
    occupied: set[tuple[int, int]] = set()
    for video in videos:
        occupied.update(parse_episode_filename(video))
    listing = ArchiveDirectoryListing(
        call_id=call_id,
        directory_id=directory_id,
        child_ids=tuple(item.directory_id for item in children),
        videos=videos,
        occupied=tuple(sorted(occupied)),
        cursor=offset,
        next_cursor=next_cursor,
        complete=complete,
        observed_at=datetime.now(UTC),
    )
    observation = _serialize(
        {
            "ok": True,
            "items": [
                {
                    "directory_id": item.directory_id,
                    "kind": "directory",
                    "name": item.name,
                }
                for item in children
            ]
            + [
                {"kind": "video", "name": item}
                for item in videos
            ],
            "next_cursor": next_cursor,
            "complete": complete,
        }
    )
    if observation is None:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.TOOL_OBSERVATION_TOO_LARGE.value,
            retryable=False,
        )
    runtime.store.append(
        ArchiveDirectoryListed(
            listing=listing,
            capabilities=children,
        )
    )
    runtime.succeed(call_id=call_id, tool_name=tool_name)
    return observation


async def detect_subtitle_variant(
    runtime: ToolRuntime,
    candidates: SnapshotCandidateSource | None,
    provider: SubtitleSampleProvider | None,
    *,
    call_id: str,
    subtitle_id: str,
) -> str:
    tool_name = "detect_subtitle_variant"
    rejection = _begin(runtime, call_id=call_id, tool_name=tool_name)
    if rejection is not None:
        return rejection
    try:
        candidate_id = CandidateId.parse(subtitle_id)
    except DomainError:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value,
            retryable=True,
        )
    if candidate_id.kind is not CandidateKind.SUBTITLE:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value,
            retryable=True,
        )
    state = runtime.state
    if (
        candidates is None
        or not isinstance(candidates, SnapshotCandidateSource)
        or candidates.snapshot_id != state.candidate_snapshot_id
        or candidates.candidate_count != state.candidate_count
        or provider is None
        or provider.snapshot_id != state.candidate_snapshot_id
        or provider.candidate_count != state.candidate_count
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE.value,
            retryable=False,
        )
    if candidate_id not in {
        candidate.id for candidate in candidates.snapshot.candidates
    }:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=ErrorCode.UNKNOWN_CANDIDATE_ID.value,
            retryable=True,
        )
    try:
        sample = await provider.sample(
            candidate_id,
            max_bytes=MAX_SUBTITLE_SAMPLE_BYTES,
        )
        if not isinstance(sample, SubtitleSample):
            raise DomainError(ErrorCode.INVALID_SUBTITLE_VARIANT)
        variant = classify_subtitle_variant(
            sample.display_name,
            sample.content,
        )
    except DomainError:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.SUBTITLE_SAMPLE_FAILED.value,
            retryable=False,
        )
    runtime.store.append(
        SubtitleVariantDetected(
            call_id=call_id,
            subtitle_id=candidate_id,
            variant=variant,
        )
    )
    runtime.succeed(call_id=call_id, tool_name=tool_name)
    return json.dumps(
        {
            "ok": True,
            "subtitle_id": str(candidate_id),
            "variant": variant.value,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _bounded_text(value: object) -> str:
    text = str(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= _MAX_CONTEXT_TEXT_BYTES:
        return text
    return encoded[:_MAX_CONTEXT_TEXT_BYTES].decode(
        "utf-8",
        errors="ignore",
    )


def _validation_issue(error: DomainError) -> MappingValidationIssue:
    context: list[tuple[str, int | str | tuple[str, ...]]] = []
    for key, value in sorted(error.context.items()):
        if len(context) >= 8:
            break
        if key not in _SAFE_VALIDATION_CONTEXT_KEYS:
            continue
        if type(value) is int:
            bounded: int | str | tuple[str, ...] = value
        elif isinstance(value, tuple):
            bounded = tuple(_bounded_text(item) for item in value[:8])
        else:
            bounded = _bounded_text(value)
        context.append((_bounded_text(key), bounded))
    return MappingValidationIssue(
        code=error.code.value,
        context=tuple(context),
    )


def _verified_inventory_conflicts(
    state: RunState,
) -> tuple[tuple[CandidateId, int, int], ...]:
    conflicts: list[tuple[CandidateId, int, int]] = []
    for issue in state.mapping_conflicts:
        if issue.code != ErrorCode.INVENTORY_CONFLICT.value:
            continue
        context = dict(issue.context)
        try:
            candidate_id = CandidateId.parse(context["video_id"])
            season = context["season"]
            episode = context["episode"]
            if type(season) is not int or type(episode) is not int:
                continue
        except (DomainError, KeyError, TypeError, ValueError):
            continue
        conflicts.append((candidate_id, season, episode))
    return tuple(conflicts)


def _mapping_rejection(
    runtime: ToolRuntime,
    *,
    call_id: str,
    issue: MappingValidationIssue,
) -> str:
    tool_name = "submit_mapping"
    runtime.store.append(
        MappingRejected(call_id=call_id, issue=issue)
    )
    runtime.reject(
        call_id=call_id,
        tool_name=tool_name,
        code=issue.code,
        retryable=True,
    )
    return json.dumps(
        {
            "ok": False,
            "validation_issues": [
                {
                    "code": issue.code,
                    "context": dict(issue.context),
                }
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


async def submit_mapping(
    runtime: ToolRuntime,
    candidates: SnapshotCandidateSource | None,
    *,
    call_id: str,
    payload: object,
    review: object = None,
) -> str:
    tool_name = "submit_mapping"
    rejection = _begin(runtime, call_id=call_id, tool_name=tool_name)
    if rejection is not None:
        return rejection
    state = runtime.state
    if (
        candidates is None
        or not isinstance(candidates, SnapshotCandidateSource)
        or candidates.snapshot_id != state.candidate_snapshot_id
        or candidates.candidate_count != state.candidate_count
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE.value,
            retryable=False,
        )
    if not state.episode_catalog_counts:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.EPISODE_CATALOG_UNAVAILABLE.value,
            retryable=True,
        )
    if not state.archive_searches:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.ARCHIVE_SEARCH_REQUIRED.value,
            retryable=True,
        )
    if state.selected_series is None:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE.value,
            retryable=False,
        )
    effective_inventory = ExistingInventory.from_episodes(
        work_type=state.work_type,
        tmdb_id=state.selected_series.tmdb_id,
        occupied=state.inventory_episodes or (),
    )

    try:
        mapping = MappingDraft.from_dict(
            payload,
            candidates=candidates.snapshot,
            catalog=EpisodeCatalog(
                season_episode_counts=state.episode_catalog_counts
            ),
        )
        detected = {
            subtitle_id for subtitle_id, _ in state.subtitle_variants
        }
        missing_variant = next(
            (
                subtitle.subtitle_id
                for subtitle in mapping.subtitles
                if subtitle.subtitle_id not in detected
            ),
            None,
        )
        if missing_variant is not None:
            raise DomainError(
                ErrorCode.SUBTITLE_VARIANT_REQUIRED,
                context={"subtitle_id": str(missing_variant)},
            )
        effective_inventory.validate(mapping)
    except DomainError as error:
        return _mapping_rejection(
            runtime,
            call_id=call_id,
            issue=_validation_issue(error),
        )

    mapped_ids = frozenset(
        [item.video_id for item in mapping.videos]
        + [item.subtitle_id for item in mapping.subtitles]
    )
    runtime.store.append(
        MappingReviewCaptured(
            call_id=call_id,
            review=normalize_plan_review(
                review,
                candidate_ids=state.candidate_ids or (),
                mapped_ids=mapped_ids,
                verified_conflicts=_verified_inventory_conflicts(state),
            ),
        )
    )
    runtime.store.append(
        MappingSubmitted(
            call_id=call_id,
            candidate_snapshot_id=candidates.snapshot_id,
            mapping=mapping,
        )
    )
    runtime.succeed(call_id=call_id, tool_name=tool_name)
    return json.dumps(
        {
            "ok": True,
            "phase": Phase.BUILD_PLAN.value,
            "video_count": len(mapping.videos),
            "subtitle_count": len(mapping.subtitles),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


async def submit_movie_mapping(
    runtime: ToolRuntime,
    candidates: SnapshotCandidateSource | None,
    *,
    call_id: str,
    payload: object,
    review: object = None,
) -> str:
    """Validate a complete single-feature Movie mapping."""

    tool_name = "submit_mapping"
    rejection = _begin(runtime, call_id=call_id, tool_name=tool_name)
    if rejection is not None:
        return rejection
    state = runtime.state
    if (
        state.phase is not Phase.MAP_MOVIE
        or state.selected_movie is None
        or candidates is None
        or not isinstance(candidates, SnapshotCandidateSource)
        or candidates.snapshot_id != state.candidate_snapshot_id
        or candidates.candidate_count != state.candidate_count
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE.value,
            retryable=False,
        )
    if not state.archive_searches:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.ARCHIVE_SEARCH_REQUIRED.value,
            retryable=True,
        )
    try:
        mapping = MovieMappingDraft.from_dict(
            payload,
            candidates=candidates.snapshot,
        )
        detected = {
            candidate_id for candidate_id, _ in state.subtitle_variants
        }
        missing = next(
            (
                candidate_id
                for candidate_id in mapping.subtitle_ids
                if candidate_id not in detected
            ),
            None,
        )
        if missing is not None:
            raise DomainError(
                ErrorCode.SUBTITLE_VARIANT_REQUIRED,
                context={"subtitle_id": str(missing)},
            )
    except DomainError as error:
        return _mapping_rejection(
            runtime,
            call_id=call_id,
            issue=_validation_issue(error),
        )
    runtime.store.append(
        MappingReviewCaptured(
            call_id=call_id,
            review=normalize_plan_review(
                review,
                candidate_ids=state.candidate_ids or (),
                mapped_ids=frozenset(
                    (mapping.video_id, *mapping.subtitle_ids)
                ),
                verified_conflicts=_verified_inventory_conflicts(state),
            ),
        )
    )
    runtime.store.append(
        MovieMappingSubmitted(
            call_id=call_id,
            candidate_snapshot_id=candidates.snapshot_id,
            mapping=mapping,
        )
    )
    runtime.succeed(call_id=call_id, tool_name=tool_name)
    return json.dumps(
        {
            "ok": True,
            "phase": Phase.BUILD_PLAN.value,
            "subtitle_count": len(mapping.subtitle_ids),
            "video_count": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
