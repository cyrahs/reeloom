from __future__ import annotations

import pytest

from reeloom.kernel.naming import SeriesIdentity
from reeloom.kernel.tmdb import TmdbCandidateRef, TmdbWorkType
from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.events import (
    CandidateSnapshotCreated,
    RunFailed,
    RunStarted,
    RunStopped,
    SeriesSelected,
    TmdbCandidatesObserved,
    ToolRejected,
    ToolRequested,
    ToolSucceeded,
)
from reeloom.runtime.reducer import reduce_event
from reeloom.runtime.state import (
    MappingValidationIssue,
    Phase,
    RunStatus,
    StopReason,
)
from reeloom.runtime.store import InMemoryEventStore


def test_run_events_reduce_to_replayable_state() -> None:
    events = (
        RunStarted(run_id="run-1", work_type=TmdbWorkType.ANIME),
        CandidateSnapshotCreated(
            snapshot_id="candidate-snapshot-v1:abc",
            candidate_count=1,
        ),
        ToolRequested(call_id="call-1", tool_name="list_candidates"),
        ToolSucceeded(call_id="call-1", tool_name="list_candidates"),
        RunStopped(reason=StopReason.MODEL_FINAL),
    )

    state = None
    for event in events:
        state = reduce_event(state, event)

    assert state is not None
    assert state.phase is Phase.IDENTIFY_SERIES
    assert state.status is RunStatus.STOPPED
    assert state.stop_reason is StopReason.MODEL_FINAL
    assert state.tool_calls == 1
    assert state.failures == 0
    assert state.event_count == len(events)


def test_assistant_final_does_not_imply_domain_completion() -> None:
    state = reduce_event(
        None,
        RunStarted(run_id="run-1", work_type=TmdbWorkType.ANIME),
    )
    state = reduce_event(
        state,
        CandidateSnapshotCreated(
            snapshot_id="candidate-snapshot-v1:abc",
            candidate_count=1,
        ),
    )
    state = reduce_event(state, RunStopped(reason=StopReason.MODEL_FINAL))

    assert state.phase is Phase.IDENTIFY_SERIES
    assert state.phase is not Phase.COMPLETED


def test_stop_reason_cannot_claim_domain_completion() -> None:
    with pytest.raises(ValueError):
        StopReason("domain_completed")


def test_snapshot_event_binds_the_run_once_before_tool_calls() -> None:
    state = reduce_event(
        None,
        RunStarted(run_id="run-1", work_type=TmdbWorkType.ANIME),
    )
    state = reduce_event(
        state,
        CandidateSnapshotCreated(
            snapshot_id="candidate-snapshot-v1:abc",
            candidate_count=2,
        ),
    )

    assert state.candidate_snapshot_id == "candidate-snapshot-v1:abc"
    assert state.candidate_count == 2
    assert state.phase is Phase.IDENTIFY_SERIES

    with pytest.raises(RuntimeDomainError) as error:
        reduce_event(
            state,
            CandidateSnapshotCreated(
                snapshot_id="candidate-snapshot-v1:def",
                candidate_count=3,
            ),
        )

    assert error.value.code is RuntimeErrorCode.INVALID_TRANSITION


def test_tmdb_candidates_and_selection_advance_domain_phase() -> None:
    state = reduce_event(
        None,
        RunStarted(run_id="run-1", work_type=TmdbWorkType.ANIME),
    )
    state = reduce_event(
        state,
        CandidateSnapshotCreated(
            snapshot_id="candidate-snapshot-v1:abc",
            candidate_count=1,
        ),
    )
    state = reduce_event(
        state,
        TmdbCandidatesObserved(
            candidates=(
                TmdbCandidateRef(
                    work_type=TmdbWorkType.ANIME,
                    tmdb_id=100,
                ),
                TmdbCandidateRef(
                    work_type=TmdbWorkType.ANIME,
                    tmdb_id=200,
                ),
            )
        ),
    )
    state = reduce_event(
        state,
        SeriesSelected(
            series=SeriesIdentity(
                title_zh_cn="动画",
                year=2020,
                tmdb_id=200,
            ),
            work_type=TmdbWorkType.ANIME,
        ),
    )

    assert state.phase is Phase.MAP_EPISODES
    assert state.selected_series is not None
    assert state.selected_series.tmdb_id == 200


