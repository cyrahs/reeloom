from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from agents import Model, ModelSettings

from reeloom.agents.organizer import run_episode_organizer
from reeloom.agents.scripted_model import ScriptedModel
from reeloom.evals.dataset import (
    EvalDataset,
    EvalRejection,
    EvalRejectionKind,
    EvalTask,
)
from reeloom.evals.scenarios import build_eval_scenario
from reeloom.observability.pricing import TokenPricing
from reeloom.observability.trace import TraceReport, build_trace
from reeloom.runtime.events import MappingRejected, ToolRejected
from reeloom.runtime.state import RunState, StopReason
from reeloom.runtime.store import StoredEvent

ModelFactory = Callable[[EvalTask], Model]


@dataclass(frozen=True, slots=True)
class EvalMetrics:
    mapping_success: bool
    clarification_required: bool
    validator_first_pass: bool
    validator_final_pass: bool
    validator_rejections: int
    tool_rejections: int
    safety_false_positives: int
    safety_false_negatives: int
    safety_scored: bool
    tool_calls: int
    model_turns: int
    input_tokens: int
    output_tokens: int
    model_tokens: int
    unmapped_count: int
    unmapped_retention_rate: float
    elapsed_ms: int
    estimated_cost_microusd: int | None


@dataclass(frozen=True, slots=True)
class EvalResult:
    task_id: str
    passed: bool
    failures: tuple[str, ...]
    metrics: EvalMetrics
    trace: TraceReport


def evaluate_task(
    task: EvalTask,
    *,
    state: RunState,
    events: tuple[StoredEvent, ...],
    elapsed_ms: int = 0,
    pricing: TokenPricing | None = None,
    strict_process: bool = True,
) -> EvalResult:
    trace = build_trace(events)
    rejection_count = trace.summary.mapping_rejections
    tool_rejection_count = trace.summary.tool_rejections
    mapping_success = _mapping_matches(task, state)
    actual_rejections = Counter(_rejections(events))
    expected_rejections = Counter(
        task.expectation.scripted_process.rejections
    )
    expected_unmapped = frozenset(
        task.expectation.unmapped_candidate_ids
    )
    actual_unmapped = (
        frozenset(state.rename_plan.draft.unmapped_candidate_ids)
        if state.rename_plan is not None
        else frozenset()
    )
    retained = len(expected_unmapped & actual_unmapped)
    metrics = EvalMetrics(
        mapping_success=mapping_success,
        clarification_required=(
            state.rename_plan is None
            and state.stop_reason is StopReason.MODEL_FINAL
        ),
        validator_first_pass=(
            state.mapping_draft is not None and rejection_count == 0
        ),
        validator_final_pass=state.mapping_draft is not None,
        validator_rejections=rejection_count,
        tool_rejections=tool_rejection_count,
        safety_false_positives=(
            sum((actual_rejections - expected_rejections).values())
            if strict_process
            else 0
        ),
        safety_false_negatives=(
            sum((expected_rejections - actual_rejections).values())
            if strict_process
            else 0
        ),
        safety_scored=strict_process,
        tool_calls=state.tool_calls,
        model_turns=state.model_turns,
        input_tokens=trace.summary.input_tokens,
        output_tokens=trace.summary.output_tokens,
        model_tokens=state.model_tokens,
        unmapped_count=len(actual_unmapped),
        unmapped_retention_rate=(
            retained / len(expected_unmapped)
            if expected_unmapped
            else 1.0
        ),
        elapsed_ms=elapsed_ms,
        estimated_cost_microusd=(
            pricing.estimate_cost_microusd(
                input_tokens=trace.summary.input_tokens,
                output_tokens=trace.summary.output_tokens,
            )
            if pricing is not None
            else None
        ),
    )
    expected = task.expectation
    checks = {
        "clarification_required": metrics.clarification_required
        == expected.clarification_required,
        "mapping_success": metrics.mapping_success
        == expected.mapping_success,
        "phase": state.phase is expected.phase,
        "status": state.status is expected.status,
        "stop_reason": state.stop_reason is expected.stop_reason,
    }
    if strict_process:
        checks["tool_calls"] = (
            metrics.tool_calls == expected.scripted_process.tool_calls
        )
        checks["safety_rejections"] = (
            metrics.safety_false_positives == 0
            and metrics.safety_false_negatives == 0
        )
    failures = tuple(name for name, passed in checks.items() if not passed)
    return EvalResult(
        task_id=task.task_id,
        passed=not failures,
        failures=failures,
        metrics=metrics,
        trace=trace,
    )


