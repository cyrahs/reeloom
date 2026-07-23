from __future__ import annotations

from dataclasses import replace

from reeloom.kernel.naming import SeriesIdentity
from reeloom.kernel.tmdb import TmdbCandidateRef, TmdbWorkType
from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.events import (
    CandidateSnapshotCreated,
    RunFailed,
    RunStarted,
    RunStopped,
    RuntimeEvent,
    SeriesSelected,
    TmdbCandidatesObserved,
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
        if (
            not event.run_id
            or not isinstance(event.work_type, TmdbWorkType)
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        return RunState(
            run_id=event.run_id,
            phase=Phase.BOOTSTRAP,
            status=RunStatus.RUNNING,
            event_count=1,
            tool_calls=0,
            failures=0,
            pending_tool_calls=frozenset(),
            work_type=event.work_type,
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

    if isinstance(event, TmdbCandidatesObserved):
        if (
            state.phase is not Phase.IDENTIFY_SERIES
            or not isinstance(event.candidates, tuple)
            or len(event.candidates) > 20
            or len(set(event.candidates)) != len(event.candidates)
            or any(
                not isinstance(candidate, TmdbCandidateRef)
                or candidate.work_type is not state.work_type
                for candidate in event.candidates
            )
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_EVENT)
        candidates = state.tmdb_candidates | frozenset(
            event.candidates
        )
        if len(candidates) > 200:
            raise RuntimeDomainError(
                RuntimeErrorCode.TMDB_CANDIDATE_LIMIT_EXCEEDED
            )
        return replace(
            state,
            event_count=event_count,
            tmdb_candidates=candidates,
        )

    if isinstance(event, SeriesSelected):
        series = event.series
        if (
            state.phase is not Phase.IDENTIFY_SERIES
            or state.selected_series is not None
            or not isinstance(series, SeriesIdentity)
            or not isinstance(event.work_type, TmdbWorkType)
            or event.work_type is not state.work_type
            or TmdbCandidateRef(
                work_type=event.work_type,
                tmdb_id=series.tmdb_id,
            )
            not in state.tmdb_candidates
        ):
            raise RuntimeDomainError(RuntimeErrorCode.INVALID_TRANSITION)
        return replace(
            state,
            phase=Phase.MAP_EPISODES,
            event_count=event_count,
            selected_series=series,
            selected_work_type=event.work_type,
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
