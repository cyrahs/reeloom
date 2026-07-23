from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from reeloom.runtime.state import StopReason


@dataclass(frozen=True, slots=True)
class RunStarted:
    run_id: str


@dataclass(frozen=True, slots=True)
class CandidateSnapshotCreated:
    snapshot_id: str
    candidate_count: int


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
    | ToolRequested
    | ToolSucceeded
    | ToolRejected
    | RunStopped
    | RunFailed
)
