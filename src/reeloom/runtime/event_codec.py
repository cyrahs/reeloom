from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import NoReturn

from reeloom.kernel.candidates import (
    Candidate,
    CandidateId,
    CandidateSnapshot,
)
from reeloom.kernel.errors import DomainError
from reeloom.kernel.mapping import (
    EpisodeCatalog,
    MappingDraft,
    VideoMapping,
)
from reeloom.kernel.naming import SeriesIdentity, SubtitleVariant
from reeloom.kernel.rename_plan import RenamePlan, RootBinding
from reeloom.kernel.schema import check_fields
from reeloom.kernel.tmdb import TmdbCandidateRef, TmdbWorkType
from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.events import (
    ApplyFailed,
    ApplyStarted,
    ApprovalRequested,
    CandidateSnapshotCreated,
    ExistingInventoryObserved,
    MappingRejected,
    MappingSubmitted,
    ModelUsageRecorded,
    MoveApplied,
    PlanApproved,
    PlanBuilt,
    RollbackCompleted,
    RunCompleted,
    RunFailed,
    RunStarted,
    RunStopped,
    RuntimeEvent,
    SeriesSelected,
    SubtitleVariantDetected,
    TmdbCandidatesObserved,
    TmdbSeasonCatalogObserved,
    ToolRejected,
    ToolRequested,
    ToolSucceeded,
)
from reeloom.runtime.state import MappingValidationIssue, StopReason

_SCHEMA_VERSION = "runtime-event-v1"
_MAX_EVENT_BYTES = 10 * 1024 * 1024
_ENVELOPE_FIELDS = frozenset({"event_type", "payload", "schema_version"})


def _invalid() -> NoReturn:
    raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate key")
        payload[key] = value
    return payload


def _str(value: object) -> str:
    if not isinstance(value, str):
        _invalid()
    return value


def _int(value: object) -> int:
    if type(value) is not int:
        _invalid()
    return value


def _bool(value: object) -> bool:
    if type(value) is not bool:
        _invalid()
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        _invalid()
    return value


def _number(value: object) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
    ):
        _invalid()
    return float(value)


def _budget_payload(budget: RunBudget) -> dict[str, object]:
    return {
        "max_elapsed_seconds": float(budget.max_elapsed_seconds),
        "max_failures": budget.max_failures,
        "max_model_turns": budget.max_model_turns,
        "max_tool_calls": budget.max_tool_calls,
        "max_total_tokens": budget.max_total_tokens,
    }


def _budget(value: object) -> RunBudget:
    payload = check_fields(
        value,
        frozenset(
            {
                "max_elapsed_seconds",
                "max_failures",
                "max_model_turns",
                "max_tool_calls",
                "max_total_tokens",
            }
        ),
        field="run_budget",
    )
    return RunBudget(
        max_model_turns=_int(payload["max_model_turns"]),
        max_tool_calls=_int(payload["max_tool_calls"]),
        max_failures=_int(payload["max_failures"]),
        max_total_tokens=_int(payload["max_total_tokens"]),
        max_elapsed_seconds=_number(payload["max_elapsed_seconds"]),
    )


