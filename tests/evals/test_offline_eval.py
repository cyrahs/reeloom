from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reeloom.evals.dataset import EvalDataset
from reeloom.agents.scripted_model import ScriptedModel
from reeloom.evals.reporting import render_eval_report
from reeloom.evals.runner import run_eval_dataset
from reeloom.observability.pricing import TokenPricing

_DATASET = (
    Path(__file__).resolve().parents[2]
    / "evals"
    / "datasets"
    / "m7-baseline-v1.json"
)


def test_fixed_offline_eval_replays_and_meets_baseline(
    tmp_path: Path,
) -> None:
    dataset = EvalDataset.load(_DATASET)

    results = asyncio.run(
        run_eval_dataset(
            dataset,
            workspace=(tmp_path / "run").resolve(),
            pricing=TokenPricing.from_strings(
                input_usd_per_million="1",
                output_usd_per_million="2",
            ),
        )
    )

    assert len(results) == 1
    assert results[0].passed
    assert results[0].failures == ()
    assert not results[0].metrics.validator_first_pass
    assert results[0].metrics.validator_final_pass
    assert results[0].metrics.validator_rejections == 1
    assert results[0].metrics.tool_rejections == 1
    assert results[0].metrics.tool_calls == 9
    assert results[0].metrics.unmapped_count == 1
    assert results[0].metrics.unmapped_retention_rate == 1.0
    assert not results[0].metrics.clarification_required
    assert results[0].metrics.input_tokens == 9
    assert results[0].metrics.output_tokens == 9
    assert results[0].metrics.safety_false_positives == 0
    assert results[0].metrics.safety_false_negatives == 0
    assert results[0].metrics.safety_scored
    assert results[0].metrics.estimated_cost_microusd == 27
    trace = results[0].trace.canonical_bytes()
    assert b"untrusted episode name" not in trace
    assert b"Correct Anime" not in trace
    assert b"Inspect the candidates" not in trace
    assert results[0].trace.summary.mapping_success


def test_mapping_success_requires_exact_semantic_ground_truth(
    tmp_path: Path,
) -> None:
    payload = json.loads(_DATASET.read_bytes())
    payload["tasks"][0]["expectation"]["videos"][0][
        "episode_start"
    ] = 1
    payload["tasks"][0]["expectation"]["videos"][0]["episode_end"] = 1
    dataset = EvalDataset.from_bytes(json.dumps(payload).encode())

    result = asyncio.run(
        run_eval_dataset(
            dataset,
            workspace=(tmp_path / "semantic").resolve(),
        )
    )[0]

    assert not result.metrics.mapping_success
    assert result.failures == ("mapping_success",)


def test_live_scoring_accepts_a_better_process_with_same_mapping(
    tmp_path: Path,
) -> None:
    payload = json.loads(_DATASET.read_bytes())
    steps = payload["tasks"][0]["transcript"]["steps"]
    del steps[-2]
    steps[-1]["expect_input_contains"] = '"variant":"chs"'
    dataset = EvalDataset.from_bytes(json.dumps(payload).encode())

    result = asyncio.run(
        run_eval_dataset(
            dataset,
            workspace=(tmp_path / "live-style").resolve(),
            model_factory=lambda task: ScriptedModel(task.transcript),
        )
    )[0]

    assert result.passed
    assert result.metrics.mapping_success
    assert result.metrics.tool_calls == 8
    assert not result.metrics.safety_scored


def test_safety_errors_compare_typed_labels_not_aggregate_counts(
    tmp_path: Path,
) -> None:
    payload = json.loads(_DATASET.read_bytes())
    for rejection in payload["tasks"][0]["expectation"][
        "scripted_process"
    ]["rejections"]:
        rejection["call_id"] = "different-call"
    dataset = EvalDataset.from_bytes(json.dumps(payload).encode())

    result = asyncio.run(
        run_eval_dataset(
            dataset,
            workspace=(tmp_path / "safety-labels").resolve(),
        )
    )[0]

    assert result.metrics.validator_rejections == 1
    assert result.metrics.tool_rejections == 1
    assert result.metrics.safety_false_positives == 2
    assert result.metrics.safety_false_negatives == 2
    assert result.failures == ("safety_rejections",)