def _mapping_matches(task: EvalTask, state: RunState) -> bool:
    plan = state.rename_plan
    if plan is None:
        return False
    expected = task.expectation
    videos = frozenset(
        (
            str(item.video_id),
            item.span.season,
            item.span.episode_start,
            item.span.episode_end,
        )
        for item in expected.videos
    )
    actual_videos = frozenset(
        (
            str(item.video_id),
            item.span.season,
            item.span.episode_start,
            item.span.episode_end,
        )
        for item in plan.draft.mapping.videos
    )
    subtitles = frozenset(
        (str(item.subtitle_id), str(item.video_id))
        for item in expected.subtitles
    )
    actual_subtitles = frozenset(
        (str(item.subtitle_id), str(item.video_id))
        for item in plan.draft.mapping.subtitles
    )
    return (
        plan.draft.series.tmdb_id == expected.selected_tmdb_id
        and actual_videos == videos
        and actual_subtitles == subtitles
        and frozenset(plan.draft.unmapped_candidate_ids)
        == frozenset(expected.unmapped_candidate_ids)
    )


def _rejections(
    events: tuple[StoredEvent, ...],
) -> tuple[EvalRejection, ...]:
    result: list[EvalRejection] = []
    for stored in events:
        event = stored.event
        if isinstance(event, MappingRejected):
            result.append(
                EvalRejection(
                    kind=EvalRejectionKind.MAPPING,
                    call_id=event.call_id,
                    code=event.issue.code,
                )
            )
        elif isinstance(event, ToolRejected):
            result.append(
                EvalRejection(
                    kind=EvalRejectionKind.TOOL,
                    call_id=event.call_id,
                    code=event.code,
                )
            )
    return tuple(result)


async def run_eval_dataset(
    dataset: EvalDataset,
    *,
    workspace: Path,
    model_factory: ModelFactory | None = None,
    pricing: TokenPricing | None = None,
    model_settings: ModelSettings | None = None,
) -> tuple[EvalResult, ...]:
    if not isinstance(dataset, EvalDataset):
        raise ValueError("invalid eval dataset")
    if not isinstance(workspace, Path) or not workspace.is_absolute():
        raise ValueError("eval workspace must be absolute")
    if workspace.exists():
        if workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("invalid eval workspace")
        if any(workspace.iterdir()):
            raise ValueError("eval workspace must be empty")
    else:
        workspace.mkdir(mode=0o700)
    results: list[EvalResult] = []
    for task in dataset.tasks:
        task_workspace = workspace / task.task_id
        context = build_eval_scenario(
            task.scenario,
            workspace=task_workspace,
            run_id=f"eval-{task.task_id}",
            work_type=task.work_type,
        )
        scripted = model_factory is None
        model = (
            ScriptedModel(task.transcript)
            if scripted
            else model_factory(task)
        )
        started_at = perf_counter()
        run_error: str | None = None
        try:
            await run_episode_organizer(
                context=context,
                model=model,
                prompt=task.prompt,
                model_settings=model_settings,
            )
        except Exception as error:
            run_error = type(error).__name__
        elapsed_ms = max(0, round((perf_counter() - started_at) * 1_000))
        result = evaluate_task(
            task,
            state=context.runtime.state,
            events=context.runtime.store.events,
            elapsed_ms=elapsed_ms,
            pricing=pricing,
            strict_process=scripted,
        )
        if run_error is not None:
            result = EvalResult(
                task_id=result.task_id,
                passed=False,
                failures=result.failures + (f"run_error:{run_error}",),
                metrics=result.metrics,
                trace=result.trace,
            )
        if (
            scripted
            and isinstance(model, ScriptedModel)
            and not model.exhausted
        ):
            result = EvalResult(
                task_id=result.task_id,
                passed=False,
                failures=result.failures + ("transcript_not_exhausted",),
                metrics=result.metrics,
                trace=result.trace,
            )
        results.append(result)
    return tuple(results)
