from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.archive_directory import (
    ArchiveDirectoryCapability,
    ArchiveDirectoryListing,
    ArchiveSearchRecord,
)
from reeloom.kernel.mapping import MappingDraft
from reeloom.kernel.movie import MovieMappingDraft
from reeloom.kernel.plan_review import PlanReview
from reeloom.kernel.naming import MovieIdentity, SeriesIdentity
from reeloom.kernel.naming import SubtitleVariant
from reeloom.kernel.initial_plan import InitialPlan
from reeloom.kernel.rename_plan import RootBinding
from reeloom.kernel.semantic_identity import SemanticRootBinding
from reeloom.kernel.subtitle_acquisition import (
    EmbeddedSubtitleInspection,
    SubtitleArchiveSetCapability,
    SubtitleArchiveSetId,
    SubtitleSearchRecord,
    SubtitleSelectionDecision,
)
from reeloom.kernel.tmdb import TmdbCandidateRef, TmdbWorkType
from reeloom.runtime.budget import RunBudget


class Phase(StrEnum):
    BOOTSTRAP = "bootstrap"
    IDENTIFY_SERIES = "identify_series"
    MAP_EPISODES = "map_episodes"
    IDENTIFY_MOVIE = "identify_movie"
    MAP_MOVIE = "map_movie"
    BUILD_PLAN = "build_plan"
    BUILD_SUBTITLE_ACQUISITION_PLAN = "build_subtitle_acquisition_plan"
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
    NEEDS_ATTENTION = "needs_attention"


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
    budget: RunBudget
    deadline_at: datetime
    candidate_snapshot_id: str | None = None
    candidate_count: int = 0
    candidate_ids: tuple[CandidateId, ...] | None = None
    subtitle_acquisition_enabled: bool | None = None
    authorized_source_root: RootBinding | SemanticRootBinding | None = None
    authorized_output_root: RootBinding | SemanticRootBinding | None = None
    tmdb_candidates: frozenset[TmdbCandidateRef] = frozenset()
    selected_series: SeriesIdentity | None = None
    selected_movie: MovieIdentity | None = None
    selected_work_type: TmdbWorkType | None = None
    selected_poster_path: str | None = None
    episode_catalog_counts: tuple[tuple[int, int], ...] = ()
    inventory_episodes: tuple[tuple[int, int], ...] | None = None
    archive_directory_capabilities: tuple[
        ArchiveDirectoryCapability, ...
    ] = ()
    archive_searches: tuple[ArchiveSearchRecord, ...] = ()
    archive_directory_listings: tuple[
        ArchiveDirectoryListing, ...
    ] = ()
    retryable_directory_failure: bool = False
    subtitle_variants: tuple[
        tuple[CandidateId, SubtitleVariant],
        ...,
    ] = ()
    embedded_subtitle_inspections: tuple[
        EmbeddedSubtitleInspection,
        ...,
    ] = ()
    subtitle_search_records: tuple[SubtitleSearchRecord, ...] = ()
    subtitle_search_failures: tuple[tuple[int, str], ...] = ()
    subtitle_archive_capabilities: tuple[
        SubtitleArchiveSetCapability,
        ...,
    ] = ()
    subtitle_archive_search_bindings: tuple[
        tuple[int, SubtitleArchiveSetId],
        ...,
    ] = ()
    subtitle_selection_decision: SubtitleSelectionDecision | None = None
    mapping_draft: MappingDraft | None = None
    movie_mapping_draft: MovieMappingDraft | None = None
    mapping_review: PlanReview | None = None
    mapping_review_call_id: str | None = None
    mapping_conflicts: tuple[MappingValidationIssue, ...] = ()
    rename_plan: InitialPlan | None = None
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
