"""Reeloom run state, events, policies, and event storage."""

from reeloom.runtime.events import (
    CandidateSnapshotCreated,
    ExistingInventoryObserved,
    MappingRejected,
    MappingSubmitted,
    ModelUsageRecorded,
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
from reeloom.runtime.state import (
    MappingValidationIssue,
    Phase,
    RunState,
    RunStatus,
    StopReason,
)
from reeloom.runtime.store import InMemoryEventStore, StoredEvent

__all__ = [
    "InMemoryEventStore",
    "CandidateSnapshotCreated",
    "ExistingInventoryObserved",
    "MappingRejected",
    "MappingSubmitted",
    "MappingValidationIssue",
    "ModelUsageRecorded",
    "Phase",
    "RunFailed",
    "RunStarted",
    "RunState",
    "RunStatus",
    "RunStopped",
    "RuntimeEvent",
    "StopReason",
    "StoredEvent",
    "SeriesSelected",
    "SubtitleVariantDetected",
    "TmdbCandidatesObserved",
    "TmdbSeasonCatalogObserved",
    "ToolRejected",
    "ToolRequested",
    "ToolSucceeded",
    "reduce_event",
]
