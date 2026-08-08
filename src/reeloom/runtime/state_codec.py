from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import PurePosixPath

from reeloom.kernel.archive_directory import (
    ArchiveDirectoryCapability,
    ArchiveDirectoryListing,
    ArchiveSearchRecord,
)
from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.initial_plan import InitialPlan
from reeloom.kernel.plan_review import PlanReview
from reeloom.kernel.naming import (
    MovieIdentity,
    SeriesIdentity,
    SubtitleVariant,
)
from reeloom.kernel.schema import check_fields, require_object
from reeloom.kernel.subtitle_acquisition import SubtitleArchiveSetId
from reeloom.kernel.tmdb import TmdbWorkType, validate_tmdb_poster_path
from reeloom.runtime.event_codec import (
    _budget,
    _budget_payload,
    _candidate_refs,
    _candidate_refs_payload,
    _embedded_inspection,
    _embedded_inspection_payload,
    _subtitle_capability,
    _subtitle_capability_payload,
    _subtitle_search_record,
    _subtitle_search_record_payload,
    _subtitle_selection,
    _subtitle_selection_payload,
    _issue,
    _issue_payload,
    _mapping,
    _mapping_payload,
    _movie_mapping,
    _movie_mapping_payload,
    _movie_payload,
    _parse_timestamp,
    _root,
    _root_payload,
    _series_payload,
    _timestamp,
)
from reeloom.runtime.state import (
    Phase,
    RunState,
    RunStatus,
    StopReason,
)

LEGACY_STATE_PROJECTION_SCHEMA = "runtime-state-v1"
V2_STATE_PROJECTION_SCHEMA = "runtime-state-v2"
V3_STATE_PROJECTION_SCHEMA = "runtime-state-v3"
V4_STATE_PROJECTION_SCHEMA = "runtime-state-v4"
V5_STATE_PROJECTION_SCHEMA = "runtime-state-v5"
V6_STATE_PROJECTION_SCHEMA = "runtime-state-v6"
V7_STATE_PROJECTION_SCHEMA = "runtime-state-v7"
V8_STATE_PROJECTION_SCHEMA = "runtime-state-v8"
STATE_PROJECTION_SCHEMA = "runtime-state-v9"
_LEGACY_FIELDS = frozenset(
    {
        "applied_count",
        "applied_source_ids",
        "approval_id",
        "authorized_output_root",
        "authorized_source_root",
        "budget",
        "candidate_count",
        "candidate_ids",
        "candidate_snapshot_id",
        "deadline_at",
        "episode_catalog_counts",
        "event_count",
        "failure_code",
        "failures",
        "inventory_episodes",
        "mapping_draft",
        "model_tokens",
        "model_turns",
        "observed_tool_calls",
        "pending_tool_calls",
        "phase",
        "plan_hash",
        "rename_plan_hash",
        "rolled_back_count",
        "run_id",
        "selected_series",
        "selected_work_type",
        "status",
        "stop_reason",
        "subtitle_variants",
        "tmdb_candidates",
        "tool_calls",
        "transaction_id",
        "validation_issues",
        "work_type",
    }
)
_V2_FIELDS = _LEGACY_FIELDS | {
    "movie_mapping_draft",
    "selected_movie",
}
_V3_FIELDS = _V2_FIELDS | {
    "archive_directory_capabilities",
    "archive_directory_listings",
    "archive_searches",
}
_V3_CURRENT_FIELDS = _V3_FIELDS | {"retryable_directory_failure"}
_V4_FIELDS = _V3_CURRENT_FIELDS | {
    "mapping_conflicts",
    "mapping_review",
    "mapping_review_call_id",
}
_V5_FIELDS = _V4_FIELDS | {"selected_poster_path"}
_V6_FIELDS = _V5_FIELDS | {"embedded_subtitle_inspections"}
_V7_FIELDS = _V6_FIELDS | {
    "subtitle_archive_capabilities",
    "subtitle_archive_search_bindings",
    "subtitle_search_records",
    "subtitle_selection_decision",
}
_FIELDS = _V7_FIELDS | {
    "subtitle_acquisition_enabled",
    "subtitle_search_failures",
}


