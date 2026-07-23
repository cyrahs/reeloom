from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.mapping import MappingDraft
from reeloom.kernel.naming import SeriesIdentity
from reeloom.kernel.naming import SubtitleVariant
from reeloom.kernel.rename_plan import RenamePlan, RootBinding
from reeloom.kernel.tmdb import TmdbCandidateRef, TmdbWorkType
from reeloom.runtime.state import MappingValidationIssue, StopReason


@dataclass(frozen=True, slots=True)
class RunStarted:
    run_id: str
    work_type: TmdbWorkType


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
class SubtitleVariantDetected:
    call_id: str
    subtitle_id: CandidateId
    variant: SubtitleVariant


@dataclass(frozen=True, slots=True)
class MappingRejected:
    call_id: str
    issue: MappingValidationIssue


@dataclass(frozen=True, slots=True)
class MappingSubmitted:
    call_id: str
    candidate_snapshot_id: str
    mapping: MappingDraft


@dataclass(frozen=True, slots=True)
class PlanBuilt:
    plan: RenamePlan


@dataclass(frozen=True, slots=True)
class ApprovalRequested:
    plan_hash: str


@dataclass(frozen=True, slots=True)
class ModelUsageRecorded:
    input_tokens: int
    output_tokens: int
    total_tokens: int


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
    | TmdbSeasonCatalogObserved
    | ExistingInventoryObserved
    | SubtitleVariantDetected
    | MappingRejected
    | MappingSubmitted
    | PlanBuilt
    | ApprovalRequested
    | ModelUsageRecorded
    | ToolRequested
    | ToolSucceeded
    | ToolRejected
    | RunStopped
    | RunFailed
)
