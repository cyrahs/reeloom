from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agents import MaxTurnsExceeded, ModelSettings, UserError

from reeloom.adapters.agent_session import FilesystemAgentSession
from reeloom.adapters.event_store import FilesystemEventStore
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
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.errors import (
    BudgetExceeded,
    RuntimeDomainError,
    RuntimeErrorCode,
)
from reeloom.runtime.events import ModelUsageRecorded, ToolRejected
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
        clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )
    result = asyncio.run(
        run_episode_organizer(
            context=context,
            model=model,
            prompt="Inspect the candidates.",
        )
    )
    return context, result


def test_plan_compiler_must_match_the_run_candidate_snapshot() -> None:
    class ForeignCompiler:
        snapshot_id = "candidate-snapshot-v1:foreign"
        candidate_count = 1

    with pytest.raises(RuntimeDomainError) as raised:
        create_organizer_context(
            run_id="run-1",
            candidate_source=_source(),
            work_type=TmdbWorkType.ANIME,
            plan_compiler=ForeignCompiler(),  # type: ignore[arg-type]
        )

    assert raised.value.code is RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE


def test_plan_compiler_requires_plan_persistence() -> None:
    source = _source()

    class MatchingCompiler:
        snapshot_id = source.snapshot_id
        candidate_count = source.candidate_count

    with pytest.raises(RuntimeDomainError) as raised:
        create_organizer_context(
            run_id="run-1",
            candidate_source=source,
            work_type=TmdbWorkType.ANIME,
            plan_compiler=MatchingCompiler(),  # type: ignore[arg-type]
        )

    assert raised.value.code is RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE


def test_organizer_context_recovers_from_persistent_events(
    tmp_path: Path,
) -> None:
    event_path = tmp_path / "events"
    event_path.mkdir()
    root = AuthorizedRoot.create(event_path)
    source = _source()
    first_store = FilesystemEventStore(root, run_id="run-1")
    first = create_organizer_context(
        run_id="run-1",
        candidate_source=source,
        work_type=TmdbWorkType.ANIME,
        event_store=first_store,
    )

    second_store = FilesystemEventStore(root, run_id="run-1")
    second = create_organizer_context(
        run_id="run-1",
        candidate_source=source,
        work_type=TmdbWorkType.ANIME,
        event_store=second_store,
    )

    assert second.runtime.state == first.runtime.state
    assert len(second.runtime.store.events) == 2


def test_recovered_context_reuses_original_budget(
    tmp_path: Path,
) -> None:
    event_path = tmp_path / "events"
    event_path.mkdir()
    root = AuthorizedRoot.create(event_path)
    original_budget = RunBudget(
        max_model_turns=1,
        max_elapsed_seconds=30,
    )
    first_store = FilesystemEventStore(root, run_id="run-budget")
    first = create_organizer_context(
        run_id="run-budget",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
        event_store=first_store,
        budget=original_budget,
        clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )

    recovered = create_organizer_context(
        run_id="run-budget",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
        event_store=FilesystemEventStore(root, run_id="run-budget"),
        clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )

    assert first.runtime.budget == original_budget
    assert recovered.runtime.budget == original_budget
    with pytest.raises(RuntimeDomainError) as raised:
        create_organizer_context(
            run_id="run-budget",
            candidate_source=_source(),
            work_type=TmdbWorkType.ANIME,
            event_store=FilesystemEventStore(
                root,
                run_id="run-budget",
            ),
            budget=RunBudget(max_model_turns=2),
        )
    assert raised.value.code is RuntimeErrorCode.INVALID_TRANSITION


