from __future__ import annotations

import pytest

from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.events import (
    CandidateSnapshotCreated,
    RunStarted,
    RunStopped,
    ToolRequested,
    ToolSucceeded,
)
from reeloom.runtime.reducer import reduce_event
from reeloom.runtime.state import Phase, RunStatus, StopReason


def _identified_state():
    state = reduce_event(None, RunStarted(run_id="run-1"))
    return reduce_event(
        state,
        CandidateSnapshotCreated(
            snapshot_id="candidate-snapshot-v1:test",
            candidate_count=1,
        ),
    )


def test_run_events_reduce_to_replayable_state() -> None:
    state = _identified_state()
    state = reduce_event(
        state,
        ToolRequested(call_id="call-1", tool_name="list_candidates"),
    )
    state = reduce_event(
        state,
        ToolSucceeded(call_id="call-1", tool_name="list_candidates"),
    )
    state = reduce_event(state, RunStopped(reason=StopReason.MODEL_FINAL))

    assert state.phase is Phase.IDENTIFY_SERIES
    assert state.status is RunStatus.STOPPED
    assert state.tool_calls == 1


def test_assistant_final_does_not_imply_domain_completion() -> None:
    state = reduce_event(
        _identified_state(),
        RunStopped(reason=StopReason.MODEL_FINAL),
    )

    assert state.phase is Phase.IDENTIFY_SERIES


def test_tool_result_requires_matching_request() -> None:
    with pytest.raises(RuntimeDomainError) as error:
        reduce_event(
            _identified_state(),
            ToolSucceeded(call_id="missing", tool_name="list_candidates"),
        )

    assert error.value.code is RuntimeErrorCode.UNKNOWN_TOOL_CALL


def test_stopping_with_pending_call_fails_closed() -> None:
    state = reduce_event(
        _identified_state(),
        ToolRequested(call_id="call-1", tool_name="list_candidates"),
    )

    with pytest.raises(RuntimeDomainError) as error:
        reduce_event(state, RunStopped(reason=StopReason.MODEL_FINAL))

    assert error.value.code is RuntimeErrorCode.INVALID_TRANSITION
