from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


class RuntimeErrorCode(StrEnum):
    INVALID_EVENT = "invalid_event"
    INVALID_TRANSITION = "invalid_transition"
    RUN_ALREADY_STARTED = "run_already_started"
    RUN_NOT_ACTIVE = "run_not_active"
    RUN_ID_MISMATCH = "run_id_mismatch"
    DUPLICATE_TOOL_CALL = "duplicate_tool_call"
    UNKNOWN_TOOL_CALL = "unknown_tool_call"
    UNKNOWN_TOOL = "unknown_tool"
    TOOL_NOT_ALLOWED = "tool_not_allowed"
    INVALID_TOOL_ARGUMENTS = "invalid_tool_arguments"
    TOOL_BUDGET_EXHAUSTED = "tool_budget_exhausted"
    FAILURE_BUDGET_EXHAUSTED = "failure_budget_exhausted"
    AGENT_RUN_FAILED = "agent_run_failed"
    CAPABILITY_NOT_AVAILABLE = "capability_not_available"
    TMDB_CANDIDATE_LIMIT_EXCEEDED = "tmdb_candidate_limit_exceeded"
    UNKNOWN_TMDB_CANDIDATE = "unknown_tmdb_candidate"
    WORK_TYPE_NOT_AUTHORIZED = "work_type_not_authorized"
    UNSUPPORTED_WORK_TYPE = "unsupported_work_type"
    SERIES_IDENTITY_UNAVAILABLE = "series_identity_unavailable"
    TOOL_OBSERVATION_TOO_LARGE = "tool_observation_too_large"
    EPISODE_CATALOG_UNAVAILABLE = "episode_catalog_unavailable"
    INVENTORY_NOT_OBSERVED = "inventory_not_observed"
    SUBTITLE_SAMPLE_FAILED = "subtitle_sample_failed"
    PLAN_COMPILER_UNAVAILABLE = "plan_compiler_unavailable"
    PLAN_BUILD_FAILED = "plan_build_failed"
    TOKEN_BUDGET_EXHAUSTED = "token_budget_exhausted"
    TIME_BUDGET_EXHAUSTED = "time_budget_exhausted"


class RuntimeDomainError(RuntimeError):
    """A stable runtime failure with bounded, machine-readable context."""

    def __init__(
        self,
        code: RuntimeErrorCode,
        *,
        context: dict[str, object] | None = None,
    ) -> None:
        self.code = code
        self.context = MappingProxyType(dict(context or {}))
        super().__init__(code.value)


class BudgetExceeded(RuntimeDomainError):
    """Raised when the immutable run budget no longer permits work."""
