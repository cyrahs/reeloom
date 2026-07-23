from __future__ import annotations

import asyncio
import json

from reeloom.kernel.candidates import (
    Candidate,
    CandidateId,
    CandidateKind,
    CandidateSnapshot,
)
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.events import CandidateSnapshotCreated, RunStarted
from reeloom.runtime.policy import PhaseToolPolicy
from reeloom.runtime.store import InMemoryEventStore
from reeloom.runtime.tool_runtime import ToolRuntime
from reeloom.tools.candidates import SnapshotCandidateSource, list_candidates


def test_candidate_tool_requires_snapshot_event_binding() -> None:
    store = InMemoryEventStore()
    store.append(
        RunStarted(run_id="run-1", work_type=TmdbWorkType.ANIME)
    )
    runtime = ToolRuntime(
        store=store,
        budget=RunBudget(),
        policy=PhaseToolPolicy(),
    )
    source = SnapshotCandidateSource(CandidateSnapshot.create([]))

    result = json.loads(
        asyncio.run(
            list_candidates(
                runtime,
                source,
                call_id="call-1",
                kind=CandidateKind.VIDEO,
                cursor=0,
                limit=10,
            )
        )
    )

    assert result == {
        "ok": False,
        "error": {
            "code": "capability_not_available",
            "retryable": False,
        },
    }


def test_candidate_tool_rejects_a_different_snapshot_source() -> None:
    bound_source = SnapshotCandidateSource(CandidateSnapshot.create([]))
    other_source = SnapshotCandidateSource(
        CandidateSnapshot.create(
            [
                Candidate(
                    id=CandidateId(CandidateKind.VIDEO, 1),
                    kind=CandidateKind.VIDEO,
                    display_name="outside-bound-snapshot.mkv",
                )
            ]
        )
    )
    store = InMemoryEventStore()
    store.append(
        RunStarted(run_id="run-1", work_type=TmdbWorkType.ANIME)
    )
    store.append(
        CandidateSnapshotCreated(
            snapshot_id=bound_source.snapshot_id,
            candidate_count=bound_source.candidate_count,
        )
    )
    runtime = ToolRuntime(
        store=store,
        budget=RunBudget(),
        policy=PhaseToolPolicy(),
    )

    result = json.loads(
        asyncio.run(
            list_candidates(
                runtime,
                other_source,
                call_id="call-1",
                kind=CandidateKind.VIDEO,
                cursor=0,
                limit=10,
            )
        )
    )

    assert result == {
        "ok": False,
        "error": {
            "code": "capability_not_available",
            "retryable": False,
        },
    }