def test_recovered_run_receives_only_remaining_turns(
    tmp_path: Path,
) -> None:
    event_path = tmp_path / "events"
    event_path.mkdir()
    root = AuthorizedRoot.create(event_path)
    context = create_organizer_context(
        run_id="run-turns",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
        event_store=FilesystemEventStore(root, run_id="run-turns"),
        budget=RunBudget(max_model_turns=1),
        clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )
    context.runtime.store.append(ModelUsageRecorded(1, 1, 2))
    recovered = create_organizer_context(
        run_id="run-turns",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
        event_store=FilesystemEventStore(root, run_id="run-turns"),
        clock=lambda: datetime(2026, 7, 24, tzinfo=UTC),
    )
    model = ScriptedModel((FinalStep(text="must not run"),))

    with pytest.raises(MaxTurnsExceeded):
        asyncio.run(
            run_episode_organizer(
                context=recovered,
                model=model,
                prompt="Continue.",
            )
        )

    assert model.consumed_steps == 0
    assert recovered.runtime.state.stop_reason is StopReason.MAX_TURNS


def test_recovered_run_keeps_absolute_deadline(
    tmp_path: Path,
) -> None:
    event_path = tmp_path / "events"
    event_path.mkdir()
    root = AuthorizedRoot.create(event_path)
    started_at = datetime(2026, 7, 24, tzinfo=UTC)
    create_organizer_context(
        run_id="run-deadline",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
        event_store=FilesystemEventStore(root, run_id="run-deadline"),
        budget=RunBudget(max_elapsed_seconds=1),
        clock=lambda: started_at,
    )
    recovered = create_organizer_context(
        run_id="run-deadline",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
        event_store=FilesystemEventStore(root, run_id="run-deadline"),
        clock=lambda: started_at + timedelta(seconds=2),
    )
    model = ScriptedModel((FinalStep(text="must not run"),))

    with pytest.raises(BudgetExceeded) as raised:
        asyncio.run(
            run_episode_organizer(
                context=recovered,
                model=model,
                prompt="Continue.",
            )
        )

    assert raised.value.code is RuntimeErrorCode.TIME_BUDGET_EXHAUSTED
    assert model.consumed_steps == 0


def test_sdk_session_persists_model_history_separately(
    tmp_path: Path,
) -> None:
    session_path = tmp_path / "session"
    session_path.mkdir()
    root = AuthorizedRoot.create(session_path)
    session = FilesystemAgentSession(root, session_id="run-session")
    context = create_organizer_context(
        run_id="run-session",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
        agent_session=session,
    )
    model = ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id="call-1",
            ),
            FinalStep(text="done", expect_input_contains="video:1"),
        )
    )

    asyncio.run(
        run_episode_organizer(
            context=context,
            model=model,
            prompt="Inspect.",
        )
    )
    persisted = asyncio.run(session.get_items())
    restarted = FilesystemAgentSession(root, session_id="run-session")

    assert persisted
    assert asyncio.run(restarted.get_items()) == persisted


def test_sdk_session_must_be_bound_to_run_id(tmp_path: Path) -> None:
    session_path = tmp_path / "session"
    session_path.mkdir()
    session = FilesystemAgentSession(
        AuthorizedRoot.create(session_path),
        session_id="another-run",
    )

    with pytest.raises(RuntimeDomainError) as raised:
        create_organizer_context(
            run_id="run-1",
            candidate_source=_source(),
            work_type=TmdbWorkType.ANIME,
            agent_session=session,
        )

    assert raised.value.code is RuntimeErrorCode.RUN_ID_MISMATCH


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


