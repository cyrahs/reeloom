from __future__ import annotations

import asyncio
import json

import pytest
from agents import MaxTurnsExceeded, UserError

from reeloom.agents.organizer import (
    create_organizer_context,
    run_episode_organizer,
)
from reeloom.agents.scripted_model import (
    FinalStep,
    ScriptedModel,
    ToolCallStep,
)
from reeloom.kernel.candidates import (
    Candidate,
    CandidateId,
    CandidateKind,
    CandidateSnapshot,
)
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.errors import RuntimeErrorCode
from reeloom.runtime.events import ToolRejected
from reeloom.runtime.policy import PhaseToolPolicy
from reeloom.runtime.state import Phase, RunStatus, StopReason
from reeloom.tools.candidates import (
    CandidatePage,
    SnapshotCandidateSource,
    ToolExecutionError,
    ToolFailureCode,
)


def _source() -> SnapshotCandidateSource:
    return SnapshotCandidateSource(
        CandidateSnapshot.create(
            [
                Candidate(
                    id=CandidateId(
                        kind=CandidateKind.VIDEO,
                        ordinal=1,
                    ),
                    kind=CandidateKind.VIDEO,
                    display_name="untrusted.mkv",
                )
            ]
        )
    )


def _run(
    model: ScriptedModel,
    *,
    budget: RunBudget | None = None,
):
    context = create_organizer_context(
        run_id="run-1",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
        budget=budget,
    )
    result = asyncio.run(
        run_episode_organizer(
            context=context,
            model=model,
            prompt="Inspect the candidates.",
        )
    )
    return context, result


def test_sdk_runner_completes_a_multi_turn_tool_loop() -> None:
    model = ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id="call-1",
            ),
            FinalStep(
                text="I found one candidate.",
                expect_input_contains="video:1",
            ),
        )
    )

    context, result = _run(model)

    assert result.final_output == "I found one candidate."
    assert result.model_turns == 2
    assert result.state.status is RunStatus.STOPPED
    assert result.state.stop_reason is StopReason.MODEL_FINAL
    assert result.state.phase is Phase.IDENTIFY_SERIES
    assert result.state.tool_calls == 1
    assert model.exhausted
    assert context.runtime.store.replay() == result.state


def test_unknown_tool_is_a_structured_observation() -> None:
    model = ScriptedModel(
        (
            ToolCallStep(
                name="read_file",
                arguments={"path": "/etc/passwd"},
                call_id="call-unknown",
            ),
            FinalStep(
                text="The tool was rejected.",
                expect_input_contains=RuntimeErrorCode.UNKNOWN_TOOL.value,
            ),
        )
    )

    context, result = _run(model)

    rejection = next(
        stored.event
        for stored in context.runtime.store.events
        if isinstance(stored.event, ToolRejected)
    )
    assert rejection.tool_name == "read_file"
    assert rejection.code == RuntimeErrorCode.UNKNOWN_TOOL.value
    assert result.state.failures == 1


def test_phase_disallowed_tool_is_a_structured_observation() -> None:
    model = ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id="call-disallowed",
            ),
            FinalStep(
                text="The tool is not valid in this phase.",
                expect_input_contains=RuntimeErrorCode.TOOL_NOT_ALLOWED.value,
            ),
        )
    )
    context = create_organizer_context(
        run_id="run-1",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
    )
    context.runtime.policy = PhaseToolPolicy(rules={})

    result = asyncio.run(
        run_episode_organizer(
            context=context,
            model=model,
            prompt="Inspect the candidates.",
        )
    )

    assert result.state.failures == 1
    assert result.state.stop_reason is StopReason.MODEL_FINAL


@pytest.mark.parametrize(
    "arguments",
    (
        {"kind": "video", "cursor": 0, "limit": 0},
        {
            "kind": "video",
            "cursor": 0,
            "limit": 10,
            "path": "/tmp",
        },
        {"kind": [], "cursor": 0, "limit": 10},
        "{not-json",
    ),
)
def test_invalid_tool_arguments_are_recoverable_and_strict(
    arguments: dict[str, object] | str,
) -> None:
    model = ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments=arguments,
                call_id="call-invalid",
            ),
            FinalStep(
                text="I corrected the invalid call.",
                expect_input_contains=(
                    RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value
                ),
            ),
        )
    )

    context, result = _run(model)

    rejection = next(
        stored.event
        for stored in context.runtime.store.events
        if isinstance(stored.event, ToolRejected)
    )
    assert rejection.code == RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value
    assert result.state.failures == 1


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


def test_retryable_tool_error_is_observed_by_the_model() -> None:
    model = ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id="call-retry",
            ),
            FinalStep(
                text="The source can be retried later.",
                expect_input_contains=(
                    ToolFailureCode.TEMPORARY_UNAVAILABLE.value
                ),
            ),
        )
    )
    context = create_organizer_context(
        run_id="run-1",
        candidate_source=_FailingSource(retryable=True),
        work_type=TmdbWorkType.ANIME,
    )

    result = asyncio.run(
        run_episode_organizer(
            context=context,
            model=model,
            prompt="Inspect the candidates.",
        )
    )

    assert result.state.failures == 1
    assert result.state.status is RunStatus.STOPPED


def test_fatal_tool_error_escapes_the_sdk_loop() -> None:
    model = ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id="call-fatal",
            ),
        )
    )
    context = create_organizer_context(
        run_id="run-1",
        candidate_source=_FailingSource(retryable=False),
        work_type=TmdbWorkType.ANIME,
    )

    with pytest.raises(UserError) as error:
        asyncio.run(
            run_episode_organizer(
                context=context,
                model=model,
                prompt="Inspect the candidates.",
            )
        )

    assert isinstance(error.value.__cause__, ToolExecutionError)
    assert context.runtime.state.status is RunStatus.FAILED
    assert context.runtime.state.stop_reason is StopReason.FATAL_ERROR


def test_sdk_max_turns_is_a_domain_stop_condition() -> None:
    model = ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id="call-1",
            ),
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id="call-2",
            ),
        )
    )
    context = create_organizer_context(
        run_id="run-1",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
        budget=RunBudget(
            max_model_turns=2,
            max_tool_calls=5,
            max_failures=3,
        ),
    )

    with pytest.raises(MaxTurnsExceeded):
        asyncio.run(
            run_episode_organizer(
                context=context,
                model=model,
                prompt="Keep inspecting.",
            )
        )

    assert context.runtime.state.status is RunStatus.STOPPED
    assert context.runtime.state.stop_reason is StopReason.MAX_TURNS


def test_same_script_produces_the_same_domain_event_transcript() -> None:
    steps = (
        ToolCallStep(
            name="list_candidates",
            arguments={"kind": "video", "cursor": 0, "limit": 1},
            call_id="call-1",
        ),
        FinalStep(text="done", expect_input_contains="video:1"),
    )

    first_context, first_result = _run(ScriptedModel(steps))
    second_context, second_result = _run(ScriptedModel(steps))

    assert first_context.runtime.store.events == (
        second_context.runtime.store.events
    )
    assert first_result.state == second_result.state