def is_supported_projection_schema(value: object) -> bool:
    return value in {
        LEGACY_STATE_PROJECTION_SCHEMA,
        V2_STATE_PROJECTION_SCHEMA,
        V3_STATE_PROJECTION_SCHEMA,
        V4_STATE_PROJECTION_SCHEMA,
        V5_STATE_PROJECTION_SCHEMA,
        V6_STATE_PROJECTION_SCHEMA,
        V7_STATE_PROJECTION_SCHEMA,
        V8_STATE_PROJECTION_SCHEMA,
        STATE_PROJECTION_SCHEMA,
    }


def _pairs(values: frozenset[tuple[str, str]]) -> list[list[str]]:
    return [list(item) for item in sorted(values)]


def encode_state(state: RunState) -> dict[str, object]:
    return {
        "applied_count": state.applied_count,
        "applied_source_ids": [
            str(item) for item in state.applied_source_ids
        ],
        "approval_id": state.approval_id,
        "authorized_output_root": (
            None
            if state.authorized_output_root is None
            else _root_payload(state.authorized_output_root)
        ),
        "authorized_source_root": (
            None
            if state.authorized_source_root is None
            else _root_payload(state.authorized_source_root)
        ),
        "budget": _budget_payload(state.budget),
        "candidate_count": state.candidate_count,
        "candidate_ids": (
            None
            if state.candidate_ids is None
            else [str(item) for item in state.candidate_ids]
        ),
        "candidate_snapshot_id": state.candidate_snapshot_id,
        "subtitle_acquisition_enabled": state.subtitle_acquisition_enabled,
        "deadline_at": _timestamp(state.deadline_at),
        "episode_catalog_counts": [
            list(item) for item in state.episode_catalog_counts
        ],
        "embedded_subtitle_inspections": [
            _embedded_inspection_payload(item)
            for item in state.embedded_subtitle_inspections
        ],
        "subtitle_search_records": [
            _subtitle_search_record_payload(item)
            for item in state.subtitle_search_records
        ],
        "subtitle_search_failures": [
            [season_number, reason_code]
            for season_number, reason_code in state.subtitle_search_failures
        ],
        "subtitle_archive_capabilities": [
            _subtitle_capability_payload(item)
            for item in state.subtitle_archive_capabilities
        ],
        "subtitle_archive_search_bindings": [
            [season_number, str(archive_set_id)]
            for season_number, archive_set_id
            in state.subtitle_archive_search_bindings
        ],
        "subtitle_selection_decision": (
            None
            if state.subtitle_selection_decision is None
            else _subtitle_selection_payload(
                state.subtitle_selection_decision
            )
        ),
        "event_count": state.event_count,
        "failure_code": state.failure_code,
        "failures": state.failures,
        "inventory_episodes": (
            None
            if state.inventory_episodes is None
            else [list(item) for item in state.inventory_episodes]
        ),
        "archive_directory_capabilities": [
            _archive_capability_payload(item)
            for item in state.archive_directory_capabilities
        ],
        "archive_searches": [
            _archive_search_payload(item)
            for item in state.archive_searches
        ],
        "archive_directory_listings": [
            _archive_listing_payload(item)
            for item in state.archive_directory_listings
        ],
        "retryable_directory_failure": state.retryable_directory_failure,
        "mapping_draft": (
            None
            if state.mapping_draft is None
            else _mapping_payload(state.mapping_draft)
        ),
        "movie_mapping_draft": (
            None
            if state.movie_mapping_draft is None
            else _movie_mapping_payload(state.movie_mapping_draft)
        ),
        "mapping_review": (
            None
            if state.mapping_review is None
            else state.mapping_review.to_dict()
        ),
        "mapping_review_call_id": state.mapping_review_call_id,
        "mapping_conflicts": [
            _issue_payload(item) for item in state.mapping_conflicts
        ],
        "model_tokens": state.model_tokens,
        "model_turns": state.model_turns,
        "observed_tool_calls": _pairs(state.observed_tool_calls),
        "pending_tool_calls": _pairs(state.pending_tool_calls),
        "phase": state.phase.value,
        "plan_hash": state.plan_hash,
        "rename_plan_hash": (
            None
            if state.rename_plan is None
            else state.rename_plan.plan_hash
        ),
        "rolled_back_count": state.rolled_back_count,
        "run_id": state.run_id,
        "selected_series": (
            None
            if state.selected_series is None
            else _series_payload(state.selected_series)
        ),
        "selected_movie": (
            None
            if state.selected_movie is None
            else _movie_payload(state.selected_movie)
        ),
        "selected_work_type": (
            None
            if state.selected_work_type is None
            else state.selected_work_type.value
        ),
        "selected_poster_path": state.selected_poster_path,
        "status": state.status.value,
        "stop_reason": (
            None if state.stop_reason is None else state.stop_reason.value
        ),
        "subtitle_variants": [
            [str(candidate_id), variant.value]
            for candidate_id, variant in state.subtitle_variants
        ],
        "tmdb_candidates": _candidate_refs_payload(
            tuple(
                sorted(
                    state.tmdb_candidates,
                    key=lambda item: (item.work_type.value, item.tmdb_id),
                )
            )
        ),
        "tool_calls": state.tool_calls,
        "transaction_id": state.transaction_id,
        "validation_issues": [
            _issue_payload(item) for item in state.validation_issues
        ],
        "work_type": state.work_type.value,
    }