def test_runner_enforces_private_sequential_responses_settings() -> None:
    class CapturingModel(ScriptedModel):
        captured: ModelSettings | None = None

        async def get_response(
            self,
            *args: object,
            **kwargs: object,
        ):
            self.captured = (
                kwargs.get("model_settings")
                if isinstance(
                    kwargs.get("model_settings"),
                    ModelSettings,
                )
                else args[2]
            )  # type: ignore[assignment]
            return await super().get_response(  # type: ignore[arg-type]
                *args,
                **kwargs,
            )

    model = CapturingModel((FinalStep(text="done"),))
    context = create_organizer_context(
        run_id="run-settings",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
    )

    asyncio.run(
        run_episode_organizer(
            context=context,
            model=model,
            prompt="Inspect.",
            model_settings=ModelSettings(
                max_tokens=99_999,
                parallel_tool_calls=True,
                store=True,
                extra_body={
                    "store": True,
                    "tools": [{"type": "web_search"}],
                },
                extra_headers={"Authorization": "Bearer attacker"},
                extra_args={"max_output_tokens": 99_999},
                extra_query={"unsafe": "true"},
            ),
        )
    )

    assert model.captured is not None
    assert model.captured.max_tokens == 8_192
    assert model.captured.parallel_tool_calls is False
    assert model.captured.store is False
    assert model.captured.extra_body is None
    assert model.captured.extra_headers is None
    assert model.captured.extra_args is None
    assert model.captured.extra_query is None


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


class _CrashingSource:
    snapshot_id = "candidate-snapshot-v1:crashing"
    candidate_count = 0

    async def page(
        self,
        *,
        kind: CandidateKind,
        cursor: int,
        limit: int,
    ) -> CandidatePage:
        del kind, cursor, limit
        raise ValueError("unexpected adapter failure")


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


def test_unexpected_tool_error_aborts_and_clears_pending_call() -> None:
    model = ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id="call-crash",
            ),
        )
    )
    context = create_organizer_context(
        run_id="run-1",
        candidate_source=_CrashingSource(),
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

    assert isinstance(error.value.__cause__, ValueError)
    assert context.runtime.state.status is RunStatus.FAILED
    assert context.runtime.state.pending_tool_calls == frozenset()


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


def test_token_budget_stops_after_recording_model_usage() -> None:
    model = ScriptedModel((FinalStep(text="must not complete"),))
    context = create_organizer_context(
        run_id="run-1",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
        budget=RunBudget(max_total_tokens=1),
    )

    with pytest.raises(BudgetExceeded) as error:
        asyncio.run(
            run_episode_organizer(
                context=context,
                model=model,
                prompt="Inspect the candidates.",
            )
        )

    assert error.value.code is RuntimeErrorCode.TOKEN_BUDGET_EXHAUSTED
    assert context.runtime.state.model_turns == 1
    assert context.runtime.state.model_tokens == 2
    assert context.runtime.state.status is RunStatus.STOPPED
    assert context.runtime.state.stop_reason is StopReason.BUDGET_EXHAUSTED


def test_exact_token_budget_prevents_another_model_request() -> None:
    model = ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id="call-1",
            ),
            FinalStep(text="must not run"),
        )
    )
    context = create_organizer_context(
        run_id="run-1",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
        budget=RunBudget(max_total_tokens=2),
    )

    with pytest.raises(BudgetExceeded) as error:
        asyncio.run(
            run_episode_organizer(
                context=context,
                model=model,
                prompt="Inspect the candidates.",
            )
        )

    assert error.value.code is RuntimeErrorCode.TOKEN_BUDGET_EXHAUSTED
    assert model.consumed_steps == 1
    assert context.runtime.state.model_tokens == 2
    assert context.runtime.state.tool_calls == 1


class _DelayedScriptedModel(ScriptedModel):
    async def get_response(self, *args: object, **kwargs: object):
        await asyncio.sleep(0.02)
        return await super().get_response(*args, **kwargs)


def test_elapsed_time_budget_stops_the_sdk_run() -> None:
    model = _DelayedScriptedModel((FinalStep(text="too late"),))
    context = create_organizer_context(
        run_id="run-1",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
        budget=RunBudget(max_elapsed_seconds=0.001),
    )

    with pytest.raises(BudgetExceeded) as error:
        asyncio.run(
            run_episode_organizer(
                context=context,
                model=model,
                prompt="Inspect the candidates.",
            )
        )

    assert error.value.code is RuntimeErrorCode.TIME_BUDGET_EXHAUSTED
    assert context.runtime.state.status is RunStatus.STOPPED
    assert context.runtime.state.stop_reason is StopReason.BUDGET_EXHAUSTED


