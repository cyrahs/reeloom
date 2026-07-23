from __future__ import annotations

import asyncio
import json

from reeloom.kernel.candidates import (
    Candidate,
    CandidateId,
    CandidateKind,
    CandidateSnapshot,
)
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.events import CandidateSnapshotCreated, RunStarted
from reeloom.runtime.policy import PhaseToolPolicy
from reeloom.runtime.store import InMemoryEventStore
from reeloom.runtime.tool_runtime import ToolRuntime
from reeloom.tools.candidates import SnapshotCandidateSource, list_candidates


def _source() -> SnapshotCandidateSource:
    return SnapshotCandidateSource(
        CandidateSnapshot.create(
            [
                Candidate(
                    id=CandidateId(CandidateKind.VIDEO, 1),
                    kind=CandidateKind.VIDEO,
                    display_name="episode.mkv",
                )
            ]
        ),
        snapshot_id="candidate-snapshot-v1:test",
    )


def _runtime(source: SnapshotCandidateSource) -> ToolRuntime:
    store = InMemoryEventStore()
    store.append(RunStarted(run_id="run-1"))
    store.append(
        CandidateSnapshotCreated(
            snapshot_id=source.snapshot_id,
            candidate_count=source.candidate_count,
        )
    )
    return ToolRuntime(
        store=store,
        budget=RunBudget(),
        policy=PhaseToolPolicy(),
    )


def test_list_candidates_returns_only_bounded_metadata() -> None:
    source = _source()
    raw = asyncio.run(
        list_candidates(
            _runtime(source),
            source,
            call_id="call-1",
            kind=CandidateKind.VIDEO,
            cursor=0,
            limit=10,
        )
    )

    assert json.loads(raw)["items"] == [
        {
            "display_name": "episode.mkv",
            "id": "video:1",
            "kind": "video",
        }
    ]
    assert '"path"' not in raw


def test_phase_policy_is_deny_by_default() -> None:
    policy = PhaseToolPolicy()

    assert policy.is_allowed(
        "list_candidates",
        _runtime(_source()).state.phase,
    )
    assert not policy.is_allowed(
        "read_file",
        _runtime(_source()).state.phase,
    )
