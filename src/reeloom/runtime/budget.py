from __future__ import annotations

from dataclasses import dataclass

from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode


@dataclass(frozen=True, slots=True)
class RunBudget:
    """Immutable limits shared by the SDK runner and Reeloom tools."""

    max_model_turns: int = 8
    max_tool_calls: int = 12
    max_failures: int = 3

    def __post_init__(self) -> None:
        for field_name in (
            "max_model_turns",
            "max_tool_calls",
            "max_failures",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 1:
                raise RuntimeDomainError(
                    RuntimeErrorCode.INVALID_EVENT,
                    context={"field": field_name},
                )
