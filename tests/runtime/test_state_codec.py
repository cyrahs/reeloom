from datetime import UTC, datetime, timedelta

import pytest

from reeloom.kernel.candidates import (
    Candidate,
    CandidateId,
    CandidateKind,
    CandidateSnapshot,
)
from reeloom.kernel.movie import MovieMappingDraft
from reeloom.kernel.naming import MovieIdentity
from reeloom.kernel.tmdb import TmdbCandidateRef, TmdbWorkType
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.events import (
    CandidateSnapshotCreated,
    ModelUsageRecorded,
    MovieMappingSubmitted,
    MovieSelected,
    RunStarted,
    TmdbCandidatesObserved,
    ToolRequested,
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


def test_legacy_episode_projection_remains_readable() -> None:
    state = reduce_event(
        None,
        RunStarted("run-legacy", TmdbWorkType.ANIME),
    )
    payload = encode_state(state)
    payload.pop("movie_mapping_draft")
    payload.pop("selected_movie")

    assert decode_state(
        payload,
        load_plan=lambda _plan_hash: pytest.fail(),
    ) == state


def test_movie_run_state_projection_round_trips() -> None:
    video_id = CandidateId(CandidateKind.VIDEO, 1)
    state = reduce_event(
        None,
        RunStarted("run-movie", TmdbWorkType.MOVIE),
    )
    state = reduce_event(
        state,
        CandidateSnapshotCreated(
            "snapshot:movie",
            1,
            (video_id,),
        ),
    )
    state = reduce_event(
        state,
        TmdbCandidatesObserved(
            (TmdbCandidateRef(TmdbWorkType.MOVIE, 42),)
        ),
    )
    state = reduce_event(
        state,
        MovieSelected(
            MovieIdentity("测试电影", 2024, 42),
            TmdbWorkType.MOVIE,
        ),
    )
    state = reduce_event(
        state,
        ToolRequested("mapping", "submit_mapping"),
    )
    state = reduce_event(
        state,
        MovieMappingSubmitted(
            "mapping",
            "snapshot:movie",
            MovieMappingDraft.create(
                video_id=video_id,
                subtitle_ids=(),
                candidates=CandidateSnapshot.create(
                    (
                        Candidate(
                            video_id,
                            CandidateKind.VIDEO,
                            "video:1",
                        ),
                    )
                ),
            ),
        ),
    )

    recovered = decode_state(
        encode_state(state),
        load_plan=lambda _plan_hash: pytest.fail(),
    )

    assert recovered == state
