from datetime import UTC, datetime, timedelta
from dataclasses import replace
from pathlib import PurePosixPath

import pytest

from reeloom.kernel.candidates import (
    Candidate,
    CandidateId,
    CandidateKind,
    CandidateSnapshot,
)
from reeloom.kernel.archive_directory import (
    ArchiveDirectoryCapability,
    ArchiveDirectoryListing,
    ArchiveSearchRecord,
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
    ToolRejected,
    ToolRequested,
    ToolSucceeded,
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


def test_directory_observations_round_trip_in_v3_projection() -> None:
    state = reduce_event(
        None,
        RunStarted("run-directory", TmdbWorkType.ANIME),
    )
    capability = ArchiveDirectoryCapability(
        "run-directory",
        "dir-1",
        None,
        PurePosixPath("旧项目"),
        "旧项目",
        1,
        1,
        2,
        3,
        4,
    )
    observed_at = datetime(2026, 7, 28, tzinfo=UTC)
    state = replace(
        state,
        archive_directory_capabilities=(capability,),
        archive_searches=(
            ArchiveSearchRecord(
                "search",
                "name",
                "旧项目",
                42,
                TmdbWorkType.ANIME,
                ("dir-1",),
                0,
                None,
                True,
                observed_at,
            ),
        ),
        archive_directory_listings=(
            ArchiveDirectoryListing(
                "list",
                "dir-1",
                (),
                ("旧项目 S01E01.mkv",),
                ((1, 1),),
                0,
                None,
                True,
                observed_at,
            ),
        ),
    )

    assert decode_state(
        encode_state(state),
        load_plan=lambda _plan_hash: pytest.fail(),
    ) == state


def test_v3_projection_rejects_coerced_archive_text() -> None:
    state = reduce_event(
        None,
        RunStarted("run-directory", TmdbWorkType.ANIME),
    )
    payload = encode_state(
        replace(
            state,
            archive_directory_capabilities=(
                ArchiveDirectoryCapability(
                    "run-directory",
                    "dir-1",
                    None,
                    PurePosixPath("旧项目"),
                    "旧项目",
                    1,
                    1,
                    2,
                    3,
                    4,
                ),
            ),
        )
    )
    payload["archive_directory_capabilities"][0]["name"] = 42

    with pytest.raises(ValueError):
        decode_state(
            payload,
            load_plan=lambda _plan_hash: pytest.fail(),
        )


def test_retryable_directory_failure_survives_projection_until_success() -> None:
    state = reduce_event(
        None,
        RunStarted("run-directory", TmdbWorkType.ANIME),
    )
    state = reduce_event(
        state,
        ToolRequested("search-1", "search_dir"),
    )
    state = reduce_event(
        state,
        ToolRejected(
            "search-1",
            "search_dir",
            "directory_io_timeout",
            True,
        ),
    )

    state = decode_state(
        encode_state(state),
        load_plan=lambda _plan_hash: pytest.fail(),
    )
    assert state.retryable_directory_failure

    state = reduce_event(
        state,
        ToolRequested("search-2", "search_dir"),
    )
    state = reduce_event(
        state,
        ToolSucceeded("search-2", "search_dir"),
    )
    assert not state.retryable_directory_failure


def test_legacy_episode_projection_remains_readable() -> None:
    state = reduce_event(
        None,
        RunStarted("run-legacy", TmdbWorkType.ANIME),
    )
    payload = encode_state(state)
    payload.pop("movie_mapping_draft")
    payload.pop("selected_movie")
    payload.pop("archive_directory_capabilities")
    payload.pop("archive_searches")
    payload.pop("archive_directory_listings")
    payload.pop("retryable_directory_failure")

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
