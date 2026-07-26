from __future__ import annotations

from dataclasses import dataclass

from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode


@dataclass(frozen=True, slots=True)
class RunBudget:
    """Immutable limits shared by the SDK runner and Reeloom tools."""

    max_model_turns: int = 64
    max_tool_calls: int = 64
    max_failures: int = 3
    max_total_tokens: int = 100_000
    max_elapsed_seconds: float = 60.0

    def __post_init__(self) -> None:
        for field_name in (
            "max_model_turns",
            "max_tool_calls",
            "max_failures",
            "max_total_tokens",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 1:
                raise RuntimeDomainError(
                    RuntimeErrorCode.INVALID_EVENT,
                    context={"field": field_name},
                )
        if (
            not isinstance(self.max_elapsed_seconds, (int, float))
            or isinstance(self.max_elapsed_seconds, bool)
            or not 0 < self.max_elapsed_seconds <= 3_600
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.INVALID_EVENT,
                context={"field": "max_elapsed_seconds"},
            )
