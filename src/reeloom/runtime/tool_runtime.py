from __future__ import annotations

from dataclasses import dataclass

from reeloom.runtime.budget import RunBudget
from reeloom.runtime.errors import (
    BudgetExceeded,
    RuntimeDomainError,
    RuntimeErrorCode,
)
from reeloom.runtime.events import (
    RunFailed,
    RunStopped,
    ToolRejected,
    ToolRequested,
    ToolSucceeded,
)
from reeloom.runtime.policy import PhaseToolPolicy
from reeloom.runtime.state import RunState, RunStatus, StopReason
from reeloom.runtime.store import EventStore


@dataclass(slots=True)
class ToolRuntime:
    """Enforces run policy before domain tool code is entered."""

    store: EventStore
    budget: RunBudget
    policy: PhaseToolPolicy

    @property
    def state(self) -> RunState:
        state = self.store.state
        if state is None:
            raise RuntimeDomainError(RuntimeErrorCode.RUN_NOT_ACTIVE)
        return state

    def begin(self, *, call_id: str, tool_name: str) -> None:
        state = self.state
        self._request(call_id=call_id, tool_name=tool_name)
        if (
            tool_name == "list_candidates"
            and state.candidate_snapshot_id is None
        ):
            self.reject(
                call_id=call_id,
                tool_name=tool_name,
                code=RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE.value,
                retryable=False,
            )
            raise RuntimeDomainError(
                RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE,
                context={"tool_name": tool_name},
            )
        if not self.policy.is_allowed(tool_name, state.phase):
            self.reject(
                call_id=call_id,
                tool_name=tool_name,
                code=RuntimeErrorCode.TOOL_NOT_ALLOWED.value,
                retryable=True,
            )
            raise RuntimeDomainError(
                RuntimeErrorCode.TOOL_NOT_ALLOWED,
                context={"tool_name": tool_name},
            )

    def record_rejection(
        self,
        *,
        call_id: str,
        tool_name: str,
        code: RuntimeErrorCode,
        retryable: bool,
    ) -> None:
        self._request(call_id=call_id, tool_name=tool_name)
        self.reject(
            call_id=call_id,
            tool_name=tool_name,
            code=code.value,
            retryable=retryable,
        )

    def _request(self, *, call_id: str, tool_name: str) -> None:
        state = self.state
        if state.tool_calls >= self.budget.max_tool_calls:
            self.store.append(RunStopped(reason=StopReason.BUDGET_EXHAUSTED))
            raise BudgetExceeded(RuntimeErrorCode.TOOL_BUDGET_EXHAUSTED)

        self.store.append(
            ToolRequested(call_id=call_id, tool_name=tool_name)
        )

    def succeed(self, *, call_id: str, tool_name: str) -> None:
        self.store.append(
            ToolSucceeded(call_id=call_id, tool_name=tool_name)
        )

    def reject(
        self,
        *,
        call_id: str,
        tool_name: str,
        code: str,
        retryable: bool,
    ) -> None:
        state = self.store.append(
            ToolRejected(
                call_id=call_id,
                tool_name=tool_name,
                code=code,
                retryable=retryable,
            )
        )
        if state.failures >= self.budget.max_failures:
            self.store.append(RunStopped(reason=StopReason.BUDGET_EXHAUSTED))
            raise BudgetExceeded(
                RuntimeErrorCode.FAILURE_BUDGET_EXHAUSTED
            )

    def fail(self, *, code: str) -> None:
        if self.state.status is RunStatus.RUNNING:
            self.store.append(RunFailed(code=code))
