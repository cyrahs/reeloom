from __future__ import annotations

import argparse
import asyncio
import logging
import os
import tempfile
from pathlib import Path

from reeloom.adapters.openai_model import (
    OpenAIModelConfig,
    OpenAIModelProvider,
)
from reeloom.evals.dataset import EvalDataset
from reeloom.evals.reporting import render_eval_report
from reeloom.evals.runner import EvalResult, run_eval_dataset
from reeloom.observability.pricing import TokenPricing

_DEFAULT_DATASET = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "datasets"
    / "m7-baseline-v1.json"
)
logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an explicit live OpenAI model eval via Responses API."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="confirm that real OpenAI network access and billing are intended",
    )
    parser.add_argument("--model")
    parser.add_argument("--dataset", type=Path, default=_DEFAULT_DATASET)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--organization")
    parser.add_argument("--project")
    parser.add_argument("--reasoning-effort")
    parser.add_argument("--verbosity", choices=("low", "medium", "high"))
    parser.add_argument("--input-usd-per-million")
    parser.add_argument("--output-usd-per-million")
    return parser.parse_args()


def _api_key() -> str | None:
    value = os.environ.get("OPENAI_API_KEY")
    return value if value else None


def _pricing(args: argparse.Namespace) -> TokenPricing | None:
    input_rate = args.input_usd_per_million
    output_rate = args.output_usd_per_million
    if (input_rate is None) != (output_rate is None):
        raise ValueError("both token prices must be provided together")
    if input_rate is None:
        return None
    return TokenPricing.from_strings(
        input_usd_per_million=input_rate,
        output_usd_per_million=output_rate,
    )


async def _run(
    *,
    api_key: str,
    args: argparse.Namespace,
) -> tuple[str, tuple[EvalResult, ...]]:
    dataset = EvalDataset.load(args.dataset.absolute())
    config = OpenAIModelConfig(
        model_name=args.model,
        request_timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
        organization=args.organization,
        project=args.project,
        reasoning_effort=args.reasoning_effort,
        verbosity=args.verbosity,
    )
    provider = OpenAIModelProvider(api_key=api_key, config=config)
    try:
        with tempfile.TemporaryDirectory(
            prefix="reeloom-openai-eval-"
        ) as directory:
            results = await run_eval_dataset(
                dataset,
                workspace=Path(directory).resolve(),
                model_factory=lambda _: provider.model,
                pricing=_pricing(args),
                model_settings=config.model_settings(),
            )
        return dataset.dataset_hash, results
    finally:
        await provider.close()


def _report(
    *,
    model_name: str,
    reasoning_effort: str | None,
    verbosity: str | None,
    dataset_hash: str,
    results: tuple[EvalResult, ...],
) -> str:
    return render_eval_report(
        dataset_hash=dataset_hash,
        results=results,
        metadata={
            "model": model_name,
            "reasoning_effort": reasoning_effort or "provider_default",
            "verbosity": verbosity or "provider_default",
        },
    )


def main() -> int:
    args = _parse_args()
    if not args.live:
        logger.error("live smoke disabled: pass --live to opt in")
        return 2
    if not args.model:
        logger.error("live smoke disabled: pass an explicit --model")
        return 2
    api_key = _api_key()
    if api_key is None:
        logger.error("live smoke disabled: OPENAI_API_KEY is required")
        return 2
    try:
        dataset_hash, results = asyncio.run(
            _run(api_key=api_key, args=args)
        )
        print(
            _report(
                model_name=args.model,
                reasoning_effort=args.reasoning_effort,
                verbosity=args.verbosity,
                dataset_hash=dataset_hash,
                results=results,
            )
        )
        return 0 if all(
            result.passed for result in results
        ) else 1
    except Exception as error:
        logger.error(
            "live OpenAI smoke failed: %s",
            type(error).__name__,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
