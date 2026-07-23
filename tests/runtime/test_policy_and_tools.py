from __future__ import annotations

import asyncio
import json

import pytest

from reeloom.kernel.candidates import (
    Candidate,
    CandidateId,
    CandidateKind,
    CandidateSnapshot,
)
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.errors import BudgetExceeded, RuntimeErrorCode
from reeloom.runtime.events import CandidateSnapshotCreated, RunStarted
from reeloom.runtime.policy import PhaseToolPolicy
from reeloom.runtime.state import Phase, RunStatus, StopReason
from reeloom.runtime.store import InMemoryEventStore
from reeloom.runtime.tool_runtime import ToolRuntime
from reeloom.tools.candidates import (
    CandidateSource,
    CandidatePage,
    SnapshotCandidateSource,
    ToolExecutionError,
    ToolFailureCode,
    list_candidates,
)


def _runtime(
    *,
    source: CandidateSource | None = None,
    budget: RunBudget | None = None,
) -> ToolRuntime:
    bound_source = source or _source()
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
    return ToolRuntime(
        store=store,
        budget=budget or RunBudget(),
        policy=PhaseToolPolicy(),
    )


def _source() -> SnapshotCandidateSource:
    candidate = Candidate(
        id=CandidateId(kind=CandidateKind.VIDEO, ordinal=1),
        kind=CandidateKind.VIDEO,
        display_name="episode.mkv",
    )
    return SnapshotCandidateSource(CandidateSnapshot.create([candidate]))


def test_phase_policy_is_deny_by_default() -> None:
    policy = PhaseToolPolicy()

    assert policy.is_allowed("list_candidates", Phase.IDENTIFY_SERIES)
    assert policy.is_allowed("list_candidates", Phase.MAP_EPISODES)
    assert policy.is_allowed("search_tmdb", Phase.IDENTIFY_SERIES)
    assert not policy.is_allowed("search_tmdb", Phase.MAP_EPISODES)
    assert not policy.is_allowed(
        "get_tmdb_season",
        Phase.IDENTIFY_SERIES,
    )
    assert policy.is_allowed("get_tmdb_season", Phase.MAP_EPISODES)
    assert not policy.is_allowed("unknown_tool", Phase.IDENTIFY_SERIES)


def test_list_candidates_returns_only_bounded_snapshot_metadata() -> None:
    runtime = _runtime()

    raw = asyncio.run(
        list_candidates(
            runtime,
            _source(),
            call_id="call-1",
            kind=CandidateKind.VIDEO,
            cursor=0,
            limit=10,
        )
    )

    assert json.loads(raw) == {
        "ok": True,
        "items": [
            {
                "display_name": "episode.mkv",
                "id": "video:1",
                "kind": "video",
            }
        ],
        "next_cursor": None,
    }
    assert '"path"' not in raw
    assert runtime.state.tool_calls == 1
    assert runtime.state.failures == 0


class _FailingSource:
    def __init__(self, *, retryable: bool) -> None:
        self.retryable = retryable

    snapshot_id = "candidate-snapshot-v1:failing"
    candidate_count = 0

    async def page(
        self,
        *,
        kind: CandidateKind,
        cursor: int,
        limit: int,
    ) -> CandidatePage:
        del kind, cursor, limit
        raise ToolExecutionError(
            ToolFailureCode.TEMPORARY_UNAVAILABLE
            if self.retryable
            else ToolFailureCode.SOURCE_FAILURE,
            retryable=self.retryable,
        )


def test_retryable_tool_failure_becomes_structured_observation() -> None:
    source = _FailingSource(retryable=True)
    runtime = _runtime(source=source)

    raw = asyncio.run(
        list_candidates(
            runtime,
            source,
            call_id="call-1",
            kind=CandidateKind.VIDEO,
            cursor=0,
            limit=10,
        )
    )

    assert json.loads(raw) == {
        "ok": False,
        "error": {
            "code": ToolFailureCode.TEMPORARY_UNAVAILABLE.value,
            "retryable": True,
        },
    }
    assert runtime.state.status is RunStatus.RUNNING
    assert runtime.state.failures == 1


def test_fatal_tool_failure_stops_the_domain_run() -> None:
    source = _FailingSource(retryable=False)
    runtime = _runtime(source=source)

    with pytest.raises(ToolExecutionError):
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

    assert runtime.state.status is RunStatus.FAILED
    assert runtime.state.stop_reason is StopReason.FATAL_ERROR


def test_repeated_failures_exhaust_budget() -> None:
    source = _FailingSource(retryable=True)
    runtime = _runtime(
        source=source,
        budget=RunBudget(
            max_model_turns=8,
            max_tool_calls=5,
            max_failures=2,
        )
    )
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
    with pytest.raises(BudgetExceeded) as error:
        asyncio.run(
            list_candidates(
                runtime,
                source,
                call_id="call-2",
                kind=CandidateKind.VIDEO,
                cursor=0,
                limit=10,
            )
        )

    assert error.value.code is RuntimeErrorCode.FAILURE_BUDGET_EXHAUSTED
    assert runtime.state.status is RunStatus.STOPPED
    assert runtime.state.stop_reason is StopReason.BUDGET_EXHAUSTED


def test_tool_call_budget_is_checked_before_dispatch() -> None:
    runtime = _runtime(
        budget=RunBudget(
            max_model_turns=8,
            max_tool_calls=1,
            max_failures=3,
        )
    )
    source = _source()
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

    with pytest.raises(BudgetExceeded) as error:
        asyncio.run(
            list_candidates(
                runtime,
                source,
                call_id="call-2",
                kind=CandidateKind.VIDEO,
                cursor=0,
                limit=10,
            )
        )

    assert error.value.code is RuntimeErrorCode.TOOL_BUDGET_EXHAUSTED
    assert runtime.state.tool_calls == 1