def canonical_state(state: RunState) -> str:
    return json.dumps(
        encode_state(state),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def patch_state(
    value: object,
    *,
    schema_version: str | None = None,
    **changes: object,
) -> str:
    payload = _normalized_payload(
        value,
        schema_version=schema_version,
    )
    if not set(changes) <= _FIELDS:
        raise ValueError
    decode_state(
        payload,
        load_plan=lambda _plan_hash: None,  # type: ignore[arg-type]
    )
    updated = dict(payload)
    updated.update(changes)
    return json.dumps(
        updated,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError
    return value


def _optional_text(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError
    return value


def _bool(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError
    return value


def _tuple_pairs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise ValueError
    pairs: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise ValueError
        pairs.append((item[0], item[1]))
    if len(set(pairs)) != len(pairs):
        raise ValueError
    return tuple(pairs)


def _int_pairs(value: object) -> tuple[tuple[int, int], ...]:
    if not isinstance(value, list):
        raise ValueError
    pairs = tuple(
        (_int(item[0]), _int(item[1]))
        for item in value
        if isinstance(item, list) and len(item) == 2
    )
    if len(pairs) != len(value):
        raise ValueError
    return pairs


def _int_text_pairs(value: object) -> tuple[tuple[int, str], ...]:
    if not isinstance(value, list):
        raise ValueError
    pairs: list[tuple[int, str]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError
        pairs.append((_int(item[0]), _text(item[1])))
    if len(set(pairs)) != len(pairs):
        raise ValueError
    return tuple(pairs)


def _subtitle_bindings(
    value: object,
) -> tuple[tuple[int, SubtitleArchiveSetId], ...]:
    if not isinstance(value, list):
        raise ValueError
    bindings: list[tuple[int, SubtitleArchiveSetId]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError
        bindings.append(
            (_int(item[0]), SubtitleArchiveSetId.parse(item[1]))
        )
    if len(set(bindings)) != len(bindings):
        raise ValueError
    return tuple(bindings)


def _archive_capability_payload(
    value: ArchiveDirectoryCapability,
) -> dict[str, object]:
    return {
        "ctime_ns": value.ctime_ns,
        "depth": value.depth,
        "device": value.device,
        "directory_id": value.directory_id,
        "inode": value.inode,
        "mtime_ns": value.mtime_ns,
        "name": value.name,
        "parent_id": value.parent_id,
        "relative_path": value.relative_path.as_posix(),
        "run_id": value.run_id,
    }


def _archive_capability(value: object) -> ArchiveDirectoryCapability:
    raw = check_fields(
        value,
        frozenset(
            {
                "ctime_ns",
                "depth",
                "device",
                "directory_id",
                "inode",
                "mtime_ns",
                "name",
                "parent_id",
                "relative_path",
                "run_id",
            }
        ),
        field="archive_directory_capability",
    )
    return ArchiveDirectoryCapability(
        run_id=_text(raw["run_id"]),
        directory_id=_text(raw["directory_id"]),
        parent_id=_optional_text(raw["parent_id"]),
        relative_path=PurePosixPath(_text(raw["relative_path"])),
        name=_text(raw["name"]),
        depth=_int(raw["depth"]),
        device=_int(raw["device"]),
        inode=_int(raw["inode"]),
        mtime_ns=_int(raw["mtime_ns"]),
        ctime_ns=_int(raw["ctime_ns"]),
    )


def _archive_search_payload(
    value: ArchiveSearchRecord,
) -> dict[str, object]:
    return {
        "call_id": value.call_id,
        "complete": value.complete,
        "cursor": value.cursor,
        "directory_ids": list(value.directory_ids),
        "mode": value.mode,
        "next_cursor": value.next_cursor,
        "observed_at": _timestamp(value.observed_at),
        "query": value.query,
        "tmdb_id": value.tmdb_id,
        "work_type": value.work_type.value,
    }


def _archive_search(value: object) -> ArchiveSearchRecord:
    raw = check_fields(
        value,
        frozenset(
            {
                "call_id",
                "complete",
                "cursor",
                "directory_ids",
                "mode",
                "next_cursor",
                "observed_at",
                "query",
                "tmdb_id",
                "work_type",
            }
        ),
        field="archive_search",
    )
    directory_ids = raw["directory_ids"]
    if (
        not isinstance(directory_ids, list)
        or any(not isinstance(item, str) for item in directory_ids)
        or type(raw["complete"]) is not bool
    ):
        raise ValueError
    return ArchiveSearchRecord(
        call_id=_text(raw["call_id"]),
        mode=_text(raw["mode"]),  # type: ignore[arg-type]
        query=_text(raw["query"]),
        tmdb_id=_int(raw["tmdb_id"]),
        work_type=TmdbWorkType(raw["work_type"]),
        directory_ids=tuple(directory_ids),
        cursor=_int(raw["cursor"]),
        next_cursor=(
            None
            if raw["next_cursor"] is None
            else _int(raw["next_cursor"])
        ),
        complete=raw["complete"],
        observed_at=_parse_timestamp(raw["observed_at"]),
    )


def _archive_listing_payload(
    value: ArchiveDirectoryListing,
) -> dict[str, object]:
    return {
        "call_id": value.call_id,
        "child_ids": list(value.child_ids),
        "complete": value.complete,
        "cursor": value.cursor,
        "directory_id": value.directory_id,
        "next_cursor": value.next_cursor,
        "observed_at": _timestamp(value.observed_at),
        "occupied": [list(item) for item in value.occupied],
        "videos": list(value.videos),
    }


def _archive_listing(value: object) -> ArchiveDirectoryListing:
    raw = check_fields(
        value,
        frozenset(
            {
                "call_id",
                "child_ids",
                "complete",
                "cursor",
                "directory_id",
                "next_cursor",
                "observed_at",
                "occupied",
                "videos",
            }
        ),
        field="archive_directory_listing",
    )
    child_ids = raw["child_ids"]
    videos = raw["videos"]
    if (
        not isinstance(child_ids, list)
        or any(not isinstance(item, str) for item in child_ids)
        or not isinstance(videos, list)
        or any(not isinstance(item, str) for item in videos)
        or type(raw["complete"]) is not bool
    ):
        raise ValueError
    return ArchiveDirectoryListing(
        call_id=_text(raw["call_id"]),
        directory_id=_text(raw["directory_id"]),
        child_ids=tuple(child_ids),
        videos=tuple(videos),
        occupied=_int_pairs(raw["occupied"]),
        cursor=_int(raw["cursor"]),
        next_cursor=(
            None
            if raw["next_cursor"] is None
            else _int(raw["next_cursor"])
        ),
        complete=raw["complete"],
        observed_at=_parse_timestamp(raw["observed_at"]),
    )


def decode_state(
    value: object,
    *,
    load_plan: Callable[[str], InitialPlan],
    schema_version: str | None = None,
) -> RunState:
    payload = _normalized_payload(
        value,
        schema_version=schema_version,
    )
    series = payload["selected_series"]
    if series is None:
        selected_series = None
    else:
        raw = check_fields(
            series,
            frozenset({"title_zh_cn", "tmdb_id", "year"}),
            field="selected_series",
        )
        selected_series = SeriesIdentity(
            title_zh_cn=raw["title_zh_cn"],
            year=raw["year"],
            tmdb_id=raw["tmdb_id"],
        )
    movie = payload["selected_movie"]
    selected_movie = (
        None
        if movie is None
        else MovieIdentity.from_dict(movie)
    )
    candidate_ids = payload["candidate_ids"]
    rename_plan_hash = payload["rename_plan_hash"]
    subtitle_variants = payload["subtitle_variants"]
    if not isinstance(subtitle_variants, list):
        raise ValueError
    variants = tuple(
        (
            CandidateId.parse(item[0]),
            SubtitleVariant(item[1]),
        )
        for item in subtitle_variants
        if isinstance(item, list) and len(item) == 2
    )
    if len(variants) != len(subtitle_variants):
        raise ValueError
    inventory = payload["inventory_episodes"]
    return RunState(
        run_id=_text(payload["run_id"]),
        phase=Phase(payload["phase"]),
        status=RunStatus(payload["status"]),
        event_count=_int(payload["event_count"]),
        tool_calls=_int(payload["tool_calls"]),
        failures=_int(payload["failures"]),
        pending_tool_calls=frozenset(
            _tuple_pairs(payload["pending_tool_calls"])
        ),
        observed_tool_calls=frozenset(
            _tuple_pairs(payload["observed_tool_calls"])
        ),
        work_type=TmdbWorkType(payload["work_type"]),
        budget=_budget(payload["budget"]),
        deadline_at=_parse_timestamp(payload["deadline_at"]),
        candidate_snapshot_id=_optional_text(
            payload["candidate_snapshot_id"]
        ),
        subtitle_acquisition_enabled=(
            None
            if payload["subtitle_acquisition_enabled"] is None
            else _bool(payload["subtitle_acquisition_enabled"])
        ),
        candidate_count=_int(payload["candidate_count"]),
        candidate_ids=(
            None
            if candidate_ids is None
            else tuple(
                CandidateId.parse(item) for item in candidate_ids
            )
        ),
        authorized_source_root=(
            None
            if payload["authorized_source_root"] is None
            else _root(payload["authorized_source_root"])
        ),
        authorized_output_root=(
            None
            if payload["authorized_output_root"] is None
            else _root(payload["authorized_output_root"])
        ),
        tmdb_candidates=frozenset(
            _candidate_refs(payload["tmdb_candidates"])
        ),
        selected_series=selected_series,
        selected_movie=selected_movie,
        selected_work_type=(
            None
            if payload["selected_work_type"] is None
            else TmdbWorkType(payload["selected_work_type"])
        ),
        selected_poster_path=validate_tmdb_poster_path(
            payload["selected_poster_path"]
        ),
        episode_catalog_counts=_int_pairs(
            payload["episode_catalog_counts"]
        ),
        embedded_subtitle_inspections=tuple(
            _embedded_inspection(item)
            for item in payload["embedded_subtitle_inspections"]
        ),
        subtitle_search_records=tuple(
            _subtitle_search_record(item)
            for item in payload["subtitle_search_records"]
        ),
        subtitle_search_failures=_int_text_pairs(
            payload["subtitle_search_failures"]
        ),
        subtitle_archive_capabilities=tuple(
            _subtitle_capability(item)
            for item in payload["subtitle_archive_capabilities"]
        ),
        subtitle_archive_search_bindings=_subtitle_bindings(
            payload["subtitle_archive_search_bindings"]
        ),
        subtitle_selection_decision=(
            None
            if payload["subtitle_selection_decision"] is None
            else _subtitle_selection(
                payload["subtitle_selection_decision"]
            )
        ),
        inventory_episodes=(
            None if inventory is None else _int_pairs(inventory)
        ),
        archive_directory_capabilities=tuple(
            _archive_capability(item)
            for item in payload["archive_directory_capabilities"]
        ),
        archive_searches=tuple(
            _archive_search(item)
            for item in payload["archive_searches"]
        ),
        archive_directory_listings=tuple(
            _archive_listing(item)
            for item in payload["archive_directory_listings"]
        ),
        retryable_directory_failure=_bool(
            payload["retryable_directory_failure"]
        ),
        subtitle_variants=variants,
        mapping_draft=(
            None
            if payload["mapping_draft"] is None
            else _mapping(payload["mapping_draft"])
        ),
        movie_mapping_draft=(
            None
            if payload["movie_mapping_draft"] is None
            else _movie_mapping(payload["movie_mapping_draft"])
        ),
        mapping_review=(
            None
            if payload["mapping_review"] is None
            else PlanReview.from_dict(payload["mapping_review"])
        ),
        mapping_review_call_id=_optional_text(
            payload["mapping_review_call_id"]
        ),
        mapping_conflicts=tuple(
            _issue(item) for item in payload["mapping_conflicts"]
        ),
        rename_plan=(
            None
            if rename_plan_hash is None
            else load_plan(_text(rename_plan_hash))
        ),
        plan_hash=_optional_text(payload["plan_hash"]),
        approval_id=_optional_text(payload["approval_id"]),
        transaction_id=_optional_text(payload["transaction_id"]),
        applied_source_ids=tuple(
            CandidateId.parse(item)
            for item in payload["applied_source_ids"]
        ),
        applied_count=_int(payload["applied_count"]),
        rolled_back_count=_int(payload["rolled_back_count"]),
        validation_issues=tuple(
            _issue(item) for item in payload["validation_issues"]
        ),
        model_turns=_int(payload["model_turns"]),
        model_tokens=_int(payload["model_tokens"]),
        stop_reason=(
            None
            if payload["stop_reason"] is None
            else StopReason(payload["stop_reason"])
        ),
        failure_code=_optional_text(payload["failure_code"]),
    )


def _normalized_payload(
    value: object,
    *,
    schema_version: str | None = None,
) -> dict[str, object]:
    raw = require_object(value, field="run_state")
    keys = frozenset(raw)
    if schema_version is not None:
        expected = {
            LEGACY_STATE_PROJECTION_SCHEMA: (_LEGACY_FIELDS,),
            V2_STATE_PROJECTION_SCHEMA: (_V2_FIELDS,),
            V3_STATE_PROJECTION_SCHEMA: (
                _V3_FIELDS,
                _V3_CURRENT_FIELDS,
            ),
            V4_STATE_PROJECTION_SCHEMA: (_V4_FIELDS,),
            V5_STATE_PROJECTION_SCHEMA: (_V5_FIELDS,),
            V6_STATE_PROJECTION_SCHEMA: (_V6_FIELDS,),
            V7_STATE_PROJECTION_SCHEMA: (_V7_FIELDS,),
            V8_STATE_PROJECTION_SCHEMA: (_FIELDS,),
            STATE_PROJECTION_SCHEMA: (_FIELDS,),
        }.get(schema_version)
        if expected is None or keys not in expected:
            raise ValueError("projection schema does not match payload")
    if keys == _FIELDS:
        return dict(check_fields(raw, _FIELDS, field="run_state"))
    if keys == _V7_FIELDS:
        payload = dict(check_fields(raw, _V7_FIELDS, field="run_state"))
        payload["subtitle_acquisition_enabled"] = None
        payload["subtitle_search_failures"] = []
        return payload
    if keys == _V6_FIELDS:
        payload = dict(check_fields(raw, _V6_FIELDS, field="run_state"))
        payload["subtitle_search_records"] = []
        payload["subtitle_archive_capabilities"] = []
        payload["subtitle_archive_search_bindings"] = []
        payload["subtitle_selection_decision"] = None
        payload["subtitle_search_failures"] = []
        payload["subtitle_acquisition_enabled"] = None
        return payload
    if keys == _V5_FIELDS:
        payload = dict(check_fields(raw, _V5_FIELDS, field="run_state"))
        payload["embedded_subtitle_inspections"] = []
        payload["subtitle_search_records"] = []
        payload["subtitle_archive_capabilities"] = []
        payload["subtitle_archive_search_bindings"] = []
        payload["subtitle_selection_decision"] = None
        payload["subtitle_search_failures"] = []
        payload["subtitle_acquisition_enabled"] = None
        return payload
    if keys == _V4_FIELDS:
        payload = dict(check_fields(raw, _V4_FIELDS, field="run_state"))
        payload["selected_poster_path"] = None
        payload["embedded_subtitle_inspections"] = []
        payload["subtitle_search_records"] = []
        payload["subtitle_archive_capabilities"] = []
        payload["subtitle_archive_search_bindings"] = []
        payload["subtitle_selection_decision"] = None
        payload["subtitle_search_failures"] = []
        payload["subtitle_acquisition_enabled"] = None
        return payload
    if keys == _V3_CURRENT_FIELDS:
        payload = dict(
            check_fields(raw, _V3_CURRENT_FIELDS, field="run_state")
        )
        payload["mapping_review"] = None
        payload["mapping_review_call_id"] = None
        payload["mapping_conflicts"] = []
        payload["selected_poster_path"] = None
        payload["embedded_subtitle_inspections"] = []
        payload["subtitle_search_records"] = []
        payload["subtitle_archive_capabilities"] = []
        payload["subtitle_archive_search_bindings"] = []
        payload["subtitle_selection_decision"] = None
        payload["subtitle_search_failures"] = []
        payload["subtitle_acquisition_enabled"] = None
        return payload
    if keys == _V3_FIELDS:
        payload = dict(check_fields(raw, _V3_FIELDS, field="run_state"))
        payload["retryable_directory_failure"] = False
        payload["mapping_review"] = None
        payload["mapping_review_call_id"] = None
        payload["mapping_conflicts"] = []
        payload["selected_poster_path"] = None
        payload["embedded_subtitle_inspections"] = []
        payload["subtitle_search_records"] = []
        payload["subtitle_archive_capabilities"] = []
        payload["subtitle_archive_search_bindings"] = []
        payload["subtitle_selection_decision"] = None
        payload["subtitle_search_failures"] = []
        payload["subtitle_acquisition_enabled"] = None
        return payload
    if keys == _V2_FIELDS:
        payload = dict(check_fields(raw, _V2_FIELDS, field="run_state"))
        payload["archive_directory_capabilities"] = []
        payload["archive_searches"] = []
        payload["archive_directory_listings"] = []
        payload["retryable_directory_failure"] = False
        payload["mapping_review"] = None
        payload["mapping_review_call_id"] = None
        payload["mapping_conflicts"] = []
        payload["selected_poster_path"] = None
        payload["embedded_subtitle_inspections"] = []
        payload["subtitle_search_records"] = []
        payload["subtitle_archive_capabilities"] = []
        payload["subtitle_archive_search_bindings"] = []
        payload["subtitle_selection_decision"] = None
        payload["subtitle_search_failures"] = []
        payload["subtitle_acquisition_enabled"] = None
        return payload
    if keys == _LEGACY_FIELDS:
        payload = dict(
            check_fields(raw, _LEGACY_FIELDS, field="run_state")
        )
        payload["movie_mapping_draft"] = None
        payload["selected_movie"] = None
        payload["archive_directory_capabilities"] = []
        payload["archive_searches"] = []
        payload["archive_directory_listings"] = []
        payload["retryable_directory_failure"] = False
        payload["mapping_review"] = None
        payload["mapping_review_call_id"] = None
        payload["mapping_conflicts"] = []
        payload["selected_poster_path"] = None
        payload["embedded_subtitle_inspections"] = []
        payload["subtitle_search_records"] = []
        payload["subtitle_archive_capabilities"] = []
        payload["subtitle_archive_search_bindings"] = []
        payload["subtitle_selection_decision"] = None
        payload["subtitle_search_failures"] = []
        payload["subtitle_acquisition_enabled"] = None
        return payload
    return dict(check_fields(raw, _FIELDS, field="run_state"))
