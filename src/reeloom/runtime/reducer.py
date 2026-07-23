from __future__ import annotations

from dataclasses import replace

from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.events import (
    CandidateSnapshotCreated,
    RunFailed,
    RunStarted,
    RunStopped,
    RuntimeEvent,
    ToolRejected,
    ToolRequested,
    ToolSucceeded,
)
from reeloom.runtime.state import Phase, RunState, RunStatus, StopReason


def _require_running(state: RunState) -> None:
    if state.status is not RunStatus.RUNNING:
        raise RuntimeDomainError(RuntimeErrorCode.RUN_NOT_ACTIVE)


def _complete_tool_call(
    state: RunState,
    *,
    call_id: str,
    tool_name: str,
) -> frozenset[tuple[str, str]]:
    pending_call = (call_id, tool_name)
    if pending_call not in state.pending_tool_calls:
        raise RuntimeDomainError(
            RuntimeErrorCode.UNKNOWN_TOOL_CALL,
            context={"call_id": call_id, "tool_name": tool_name},
        )
    return state.pending_tool_calls - {pending_call}


def reduce_event(
    state: RunState | None,
    event: RuntimeEvent,
) -> RunState:
    """Apply one event without I/O; replaying the same events gives the same state."""

    if isinstance(event, RunStarted):
        if state is not None:
            raise RuntimeDomainError(RuntimeErrorCode.RUN_ALREADY_STARTED)
        if not event.run_id:
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        return RunState(
            run_id=event.run_id,
            phase=Phase.BOOTSTRAP,
            status=RunStatus.RUNNING,
            event_count=1,
            tool_calls=0,
            failures=0,
            pending_tool_calls=frozenset(),
        )

    if state is None:
        raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)

    _require_running(state)
    event_count = state.event_count + 1

    if isinstance(event, CandidateSnapshotCreated):
        if (
            state.candidate_snapshot_id is not None
            or state.tool_calls != 0
            or state.pending_tool_calls
            or state.phase is not Phase.BOOTSTRAP
            or not event.snapshot_id
            or type(event.candidate_count) is not int
            or event.candidate_count < 0
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
        return replace(
            state,
            phase=Phase.IDENTIFY_SERIES,
            event_count=event_count,
            candidate_snapshot_id=event.snapshot_id,
            candidate_count=event.candidate_count,
        )

    if isinstance(event, ToolRequested):
        if not event.call_id or not event.tool_name:
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        if any(
            call_id == event.call_id
            for call_id, _ in state.pending_tool_calls
        ):
            raise RuntimeDomainError(
                RuntimeErrorCode.DUPLICATE_TOOL_CALL,
                context={"call_id": event.call_id},
            )
        return replace(
            state,
            event_count=event_count,
            tool_calls=state.tool_calls + 1,
            pending_tool_calls=state.pending_tool_calls
            | {(event.call_id, event.tool_name)},
        )

    if isinstance(event, ToolSucceeded):
        pending = _complete_tool_call(
            state,
            call_id=event.call_id,
            tool_name=event.tool_name,
        )
        return replace(
            state,
            event_count=event_count,
            pending_tool_calls=pending,
        )

    if isinstance(event, ToolRejected):
        pending = _complete_tool_call(
            state,
            call_id=event.call_id,
            tool_name=event.tool_name,
        )
        return replace(
            state,
            event_count=event_count,
            failures=state.failures + 1,
            pending_tool_calls=pending,
        )

    if isinstance(event, RunStopped):
        if state.pending_tool_calls:
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
        phase = (
            Phase.COMPLETED
            if event.reason is StopReason.DOMAIN_COMPLETED
            else state.phase
        )
        return replace(
            state,
            phase=phase,
            status=RunStatus.STOPPED,
            event_count=event_count,
            stop_reason=event.reason,
        )

    if isinstance(event, RunFailed):
        if state.pending_tool_calls:
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
        if not event.code:
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        return replace(
            state,
            phase=Phase.FAILED,
            status=RunStatus.FAILED,
            event_count=event_count,
            stop_reason=StopReason.FATAL_ERROR,
            failure_code=event.code,
        )

    raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
