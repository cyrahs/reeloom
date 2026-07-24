from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.mapping import MappingDraft
from reeloom.kernel.naming import SeriesIdentity
from reeloom.kernel.naming import SubtitleVariant
from reeloom.kernel.rename_plan import RenamePlan, RootBinding
from reeloom.kernel.tmdb import TmdbCandidateRef, TmdbWorkType


class Phase(StrEnum):
    BOOTSTRAP = "bootstrap"
    IDENTIFY_SERIES = "identify_series"
    MAP_EPISODES = "map_episodes"
    BUILD_PLAN = "build_plan"
    AWAITING_APPROVAL = "awaiting_approval"
    APPLYING = "applying"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class RunStatus(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


class StopReason(StrEnum):
    MODEL_FINAL = "model_final"
    AWAITING_APPROVAL = "awaiting_approval"
    MAX_TURNS = "max_turns"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FATAL_ERROR = "fatal_error"


ValidationValue = int | str | tuple[str, ...]
_MAX_VALIDATION_TEXT_BYTES = 160


def _is_bounded_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= _MAX_VALIDATION_TEXT_BYTES
    )


@dataclass(frozen=True, slots=True)
class MappingValidationIssue:
    code: str
    context: tuple[tuple[str, ValidationValue], ...] = ()

    def __post_init__(self) -> None:
        if (
            not _is_bounded_text(self.code)
            or not isinstance(self.context, tuple)
            or len(self.context) > 8
            or any(
                not _is_bounded_text(key)
                or not isinstance(value, (int, str, tuple))
                or isinstance(value, bool)
                or (
                    isinstance(value, str)
                    and not _is_bounded_text(value)
                )
                or (
                    isinstance(value, tuple)
                    and (
                        len(value) > 8
                        or any(
                            not _is_bounded_text(item)
                            for item in value
                        )
                    )
                )
                for key, value in self.context
            )
        ):
            raise ValueError("invalid mapping validation issue")


@dataclass(frozen=True, slots=True)
class RunState:
    """A replayable projection of Reeloom domain events."""

    run_id: str
    phase: Phase
    status: RunStatus
    event_count: int
    tool_calls: int
    failures: int
    pending_tool_calls: frozenset[tuple[str, str]]
    observed_tool_calls: frozenset[tuple[str, str]]
    work_type: TmdbWorkType
    candidate_snapshot_id: str | None = None
    candidate_count: int = 0
    candidate_ids: tuple[CandidateId, ...] | None = None
    authorized_source_root: RootBinding | None = None
    authorized_output_root: RootBinding | None = None
    tmdb_candidates: frozenset[TmdbCandidateRef] = frozenset()
    selected_series: SeriesIdentity | None = None
    selected_work_type: TmdbWorkType | None = None
    episode_catalog_counts: tuple[tuple[int, int], ...] = ()
    inventory_episodes: tuple[tuple[int, int], ...] | None = None
    subtitle_variants: tuple[
        tuple[CandidateId, SubtitleVariant],
        ...,
    ] = ()
    mapping_draft: MappingDraft | None = None
    rename_plan: RenamePlan | None = None
    plan_hash: str | None = None
    approval_id: str | None = None
    transaction_id: str | None = None
    applied_source_ids: tuple[CandidateId, ...] = ()
    applied_count: int = 0
    rolled_back_count: int = 0
    validation_issues: tuple[MappingValidationIssue, ...] = ()
    model_turns: int = 0
    model_tokens: int = 0
    stop_reason: StopReason | None = None
    failure_code: str | None = None
