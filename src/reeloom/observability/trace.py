from __future__ import annotations

import json
from dataclasses import dataclass

from reeloom.kernel.errors import ErrorCode
from reeloom.runtime.errors import RuntimeErrorCode
from reeloom.runtime.events import (
    ApplyFailed,
    ApplyStarted,
    ArchiveDirectoryListed,
    ArchiveSearchObserved,
    ApprovalRequested,
    CandidateSnapshotCreated,
    ExistingInventoryObserved,
    EmbeddedSubtitlesInspected,
    SubtitleSearchObserved,
    SubtitleSelectionSubmitted,
    MappingRejected,
    MappingReviewCaptured,
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
from reeloom.runtime.reducer import reduce_event
from reeloom.runtime.state import Phase, RunState, RunStatus
from reeloom.runtime.store import StoredEvent

_SCHEMA_VERSION = "redacted-trace-v1"
_ALLOWED_TOOLS = frozenset(
    {
        "detect_subtitle_variant",
        "check_sub_from_video",
        "search_sub",
        "select_subtitle_release",
        "get_existing_inventory",
        "list_dir",
        "get_tmdb_season",
        "get_tmdb_series",
        "list_candidates",
        "search_tmdb",
        "search_dir",
        "select_series",
        "submit_mapping",
    }
)
_ALLOWED_CODES = frozenset(
    {code.value for code in ErrorCode}
    | {code.value for code in RuntimeErrorCode}
)
TraceValue = bool | int | str


def _safe_tool(value: str) -> str:
    return value if value in _ALLOWED_TOOLS else "unknown"


def _safe_code(value: str) -> str:
    return value if value in _ALLOWED_CODES else "other"


@dataclass(frozen=True, slots=True)
class TraceRecord:
    sequence: int
    event_type: str
    phase: Phase
    status: RunStatus
    attributes: tuple[tuple[str, TraceValue], ...] = ()


@dataclass(frozen=True, slots=True)
class TraceSummary:
    event_count: int
    tool_calls: int
    tool_rejections: int
    mapping_rejections: int
    model_turns: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    mapping_success: bool
    unmapped_count: int
    applied_count: int


@dataclass(frozen=True, slots=True)
class TraceReport:
    schema_version: str
    run_id: str
    records: tuple[TraceRecord, ...]
    summary: TraceSummary

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            {
                "records": [
                    {
                        "attributes": dict(record.attributes),
                        "event_type": record.event_type,
                        "phase": record.phase.value,
                        "sequence": record.sequence,
                        "status": record.status.value,
                    }
                    for record in self.records
                ],
                "run_id": self.run_id,
                "schema_version": self.schema_version,
                "summary": {
                    "applied_count": self.summary.applied_count,
                    "event_count": self.summary.event_count,
                    "input_tokens": self.summary.input_tokens,
                    "mapping_rejections": self.summary.mapping_rejections,
                    "mapping_success": self.summary.mapping_success,
                    "model_turns": self.summary.model_turns,
                    "output_tokens": self.summary.output_tokens,
                    "tool_calls": self.summary.tool_calls,
                    "tool_rejections": self.summary.tool_rejections,
                    "total_tokens": self.summary.total_tokens,
                    "unmapped_count": self.summary.unmapped_count,
                },
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")


def _attributes(event: RuntimeEvent) -> dict[str, TraceValue]:
    if isinstance(event, RunStarted):
        return {"work_type": event.work_type.value}
    if isinstance(event, CandidateSnapshotCreated):
        return {"candidate_count": event.candidate_count}
    if isinstance(event, TmdbCandidatesObserved):
        return {"candidate_count": len(event.candidates)}
    if isinstance(event, SeriesSelected):
        return {
            "tmdb_id": event.series.tmdb_id,
            "work_type": event.work_type.value,
        }
    if isinstance(event, TmdbSeasonCatalogObserved):
        return {
            "episode_count": event.episode_count,
            "season_number": event.season_number,
            "tmdb_id": event.tmdb_id,
        }
    if isinstance(event, ExistingInventoryObserved):
        return {
            "occupied_count": len(event.occupied),
            "tmdb_id": event.tmdb_id,
        }
    if isinstance(event, ArchiveSearchObserved):
        return {
            "complete": event.search.complete,
            "match_count": len(event.search.directory_ids),
            "work_type": event.search.work_type.value,
        }
    if isinstance(event, ArchiveDirectoryListed):
        return {
            "complete": event.listing.complete,
            "directory_count": len(event.listing.child_ids),
            "video_count": len(event.listing.videos),
        }
    if isinstance(event, SubtitleVariantDetected):
        return {"variant": event.variant.value}
    if isinstance(event, EmbeddedSubtitlesInspected):
        return {
            "chinese_status": event.inspection.chinese_status.value,
            "probe_status": event.inspection.probe_status.value,
            "season_number": event.inspection.season_number,
            "track_count": len(event.inspection.tracks),
        }
    if isinstance(event, SubtitleSearchObserved):
        return {
            "archive_set_count": len(event.capabilities),
            "complete": event.record.page.complete,
            "has_next_cursor": event.record.page.next_cursor is not None,
            "release_count": len(event.record.page.items),
            "season_number": event.record.season_number,
        }
    if isinstance(event, SubtitleSelectionSubmitted):
        return {
            "selection_count": len(event.decision.selections),
            "status": event.decision.status.value,
        }
    if isinstance(event, MappingRejected):
        return {"code": _safe_code(event.issue.code)}
    if isinstance(event, MappingReviewCaptured):
        return {
            "item_count": len(event.review.items),
            "verified_count": sum(
                item.verification.value == "verified"
                for item in event.review.items
            ),
        }
    if isinstance(event, MappingSubmitted):
        return {
            "subtitle_count": len(event.mapping.subtitles),
            "video_count": len(event.mapping.videos),
        }
    if isinstance(event, PlanBuilt):
        return {
            "move_count": len(event.plan.draft.moves),
            "plan_hash": event.plan.plan_hash,
            "unmapped_count": len(
                event.plan.draft.unmapped_candidate_ids
            ),
        }
    if isinstance(event, (ApprovalRequested, PlanApproved, ApplyStarted)):
        return {"plan_hash": event.plan_hash}
    if isinstance(event, MoveApplied):
        return {"source_kind": event.source_id.kind.value}
    if isinstance(event, ApplyFailed):
        return {"code": _safe_code(event.code)}
    if isinstance(event, RollbackCompleted):
        return {"rolled_back_count": event.rolled_back_count}
    if isinstance(event, RunCompleted):
        return {"applied_count": event.applied_count}
    if isinstance(event, ModelUsageRecorded):
        return {
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "total_tokens": event.total_tokens,
        }
    if isinstance(event, (ToolRequested, ToolSucceeded)):
        return {"tool_name": _safe_tool(event.tool_name)}
    if isinstance(event, ToolRejected):
        return {
            "code": _safe_code(event.code),
            "retryable": event.retryable,
            "tool_name": _safe_tool(event.tool_name),
        }
    if isinstance(event, RunStopped):
        return {"reason": event.reason.value}
    if isinstance(event, RunFailed):
        return {"code": _safe_code(event.code)}
    return {}


def build_trace(events: tuple[StoredEvent, ...]) -> TraceReport:
    state: RunState | None = None
    records: list[TraceRecord] = []
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    mapping_rejections = 0
    tool_rejections = 0
    for expected_sequence, stored in enumerate(events, start=1):
        if (
            not isinstance(stored, StoredEvent)
            or stored.sequence != expected_sequence
        ):
            raise ValueError("invalid trace event sequence")
        state = reduce_event(state, stored.event)
        if isinstance(stored.event, ModelUsageRecorded):
            input_tokens += stored.event.input_tokens
            output_tokens += stored.event.output_tokens
            total_tokens += stored.event.total_tokens
        mapping_rejections += isinstance(stored.event, MappingRejected)
        tool_rejections += isinstance(stored.event, ToolRejected)
        records.append(
            TraceRecord(
                sequence=stored.sequence,
                event_type=type(stored.event).__name__,
                phase=state.phase,
                status=state.status,
                attributes=tuple(
                    sorted(_attributes(stored.event).items())
                ),
            )
        )
    if state is None:
        raise ValueError("trace requires a started run")
    return TraceReport(
        schema_version=_SCHEMA_VERSION,
        run_id=state.run_id,
        records=tuple(records),
        summary=TraceSummary(
            event_count=state.event_count,
            tool_calls=state.tool_calls,
            tool_rejections=tool_rejections,
            mapping_rejections=mapping_rejections,
            model_turns=state.model_turns,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            mapping_success=state.rename_plan is not None,
            unmapped_count=(
                len(state.rename_plan.draft.unmapped_candidate_ids)
                if state.rename_plan is not None
                else 0
            ),
            applied_count=state.applied_count,
        ),
    )
