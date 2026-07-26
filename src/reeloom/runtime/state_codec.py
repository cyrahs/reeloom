from __future__ import annotations

import json
from collections.abc import Callable

from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.naming import SeriesIdentity, SubtitleVariant
from reeloom.kernel.rename_plan import RenamePlan
from reeloom.kernel.schema import check_fields
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.runtime.event_codec import (
    _budget,
    _budget_payload,
    _candidate_refs,
    _candidate_refs_payload,
    _issue,
    _issue_payload,
    _mapping,
    _mapping_payload,
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

STATE_PROJECTION_SCHEMA = "runtime-state-v1"
_FIELDS = frozenset(
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
        "deadline_at": _timestamp(state.deadline_at),
        "episode_catalog_counts": [
            list(item) for item in state.episode_catalog_counts
        ],
        "event_count": state.event_count,
        "failure_code": state.failure_code,
        "failures": state.failures,
        "inventory_episodes": (
            None
            if state.inventory_episodes is None
            else [list(item) for item in state.inventory_episodes]
        ),
        "mapping_draft": (
            None
            if state.mapping_draft is None
            else _mapping_payload(state.mapping_draft)
        ),
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
        "selected_work_type": (
            None
            if state.selected_work_type is None
            else state.selected_work_type.value
        ),
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
    **changes: object,
) -> str:
    payload = check_fields(value, _FIELDS, field="run_state")
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


def decode_state(
    value: object,
    *,
    load_plan: Callable[[str], RenamePlan],
) -> RunState:
    payload = check_fields(value, _FIELDS, field="run_state")
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
        run_id=str(payload["run_id"]),
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
        selected_work_type=(
            None
            if payload["selected_work_type"] is None
            else TmdbWorkType(payload["selected_work_type"])
        ),
        episode_catalog_counts=_int_pairs(
            payload["episode_catalog_counts"]
        ),
        inventory_episodes=(
            None if inventory is None else _int_pairs(inventory)
        ),
        subtitle_variants=variants,
        mapping_draft=(
            None
            if payload["mapping_draft"] is None
            else _mapping(payload["mapping_draft"])
        ),
        rename_plan=(
            None
            if rename_plan_hash is None
            else load_plan(str(rename_plan_hash))
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
