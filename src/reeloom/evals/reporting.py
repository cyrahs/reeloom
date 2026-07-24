from __future__ import annotations

import json
from collections.abc import Mapping

from reeloom.evals.runner import EvalResult


def render_eval_report(
    *,
    dataset_hash: str,
    results: tuple[EvalResult, ...],
    metadata: Mapping[str, object] | None = None,
) -> str:
    payload: dict[str, object] = dict(metadata or {})
    payload.update(
        {
            "dataset_hash": dataset_hash,
            "passed": all(result.passed for result in results),
            "results": [_result_payload(result) for result in results],
            "summary": _summary_payload(results),
        }
    )
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _summary_payload(
    results: tuple[EvalResult, ...],
) -> dict[str, object]:
    task_count = len(results)
    divisor = task_count or 1
    return {
        "clarification_rate": (
            sum(
                result.metrics.clarification_required
                for result in results
            )
            / divisor
        ),
        "input_tokens": sum(
            result.metrics.input_tokens for result in results
        ),
        "mapping_success_rate": (
            sum(result.metrics.mapping_success for result in results)
            / divisor
        ),
        "output_tokens": sum(
            result.metrics.output_tokens for result in results
        ),
        "task_count": task_count,
        "unmapped_retention_rate": (
            sum(
                result.metrics.unmapped_retention_rate
                for result in results
            )
            / divisor
        ),
    }


def _result_payload(result: EvalResult) -> dict[str, object]:
    metrics = result.metrics
    return {
        "failures": result.failures,
        "metrics": {
            "clarification_required": metrics.clarification_required,
            "elapsed_ms": metrics.elapsed_ms,
            "estimated_cost_microusd": metrics.estimated_cost_microusd,
            "input_tokens": metrics.input_tokens,
            "mapping_success": metrics.mapping_success,
            "model_tokens": metrics.model_tokens,
            "model_turns": metrics.model_turns,
            "output_tokens": metrics.output_tokens,
            "safety_false_negatives": metrics.safety_false_negatives,
            "safety_false_positives": metrics.safety_false_positives,
            "safety_scored": metrics.safety_scored,
            "tool_calls": metrics.tool_calls,
            "tool_rejections": metrics.tool_rejections,
            "unmapped_count": metrics.unmapped_count,
            "unmapped_retention_rate": metrics.unmapped_retention_rate,
            "validator_final_pass": metrics.validator_final_pass,
            "validator_first_pass": metrics.validator_first_pass,
            "validator_rejections": metrics.validator_rejections,
        },
        "passed": result.passed,
        "task_id": result.task_id,
    }
