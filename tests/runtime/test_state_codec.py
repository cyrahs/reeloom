from datetime import UTC, datetime, timedelta

import pytest

from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.events import (
    CandidateSnapshotCreated,
    ModelUsageRecorded,
    RunStarted,
)
from reeloom.runtime.reducer import reduce_event
from reeloom.runtime.state_codec import decode_state, encode_state


def test_run_state_projection_round_trips_without_event_history() -> None:
    deadline = datetime.now(UTC) + timedelta(minutes=5)
    state = reduce_event(
        None,
        RunStarted(
            "run-1",
            TmdbWorkType.ANIME,
            RunBudget(max_total_tokens=1234),
            deadline,
        ),
    )
    state = reduce_event(
        state,
        CandidateSnapshotCreated("snapshot:1", 0),
    )
    state = reduce_event(state, ModelUsageRecorded(2, 3, 5))

    recovered = decode_state(
        encode_state(state),
        load_plan=lambda _plan_hash: pytest.fail(
            "projection has no plan reference"
        ),
    )

    assert recovered == state


def test_run_state_projection_rejects_unknown_fields() -> None:
    state = reduce_event(
        None,
        RunStarted("run-1", TmdbWorkType.ANIME),
    )
    payload = encode_state(state)
    payload["unexpected"] = "value"

    with pytest.raises(Exception):
        decode_state(payload, load_plan=lambda _plan_hash: pytest.fail())