class _CancellationIgnoringModel(ScriptedModel):
    async def get_response(self, *args: object, **kwargs: object):
        try:
            await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)
        return await super().get_response(*args, **kwargs)


def test_elapsed_budget_rejects_result_after_cancellation_is_swallowed() -> None:
    model = _CancellationIgnoringModel((FinalStep(text="too late"),))
    context = create_organizer_context(
        run_id="run-1",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
        budget=RunBudget(max_elapsed_seconds=0.001),
    )

    with pytest.raises(BudgetExceeded) as error:
        asyncio.run(
            run_episode_organizer(
                context=context,
                model=model,
                prompt="Inspect the candidates.",
            )
        )

    assert error.value.code is RuntimeErrorCode.TIME_BUDGET_EXHAUSTED
    assert context.runtime.state.stop_reason is StopReason.BUDGET_EXHAUSTED


class _UpstreamTimeoutModel(ScriptedModel):
    async def get_response(self, *args: object, **kwargs: object):
        del args, kwargs
        raise TimeoutError("upstream timeout")


def test_upstream_timeout_is_not_misclassified_as_run_budget() -> None:
    context = create_organizer_context(
        run_id="run-1",
        candidate_source=_source(),
        work_type=TmdbWorkType.ANIME,
    )

    with pytest.raises(TimeoutError, match="upstream timeout"):
        asyncio.run(
            run_episode_organizer(
                context=context,
                model=_UpstreamTimeoutModel((FinalStep(text="unused"),)),
                prompt="Inspect the candidates.",
            )
        )

    assert context.runtime.state.status is RunStatus.FAILED
    assert context.runtime.state.failure_code == (
        RuntimeErrorCode.AGENT_RUN_FAILED.value
    )


class _SlowCandidateSource:
    def __init__(self) -> None:
        self._source = _source()
        self.snapshot_id = self._source.snapshot_id
        self.candidate_count = self._source.candidate_count

    async def page(
        self,
        *,
        kind: CandidateKind,
        cursor: int,
        limit: int,
    ) -> CandidatePage:
        await asyncio.sleep(0.2)
        return await self._source.page(
            kind=kind,
            cursor=cursor,
            limit=limit,
        )


def test_elapsed_budget_cancels_and_clears_a_pending_tool_call() -> None:
    model = ScriptedModel(
        (
            ToolCallStep(
                name="list_candidates",
                arguments={"kind": "video", "cursor": 0, "limit": 10},
                call_id="slow-call",
            ),
        )
    )
    context = create_organizer_context(
        run_id="run-1",
        candidate_source=_SlowCandidateSource(),
        work_type=TmdbWorkType.ANIME,
        budget=RunBudget(max_elapsed_seconds=0.05),
    )

    with pytest.raises(BudgetExceeded) as error:
        asyncio.run(
            run_episode_organizer(
                context=context,
                model=model,
                prompt="Inspect the candidates.",
            )
        )

    assert error.value.code is RuntimeErrorCode.TIME_BUDGET_EXHAUSTED
    assert context.runtime.state.tool_calls == 1
    assert context.runtime.state.pending_tool_calls == frozenset()
    assert context.runtime.state.stop_reason is StopReason.BUDGET_EXHAUSTED


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


def test_stopped_context_cannot_start_another_sdk_run() -> None:
    context, _ = _run(ScriptedModel((FinalStep(text="done"),)))
    second_model = ScriptedModel((FinalStep(text="must not run"),))

    with pytest.raises(RuntimeDomainError) as error:
        asyncio.run(
            run_episode_organizer(
                context=context,
                model=second_model,
                prompt="Run again.",
            )
        )

    assert error.value.code is RuntimeErrorCode.RUN_NOT_ACTIVE
    assert second_model.consumed_steps == 0
