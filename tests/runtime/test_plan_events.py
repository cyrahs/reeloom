from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.naming import SeriesIdentity
from reeloom.kernel.rename_plan import (
    RenamePlan,
    RootBinding,
    compile_plan_draft,
)
from reeloom.kernel.scanner import ScannedFile, build_candidate_snapshot
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.events import (
    ApprovalRequested,
    CandidateSnapshotCreated,
    PlanBuilt,
    RunStarted,
    RunStopped,
)
from reeloom.runtime.reducer import reduce_event
from reeloom.runtime.state import Phase, RunStatus, StopReason


def _plan_and_state():
    snapshot = build_candidate_snapshot(
        (
            ScannedFile(
                relative_path=PurePosixPath("episode.mkv"),
                kind=CandidateKind.VIDEO,
                size_bytes=5,
                device=1,
                inode=2,
                mtime_ns=3,
                ctime_ns=4,
            ),
        )
    )
    series = SeriesIdentity("动画", 2024, 200)
    mapping = MappingDraft.from_dict(
        {
            "videos": [
                {
                    "video_id": "video:1",
                    "season": 1,
                    "episode_start": 1,
                    "episode_end": 1,
                }
            ],
            "subtitles": [],
        },
        candidates=snapshot.candidates,
        catalog=EpisodeCatalog.from_counts({1: 1}),
    )
    draft = compile_plan_draft(
        series=series,
        mapping=mapping,
        candidates=snapshot,
        subtitle_variants=(),
    )
    plan = RenamePlan.create(
        run_id="run-m5",
        work_type=TmdbWorkType.ANIME,
        created_at=datetime(2026, 7, 23, tzinfo=UTC),
        source_root=RootBinding(PurePosixPath("/incoming"), 1, 10),
        output_root=RootBinding(PurePosixPath("/anime"), 1, 11),
        candidate_snapshot=snapshot,
        subtitle_variants=(),
        draft=draft,
        checked_destinations=tuple(
            move.destination for move in draft.moves
        ),
    )
    state = reduce_event(
        None,
        RunStarted(run_id="run-m5", work_type=TmdbWorkType.ANIME),
    )
    state = reduce_event(
        state,
        CandidateSnapshotCreated(
            snapshot_id=snapshot.snapshot_id,
            candidate_count=1,
            candidate_ids=(
                snapshot.records[0].candidate.id,
            ),
            source_root=plan.source_root,
            output_root=plan.output_root,
        ),
    )
    return plan, replace(
        state,
        phase=Phase.BUILD_PLAN,
        selected_series=series,
        selected_work_type=TmdbWorkType.ANIME,
        mapping_draft=mapping,
    )


def test_plan_events_reach_a_stopped_approval_boundary() -> None:
    plan, state = _plan_and_state()

    state = reduce_event(state, PlanBuilt(plan=plan))
    assert state.phase is Phase.BUILD_PLAN
    assert state.rename_plan == plan
    assert state.plan_hash == plan.plan_hash

    state = reduce_event(
        state,
        ApprovalRequested(plan_hash=plan.plan_hash),
    )
    assert state.phase is Phase.AWAITING_APPROVAL

    state = reduce_event(
        state,
        RunStopped(reason=StopReason.AWAITING_APPROVAL),
    )
    assert state.status is RunStatus.STOPPED
    assert state.stop_reason is StopReason.AWAITING_APPROVAL


def test_build_plan_cannot_stop_on_plain_model_text() -> None:
    _, state = _plan_and_state()

    with pytest.raises(RuntimeDomainError) as raised:
        reduce_event(
            state,
            RunStopped(reason=StopReason.MODEL_FINAL),
        )

    assert raised.value.code is RuntimeErrorCode.INVALID_TRANSITION


def test_approval_request_must_bind_the_exact_plan_hash() -> None:
    plan, state = _plan_and_state()
    state = reduce_event(state, PlanBuilt(plan=plan))

    with pytest.raises(RuntimeDomainError) as raised:
        reduce_event(
            state,
            ApprovalRequested(plan_hash="sha256:" + "0" * 64),
        )

    assert raised.value.code is RuntimeErrorCode.INVALID_TRANSITION


def test_plan_event_rejects_a_tampered_plan_hash() -> None:
    plan, state = _plan_and_state()
    object.__setattr__(plan, "plan_hash", "sha256:" + "0" * 64)

    with pytest.raises(RuntimeDomainError) as raised:
        reduce_event(state, PlanBuilt(plan=plan))

    assert raised.value.code is RuntimeErrorCode.INVALID_TRANSITION


def test_plan_event_must_match_the_bootstrap_root_capabilities() -> None:
    plan, state = _plan_and_state()
    state = replace(
        state,
        authorized_output_root=RootBinding(
            PurePosixPath("/different-output"),
            1,
            99,
        ),
    )

    with pytest.raises(RuntimeDomainError) as raised:
        reduce_event(state, PlanBuilt(plan=plan))

    assert raised.value.code is RuntimeErrorCode.INVALID_TRANSITION
