from __future__ import annotations

import json

from reeloom.kernel.candidates import (
    CandidateId,
    CandidateKind,
)
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.inventory import (
    MAX_INVENTORY_TMDB_ID,
    ExistingInventory,
)
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.movie import MovieMappingDraft
from reeloom.kernel.subtitles import (
    MAX_SUBTITLE_SAMPLE_BYTES,
    detect_subtitle_variant as classify_subtitle_variant,
)
from reeloom.ports.subtitles import SubtitleSample, SubtitleSampleProvider
from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.events import (
    ExistingInventoryObserved,
    MappingRejected,
    MappingSubmitted,
    MovieMappingSubmitted,
    SubtitleVariantDetected,
)
from reeloom.runtime.state import MappingValidationIssue, Phase
from reeloom.runtime.tool_runtime import ToolRuntime
from reeloom.ports.inventory import ExistingInventoryProvider
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


def _selected_matches(runtime: ToolRuntime, tmdb_id: int) -> bool:
    state = runtime.state
    return (
        state.phase is Phase.MAP_EPISODES
        and state.selected_series is not None
        and state.selected_series.tmdb_id == tmdb_id
        and state.selected_work_type is state.work_type
    )


async def get_existing_inventory(
    runtime: ToolRuntime,
    inventory: ExistingInventory | ExistingInventoryProvider | None,
    *,
    call_id: str,
    tmdb_id: int,
) -> str:
    tool_name = "get_existing_inventory"
    rejection = _begin(runtime, call_id=call_id, tool_name=tool_name)
    if rejection is not None:
        return rejection
    if (
        type(tmdb_id) is not int
        or not 1 <= tmdb_id <= MAX_INVENTORY_TMDB_ID
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value,
            retryable=True,
        )
    if not _selected_matches(runtime, tmdb_id):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.UNKNOWN_TMDB_CANDIDATE.value,
            retryable=True,
        )
    state = runtime.state
    if inventory is None:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE.value,
            retryable=False,
        )
    effective = (
        await inventory.get_inventory(
            work_type=state.work_type,
            tmdb_id=tmdb_id,
        )
        if not isinstance(inventory, ExistingInventory)
        else inventory
    )
    if (
        effective.tmdb_id != tmdb_id
        or effective.work_type is not state.work_type
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE.value,
            retryable=False,
        )
    occupied = tuple(
        (location.season, location.episode)
        for location in effective.occupied
    )
    observation = _serialize(
        {
            "ok": True,
            "tmdb_id": tmdb_id,
            "occupied": [
                {"season": season, "episode": episode}
                for season, episode in occupied
            ],
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
        ExistingInventoryObserved(
            call_id=call_id,
            tmdb_id=tmdb_id,
            work_type=state.work_type,
            occupied=occupied,
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
    inventory: ExistingInventory | ExistingInventoryProvider | None,
    *,
    call_id: str,
    payload: object,
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
    if state.inventory_episodes is None:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.INVENTORY_NOT_OBSERVED.value,
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
    if inventory is None:
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE.value,
            retryable=False,
        )
    effective_inventory = (
        await inventory.get_inventory(
            work_type=state.work_type,
            tmdb_id=state.selected_series.tmdb_id,
        )
        if not isinstance(inventory, ExistingInventory)
        else inventory
    )
    observed_inventory = tuple(
        (location.season, location.episode)
        for location in effective_inventory.occupied
    )
    if (
        effective_inventory.work_type is not state.work_type
        or effective_inventory.tmdb_id != state.selected_series.tmdb_id
        or observed_inventory != state.inventory_episodes
    ):
        return _reject(
            runtime,
            call_id=call_id,
            tool_name=tool_name,
            code=RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE.value,
            retryable=False,
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