def test_non_candidate_tmdb_id_cannot_be_selected() -> None:
    state = reduce_event(
        None,
        RunStarted(run_id="run-1", work_type=TmdbWorkType.ANIME),
    )
    state = reduce_event(
        state,
        CandidateSnapshotCreated(
            snapshot_id="candidate-snapshot-v1:abc",
            candidate_count=1,
        ),
    )
    state = reduce_event(
        state,
        TmdbCandidatesObserved(
            candidates=(
                TmdbCandidateRef(
                    work_type=TmdbWorkType.ANIME,
                    tmdb_id=100,
                ),
            )
        ),
    )

    with pytest.raises(RuntimeDomainError) as error:
        reduce_event(
            state,
            SeriesSelected(
                series=SeriesIdentity(
                    title_zh_cn="动画",
                    year=2020,
                    tmdb_id=200,
                ),
                work_type=TmdbWorkType.ANIME,
            ),
        )

    assert error.value.code is RuntimeErrorCode.INVALID_TRANSITION


def test_run_rejects_candidate_from_another_work_type_namespace() -> None:
    state = reduce_event(
        None,
        RunStarted(run_id="run-1", work_type=TmdbWorkType.ANIME),
    )
    state = reduce_event(
        state,
        CandidateSnapshotCreated(
            snapshot_id="candidate-snapshot-v1:abc",
            candidate_count=1,
        ),
    )

    with pytest.raises(RuntimeDomainError) as error:
        reduce_event(
            state,
            TmdbCandidatesObserved(
                candidates=(
                    TmdbCandidateRef(
                        work_type=TmdbWorkType.MOVIE,
                        tmdb_id=100,
                    ),
                )
            ),
        )

    assert error.value.code is RuntimeErrorCode.INVALID_EVENT


def test_tool_result_requires_a_matching_request() -> None:
    state = reduce_event(
        None,
        RunStarted(run_id="run-1", work_type=TmdbWorkType.ANIME),
    )

    with pytest.raises(RuntimeDomainError) as error:
        reduce_event(
            state,
            ToolSucceeded(call_id="missing", tool_name="list_candidates"),
        )

    assert error.value.code is RuntimeErrorCode.UNKNOWN_TOOL_CALL


def test_tool_result_name_must_match_the_requested_tool() -> None:
    state = reduce_event(
        None,
        RunStarted(run_id="run-1", work_type=TmdbWorkType.ANIME),
    )
    state = reduce_event(
        state,
        ToolRequested(call_id="call-1", tool_name="list_candidates"),
    )

    with pytest.raises(RuntimeDomainError) as error:
        reduce_event(
            state,
            ToolSucceeded(call_id="call-1", tool_name="read_file"),
        )

    assert error.value.code is RuntimeErrorCode.UNKNOWN_TOOL_CALL


def test_stopping_with_pending_tool_call_fails_closed() -> None:
    state = reduce_event(
        None,
        RunStarted(run_id="run-1", work_type=TmdbWorkType.ANIME),
    )
    state = reduce_event(
        state,
        ToolRequested(call_id="call-1", tool_name="list_candidates"),
    )

    with pytest.raises(RuntimeDomainError) as error:
        reduce_event(state, RunStopped(reason=StopReason.MODEL_FINAL))

    assert error.value.code is RuntimeErrorCode.INVALID_TRANSITION


def test_terminal_failure_aborts_pending_tool_calls() -> None:
    state = reduce_event(
        None,
        RunStarted(run_id="run-1", work_type=TmdbWorkType.ANIME),
    )
    state = reduce_event(
        state,
        ToolRequested(call_id="call-1", tool_name="list_candidates"),
    )

    state = reduce_event(state, RunFailed(code="adapter_crashed"))

    assert state.status is RunStatus.FAILED
    assert state.pending_tool_calls == frozenset()
    assert state.failure_code == "adapter_crashed"


def test_store_is_append_only_and_replays_deterministically() -> None:
    store = InMemoryEventStore()
    store.append(
        RunStarted(run_id="run-1", work_type=TmdbWorkType.ANIME)
    )
    store.append(ToolRequested(call_id="call-1", tool_name="list_candidates"))
    store.append(
        ToolRejected(
            call_id="call-1",
            tool_name="list_candidates",
            code="temporary_failure",
            retryable=True,
        )
    )

    assert tuple(event.sequence for event in store.events) == (1, 2, 3)
    assert store.replay() == store.state
    assert store.state is not None
    assert store.state.failures == 1


def test_mapping_validation_issue_rejects_unbounded_text() -> None:
    with pytest.raises(ValueError):
        MappingValidationIssue(
            code="invalid_mapping",
            context=(("detail", "x" * 161),),
        )