def test_report_includes_aggregate_rates_and_split_tokens(
    tmp_path: Path,
) -> None:
    dataset = EvalDataset.load(_DATASET)
    result = asyncio.run(
        run_eval_dataset(
            dataset,
            workspace=(tmp_path / "report").resolve(),
        )
    )

    report = json.loads(
        render_eval_report(
            dataset_hash=dataset.dataset_hash,
            results=result,
        )
    )

    assert report["summary"] == {
        "clarification_rate": 0.0,
        "input_tokens": 9,
        "mapping_success_rate": 1.0,
        "output_tokens": 9,
        "task_count": 1,
        "unmapped_retention_rate": 1.0,
    }


def test_model_final_without_a_plan_counts_as_human_clarification(
    tmp_path: Path,
) -> None:
    payload = json.loads(_DATASET.read_bytes())
    expectation = payload["tasks"][0]["expectation"]
    expectation.update(
        {
            "clarification_required": True,
            "mapping_success": False,
            "phase": "identify_series",
            "status": "stopped",
            "stop_reason": "model_final",
            "scripted_process": {
                "rejections": [],
                "tool_calls": 0,
            },
        }
    )
    payload["tasks"][0]["transcript"]["steps"] = [
        {
            "expect_input_contains": None,
            "text": "Need user input.",
            "type": "final",
        }
    ]
    dataset = EvalDataset.from_bytes(json.dumps(payload).encode())

    result = asyncio.run(
        run_eval_dataset(
            dataset,
            workspace=(tmp_path / "clarification").resolve(),
        )
    )[0]

    assert result.passed
    assert result.metrics.clarification_required


def test_eval_dataset_hash_is_stable_and_schema_is_strict() -> None:
    first = EvalDataset.load(_DATASET)
    second = EvalDataset.load(_DATASET)
    payload = json.loads(_DATASET.read_bytes())
    payload["unexpected"] = True

    assert first.dataset_hash == second.dataset_hash
    with pytest.raises(ValueError):
        EvalDataset.from_bytes(
            json.dumps(payload).encode("utf-8")
        )


def test_eval_task_id_cannot_escape_workspace() -> None:
    payload = json.loads(_DATASET.read_bytes())
    payload["tasks"][0]["task_id"] = "../escape"

    with pytest.raises(ValueError):
        EvalDataset.from_bytes(json.dumps(payload).encode("utf-8"))


def test_eval_dataset_rejects_duplicate_json_keys() -> None:
    content = _DATASET.read_text(encoding="utf-8")
    duplicate = content.replace(
        '"schema_version": "reeloom-eval-dataset-v1",',
        (
            '"schema_version": "reeloom-eval-dataset-v1",'
            '"schema_version": "reeloom-eval-dataset-v1",'
        ),
        1,
    )

    with pytest.raises(ValueError):
        EvalDataset.from_bytes(duplicate.encode("utf-8"))


def test_eval_dataset_loader_rejects_forbidden_or_unsafe_files(
    tmp_path: Path,
) -> None:
    target = tmp_path / "dataset.json"
    target.write_bytes(_DATASET.read_bytes())
    symlink = tmp_path / "dataset-link.json"
    symlink.symlink_to(target)

    for invalid in (
        tmp_path / ".env-eval",
        symlink,
    ):
        with pytest.raises(ValueError):
            EvalDataset.load(invalid)


def test_eval_dataset_loader_checks_size_before_reading(
    tmp_path: Path,
) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (4 * 1024 * 1024 + 1))

    with pytest.raises(ValueError):
        EvalDataset.load(oversized)
