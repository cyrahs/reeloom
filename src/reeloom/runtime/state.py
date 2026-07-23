from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

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
    DOMAIN_COMPLETED = "domain_completed"
    MAX_TURNS = "max_turns"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FATAL_ERROR = "fatal_error"


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
    candidate_snapshot_id: str | None = None
    candidate_count: int = 0
    stop_reason: StopReason | None = None
    failure_code: str | None = None