def _timestamp(value: datetime) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        _invalid()
    return (
        value.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(
            _str(value).replace("Z", "+00:00")
        )
    except ValueError:
        _invalid()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _invalid()
    return parsed


def _work_type(value: object) -> TmdbWorkType:
    try:
        return TmdbWorkType(_str(value))
    except ValueError:
        _invalid()


def _candidate_id(value: object) -> CandidateId:
    return CandidateId.parse(value)


def _root_payload(root: RootBinding) -> dict[str, object]:
    return {
        "device": root.device,
        "inode": root.inode,
        "path": root.path.as_posix(),
    }


def _root(value: object) -> RootBinding:
    payload = check_fields(
        value,
        frozenset({"device", "inode", "path"}),
        field="root",
    )
    return RootBinding(
        PurePosixPath(_str(payload["path"])),
        _int(payload["device"]),
        _int(payload["inode"]),
    )


def _series_payload(series: SeriesIdentity) -> dict[str, object]:
    return {
        "title_zh_cn": series.title_zh_cn,
        "tmdb_id": series.tmdb_id,
        "year": series.year,
    }


def _mapping_payload(mapping: MappingDraft) -> dict[str, object]:
    return {
        "subtitles": [
            {
                "subtitle_id": str(item.subtitle_id),
                "video_id": str(item.video_id),
            }
            for item in mapping.subtitles
        ],
        "videos": [
            {
                "episode_end": item.span.episode_end,
                "episode_start": item.span.episode_start,
                "season": item.span.season,
                "video_id": str(item.video_id),
            }
            for item in mapping.videos
        ],
    }


def _mapping(value: object) -> MappingDraft:
    payload = check_fields(
        value,
        frozenset({"subtitles", "videos"}),
        field="mapping",
    )
    raw_videos = _list(payload["videos"])
    raw_subtitles = _list(payload["subtitles"])
    videos = tuple(VideoMapping.from_dict(item) for item in raw_videos)
    candidate_ids = {video.video_id for video in videos}
    for item in raw_subtitles:
        subtitle = check_fields(
            item,
            frozenset({"subtitle_id", "video_id"}),
            field="subtitle_mapping",
        )
        candidate_ids.add(_candidate_id(subtitle["subtitle_id"]))
        candidate_ids.add(_candidate_id(subtitle["video_id"]))
    candidates = CandidateSnapshot.create(
        Candidate(candidate_id, candidate_id.kind, str(candidate_id))
        for candidate_id in sorted(
            candidate_ids,
            key=lambda item: (item.kind.value, item.ordinal),
        )
    )
    counts: dict[int, int] = {}
    for video in videos:
        counts[video.span.season] = max(
            counts.get(video.span.season, 0),
            video.span.episode_end,
        )
    return MappingDraft.from_dict(
        payload,
        candidates=candidates,
        catalog=EpisodeCatalog.from_counts(counts),
    )


def _issue_payload(issue: MappingValidationIssue) -> dict[str, object]:
    return {
        "code": issue.code,
        "context": [
            {
                "key": key,
                "value": list(value) if isinstance(value, tuple) else value,
            }
            for key, value in issue.context
        ],
    }


def _issue(value: object) -> MappingValidationIssue:
    payload = check_fields(
        value,
        frozenset({"code", "context"}),
        field="mapping_issue",
    )
    context: list[tuple[str, int | str | tuple[str, ...]]] = []
    for item in _list(payload["context"]):
        entry = check_fields(
            item,
            frozenset({"key", "value"}),
            field="mapping_issue.context",
        )
        raw_value = entry["value"]
        if isinstance(raw_value, list):
            parsed_value: int | str | tuple[str, ...] = tuple(
                _str(part) for part in raw_value
            )
        elif type(raw_value) is int:
            parsed_value = raw_value
        else:
            parsed_value = _str(raw_value)
        context.append((_str(entry["key"]), parsed_value))
    return MappingValidationIssue(_str(payload["code"]), tuple(context))


def _candidate_refs_payload(
    refs: tuple[TmdbCandidateRef, ...],
) -> list[dict[str, object]]:
    return [
        {"tmdb_id": ref.tmdb_id, "work_type": ref.work_type.value}
        for ref in refs
    ]


def _candidate_refs(value: object) -> tuple[TmdbCandidateRef, ...]:
    refs: list[TmdbCandidateRef] = []
    for item in _list(value):
        payload = check_fields(
            item,
            frozenset({"tmdb_id", "work_type"}),
            field="tmdb_candidate",
        )
        refs.append(
            TmdbCandidateRef(
                _work_type(payload["work_type"]),
                _int(payload["tmdb_id"]),
            )
        )
    return tuple(refs)


def _event_payload(event: RuntimeEvent) -> tuple[str, dict[str, object]]:
    if isinstance(event, RunStarted):
        return "run_started", {
            "budget": _budget_payload(event.budget),
            "deadline_at": _timestamp(event.deadline_at),
            "run_id": event.run_id,
            "work_type": event.work_type.value,
        }
    if isinstance(event, CandidateSnapshotCreated):
        return "candidate_snapshot_created", {
            "candidate_count": event.candidate_count,
            "candidate_ids": (
                [str(item) for item in event.candidate_ids]
                if event.candidate_ids is not None
                else None
            ),
            "output_root": (
                _root_payload(event.output_root)
                if event.output_root is not None
                else None
            ),
            "snapshot_id": event.snapshot_id,
            "source_root": (
                _root_payload(event.source_root)
                if event.source_root is not None
                else None
            ),
        }
    if isinstance(event, TmdbCandidatesObserved):
        return "tmdb_candidates_observed", {
            "candidates": _candidate_refs_payload(event.candidates)
        }
    if isinstance(event, SeriesSelected):
        return "series_selected", {
            "series": _series_payload(event.series),
            "work_type": event.work_type.value,
        }
    if isinstance(event, TmdbSeasonCatalogObserved):
        return "tmdb_season_catalog_observed", {
            "call_id": event.call_id,
            "episode_count": event.episode_count,
            "season_number": event.season_number,
            "tmdb_id": event.tmdb_id,
            "work_type": event.work_type.value,
        }
    if isinstance(event, ExistingInventoryObserved):
        return "existing_inventory_observed", {
            "call_id": event.call_id,
            "occupied": [list(item) for item in event.occupied],
            "tmdb_id": event.tmdb_id,
            "work_type": event.work_type.value,
        }
    if isinstance(event, SubtitleVariantDetected):
        return "subtitle_variant_detected", {
            "call_id": event.call_id,
            "subtitle_id": str(event.subtitle_id),
            "variant": event.variant.value,
        }
    if isinstance(event, MappingRejected):
        return "mapping_rejected", {
            "call_id": event.call_id,
            "issue": _issue_payload(event.issue),
        }
    if isinstance(event, MappingSubmitted):
        return "mapping_submitted", {
            "call_id": event.call_id,
            "candidate_snapshot_id": event.candidate_snapshot_id,
            "mapping": _mapping_payload(event.mapping),
        }
    if isinstance(event, PlanBuilt):
        return "plan_built", {
            "canonical_plan": event.plan.canonical_bytes().decode("ascii"),
            "plan_hash": event.plan.plan_hash,
        }
    if isinstance(event, ApprovalRequested):
        return "approval_requested", {"plan_hash": event.plan_hash}
    if isinstance(event, PlanApproved):
        return "plan_approved", {
            "approval_id": event.approval_id,
            "plan_hash": event.plan_hash,
        }
    if isinstance(event, ApplyStarted):
        return "apply_started", {
            "approval_id": event.approval_id,
            "plan_hash": event.plan_hash,
        }
    if isinstance(event, MoveApplied):
        return "move_applied", {"source_id": str(event.source_id)}
    if isinstance(event, ApplyFailed):
        return "apply_failed", {"code": event.code}
    if isinstance(event, RollbackCompleted):
        return "rollback_completed", {
            "rolled_back_count": event.rolled_back_count,
            "transaction_id": event.transaction_id,
        }
    if isinstance(event, RunCompleted):
        return "run_completed", {
            "applied_count": event.applied_count,
            "transaction_id": event.transaction_id,
        }
    if isinstance(event, ModelUsageRecorded):
        return "model_usage_recorded", {
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "total_tokens": event.total_tokens,
        }
    if isinstance(event, ToolRequested):
        return "tool_requested", {
            "call_id": event.call_id,
            "tool_name": event.tool_name,
        }
    if isinstance(event, ToolSucceeded):
        return "tool_succeeded", {
            "call_id": event.call_id,
            "tool_name": event.tool_name,
        }
    if isinstance(event, ToolRejected):
        return "tool_rejected", {
            "call_id": event.call_id,
            "code": event.code,
            "retryable": event.retryable,
            "tool_name": event.tool_name,
        }
    if isinstance(event, RunStopped):
        return "run_stopped", {"reason": event.reason.value}
    if isinstance(event, RunFailed):
        return "run_failed", {"code": event.code}
    _invalid()


def _fields(
    value: object,
    names: set[str],
    *,
    field: str,
) -> dict[str, object]:
    return dict(check_fields(value, frozenset(names), field=field))


def _event_from_payload(
    event_type: str,
    value: object,
) -> RuntimeEvent:
    if event_type == "run_started":
        p = _fields(
            value,
            {"budget", "deadline_at", "run_id", "work_type"},
            field=event_type,
        )
        return RunStarted(
            _str(p["run_id"]),
            _work_type(p["work_type"]),
            _budget(p["budget"]),
            _parse_timestamp(p["deadline_at"]),
        )
    if event_type == "candidate_snapshot_created":
        p = _fields(
            value,
            {
                "candidate_count",
                "candidate_ids",
                "output_root",
                "snapshot_id",
                "source_root",
            },
            field=event_type,
        )
        ids = p["candidate_ids"]
        return CandidateSnapshotCreated(
            snapshot_id=_str(p["snapshot_id"]),
            candidate_count=_int(p["candidate_count"]),
            candidate_ids=(
                tuple(_candidate_id(item) for item in _list(ids))
                if ids is not None
                else None
            ),
            source_root=(
                _root(p["source_root"])
                if p["source_root"] is not None
                else None
            ),
            output_root=(
                _root(p["output_root"])
                if p["output_root"] is not None
                else None
            ),
        )
    if event_type == "tmdb_candidates_observed":
        p = _fields(value, {"candidates"}, field=event_type)
        return TmdbCandidatesObserved(_candidate_refs(p["candidates"]))
    if event_type == "series_selected":
        p = _fields(value, {"series", "work_type"}, field=event_type)
        return SeriesSelected(
            SeriesIdentity.from_dict(p["series"]),
            _work_type(p["work_type"]),
        )
    if event_type == "tmdb_season_catalog_observed":
        p = _fields(
            value,
            {
                "call_id",
                "episode_count",
                "season_number",
                "tmdb_id",
                "work_type",
            },
            field=event_type,
        )
        return TmdbSeasonCatalogObserved(
            _str(p["call_id"]),
            _int(p["tmdb_id"]),
            _work_type(p["work_type"]),
            _int(p["season_number"]),
            _int(p["episode_count"]),
        )
    if event_type == "existing_inventory_observed":
        p = _fields(
            value,
            {"call_id", "occupied", "tmdb_id", "work_type"},
            field=event_type,
        )
        occupied = tuple(
            (_int(pair[0]), _int(pair[1]))
            for item in _list(p["occupied"])
            for pair in [_list(item)]
            if len(pair) == 2
        )
        if len(occupied) != len(_list(p["occupied"])):
            _invalid()
        return ExistingInventoryObserved(
            _str(p["call_id"]),
            _int(p["tmdb_id"]),
            _work_type(p["work_type"]),
            occupied,
        )
    if event_type == "subtitle_variant_detected":
        p = _fields(
            value,
            {"call_id", "subtitle_id", "variant"},
            field=event_type,
        )
        try:
            variant = SubtitleVariant(_str(p["variant"]))
        except ValueError:
            _invalid()
        return SubtitleVariantDetected(
            _str(p["call_id"]),
            _candidate_id(p["subtitle_id"]),
            variant,
        )
    if event_type == "mapping_rejected":
        p = _fields(value, {"call_id", "issue"}, field=event_type)
        return MappingRejected(_str(p["call_id"]), _issue(p["issue"]))
    if event_type == "mapping_submitted":
        p = _fields(
            value,
            {"call_id", "candidate_snapshot_id", "mapping"},
            field=event_type,
        )
        return MappingSubmitted(
            _str(p["call_id"]),
            _str(p["candidate_snapshot_id"]),
            _mapping(p["mapping"]),
        )
    if event_type == "plan_built":
        p = _fields(
            value,
            {"canonical_plan", "plan_hash"},
            field=event_type,
        )
        return PlanBuilt(
            RenamePlan.from_canonical_bytes(
                _str(p["canonical_plan"]).encode("ascii"),
                plan_hash=_str(p["plan_hash"]),
            )
        )
    if event_type == "approval_requested":
        p = _fields(value, {"plan_hash"}, field=event_type)
        return ApprovalRequested(_str(p["plan_hash"]))
    if event_type == "plan_approved":
        p = _fields(
            value,
            {"approval_id", "plan_hash"},
            field=event_type,
        )
        return PlanApproved(_str(p["plan_hash"]), _str(p["approval_id"]))
    if event_type == "apply_started":
        p = _fields(
            value,
            {"approval_id", "plan_hash"},
            field=event_type,
        )
        return ApplyStarted(_str(p["plan_hash"]), _str(p["approval_id"]))
    if event_type == "move_applied":
        p = _fields(value, {"source_id"}, field=event_type)
        return MoveApplied(_candidate_id(p["source_id"]))
    if event_type == "apply_failed":
        p = _fields(value, {"code"}, field=event_type)
        return ApplyFailed(_str(p["code"]))
    if event_type == "rollback_completed":
        p = _fields(
            value,
            {"rolled_back_count", "transaction_id"},
            field=event_type,
        )
        return RollbackCompleted(
            _str(p["transaction_id"]),
            _int(p["rolled_back_count"]),
        )
    if event_type == "run_completed":
        p = _fields(
            value,
            {"applied_count", "transaction_id"},
            field=event_type,
        )
        return RunCompleted(
            _str(p["transaction_id"]),
            _int(p["applied_count"]),
        )
    if event_type == "model_usage_recorded":
        p = _fields(
            value,
            {"input_tokens", "output_tokens", "total_tokens"},
            field=event_type,
        )
        return ModelUsageRecorded(
            _int(p["input_tokens"]),
            _int(p["output_tokens"]),
            _int(p["total_tokens"]),
        )
    if event_type in {"tool_requested", "tool_succeeded"}:
        p = _fields(value, {"call_id", "tool_name"}, field=event_type)
        event_class = (
            ToolRequested
            if event_type == "tool_requested"
            else ToolSucceeded
        )
        return event_class(_str(p["call_id"]), _str(p["tool_name"]))
    if event_type == "tool_rejected":
        p = _fields(
            value,
            {"call_id", "code", "retryable", "tool_name"},
            field=event_type,
        )
        return ToolRejected(
            _str(p["call_id"]),
            _str(p["tool_name"]),
            _str(p["code"]),
            _bool(p["retryable"]),
        )
    if event_type == "run_stopped":
        p = _fields(value, {"reason"}, field=event_type)
        try:
            reason = StopReason(_str(p["reason"]))
        except ValueError:
            _invalid()
        return RunStopped(reason)
    if event_type == "run_failed":
        p = _fields(value, {"code"}, field=event_type)
        return RunFailed(_str(p["code"]))
    _invalid()


def encode_event(event: RuntimeEvent) -> bytes:
    """Encode one typed event into a strict canonical JSON envelope."""

    try:
        event_type, payload = _event_payload(event)
        encoded = json.dumps(
            {
                "event_type": event_type,
                "payload": payload,
                "schema_version": _SCHEMA_VERSION,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (DomainError, TypeError, UnicodeEncodeError, ValueError):
        _invalid()
    if len(encoded) > _MAX_EVENT_BYTES:
        _invalid()
    return encoded


def decode_event(content: bytes) -> RuntimeEvent:
    """Decode only current, canonical, fully typed event envelopes."""

    if (
        not isinstance(content, bytes)
        or not 0 < len(content) <= _MAX_EVENT_BYTES
    ):
        _invalid()
    try:
        envelope = check_fields(
            json.loads(
                content,
                object_pairs_hook=_reject_duplicate_keys,
            ),
            _ENVELOPE_FIELDS,
            field="runtime_event",
        )
        if envelope["schema_version"] != _SCHEMA_VERSION:
            _invalid()
        event = _event_from_payload(
            _str(envelope["event_type"]),
            envelope["payload"],
        )
        if encode_event(event) != content:
            _invalid()
        return event
    except RuntimeDomainError:
        raise
    except (
        DomainError,
        UnicodeDecodeError,
        UnicodeEncodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        _invalid()
