from __future__ import annotations

import asyncio

from reeloom.agents.organizer import (
    create_organizer_context,
    run_episode_organizer,
)
from reeloom.agents.scripted_model import (
    FinalStep,
    ScriptedModel,
    ToolCallStep,
)
from reeloom.kernel.candidates import CandidateSnapshot
from reeloom.runtime.state import Phase, RunStatus, StopReason
from reeloom.tools.candidates import SnapshotCandidateSource


def _source() -> SnapshotCandidateSource:
    return SnapshotCandidateSource(
        CandidateSnapshot.create([]),
        snapshot_id="candidate-snapshot-v1:test",
    )


def test_sdk_runner_completes_a_multi_turn_tool_loop() -> None:
    model = ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id="call-1",
            ),
            FinalStep(text="done", expect_input_contains='"items":[]'),
        )
    )
    context = create_organizer_context(
        run_id="run-1",
        candidate_source=_source(),
    )

    result = asyncio.run(
        run_episode_organizer(
            context=context,
            model=model,
            prompt="Inspect candidates.",
        )
    )

    assert result.final_output == "done"
    assert result.state.status is RunStatus.STOPPED
    assert result.state.stop_reason is StopReason.MODEL_FINAL
    assert result.state.phase is Phase.IDENTIFY_SERIES
    assert result.model_turns == 2


def test_unknown_tool_is_a_structured_observation() -> None:
    model = ScriptedModel(
        (
            ToolCallStep(
                name="read_file",
                arguments={"path": "/etc/passwd"},
                call_id="call-unknown",
            ),
            FinalStep(text="rejected", expect_input_contains="unknown_tool"),
        )
    )
    context = create_organizer_context(
        run_id="run-1",
        candidate_source=_source(),
    )

    result = asyncio.run(
        run_episode_organizer(
            context=context,
            model=model,
            prompt="Inspect candidates.",
        )
    )

    assert result.state.failures == 1
