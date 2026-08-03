from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeAlias

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
from reeloom.kernel.tmdb import TmdbCandidateRef, TmdbWorkType
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.state import MappingValidationIssue, StopReason

_DEFAULT_DEADLINE = datetime.max.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RunStarted:
    run_id: str
    work_type: TmdbWorkType
    budget: RunBudget = RunBudget()
    deadline_at: datetime = _DEFAULT_DEADLINE


@dataclass(frozen=True, slots=True)
class CandidateSnapshotCreated:
    snapshot_id: str
    candidate_count: int
    candidate_ids: tuple[CandidateId, ...] | None = None
    source_root: RootBinding | None = None
    output_root: RootBinding | None = None


@dataclass(frozen=True, slots=True)
class TmdbCandidatesObserved:
    candidates: tuple[TmdbCandidateRef, ...]


@dataclass(frozen=True, slots=True)
class SeriesSelected:
    series: SeriesIdentity
    work_type: TmdbWorkType
    poster_path: str | None = None


@dataclass(frozen=True, slots=True)
class MovieSelected:
    movie: MovieIdentity
    work_type: TmdbWorkType
    poster_path: str | None = None


@dataclass(frozen=True, slots=True)
class TmdbSeasonCatalogObserved:
    call_id: str
    tmdb_id: int
    work_type: TmdbWorkType
    season_number: int
    episode_count: int


@dataclass(frozen=True, slots=True)
class ExistingInventoryObserved:
    call_id: str
    tmdb_id: int
    work_type: TmdbWorkType
    occupied: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class ArchiveSearchObserved:
    search: ArchiveSearchRecord
    capabilities: tuple[ArchiveDirectoryCapability, ...]


@dataclass(frozen=True, slots=True)
class ArchiveDirectoryListed:
    listing: ArchiveDirectoryListing
    capabilities: tuple[ArchiveDirectoryCapability, ...]


@dataclass(frozen=True, slots=True)
class SubtitleVariantDetected:
    call_id: str
    subtitle_id: CandidateId
    variant: SubtitleVariant


@dataclass(frozen=True, slots=True)
class MappingRejected:
    call_id: str
    issue: MappingValidationIssue


@dataclass(frozen=True, slots=True)
class MappingReviewCaptured:
    call_id: str
    review: PlanReview


@dataclass(frozen=True, slots=True)
class MappingSubmitted:
    call_id: str
    candidate_snapshot_id: str
    mapping: MappingDraft


@dataclass(frozen=True, slots=True)
class MovieMappingSubmitted:
    call_id: str
    candidate_snapshot_id: str
    mapping: MovieMappingDraft


@dataclass(frozen=True, slots=True)
class PlanBuilt:
    plan: InitialPlan


@dataclass(frozen=True, slots=True)
class ApprovalRequested:
    plan_hash: str


@dataclass(frozen=True, slots=True)
class PlanApproved:
    plan_hash: str
    approval_id: str


@dataclass(frozen=True, slots=True)
class ApplyStarted:
    plan_hash: str
    approval_id: str


@dataclass(frozen=True, slots=True)
class MoveApplied:
    source_id: CandidateId


@dataclass(frozen=True, slots=True)
class ApplyFailed:
    code: str


@dataclass(frozen=True, slots=True)
class RollbackCompleted:
    transaction_id: str
    rolled_back_count: int


@dataclass(frozen=True, slots=True)
class RunCompleted:
    transaction_id: str
    applied_count: int


@dataclass(frozen=True, slots=True)
class ModelUsageRecorded:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class InteractionCompleted:
    interaction_id: str
    kind: str
    model_turns: int
    model_tokens: int
    fresh_mapping_submitted: bool
    final_plan_hash: str
    plan_hash: str | None = None
    tool_calls: int = 0
    failures: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionSettled:
    plan_hash: str
    approval_id: str
    transaction_id: str
    status: str
    applied_count: int
    rolled_back_count: int
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class ToolRequested:
    call_id: str
    tool_name: str


@dataclass(frozen=True, slots=True)
class ToolSucceeded:
    call_id: str
    tool_name: str


@dataclass(frozen=True, slots=True)
class ToolRejected:
    call_id: str
    tool_name: str
    code: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class RunStopped:
    reason: StopReason


@dataclass(frozen=True, slots=True)
class RunFailed:
    code: str


RuntimeEvent: TypeAlias = (
    RunStarted
    | CandidateSnapshotCreated
    | TmdbCandidatesObserved
    | SeriesSelected
    | MovieSelected
    | TmdbSeasonCatalogObserved
    | ExistingInventoryObserved
    | ArchiveSearchObserved
    | ArchiveDirectoryListed
    | SubtitleVariantDetected
    | MappingRejected
    | MappingReviewCaptured
    | MappingSubmitted
    | MovieMappingSubmitted
    | PlanBuilt
    | ApprovalRequested
    | PlanApproved
    | ApplyStarted
    | MoveApplied
    | ApplyFailed
    | RollbackCompleted
    | RunCompleted
    | ModelUsageRecorded
    | InteractionCompleted
    | ExecutionSettled
    | ToolRequested
    | ToolSucceeded
    | ToolRejected
    | RunStopped
    | RunFailed
)
