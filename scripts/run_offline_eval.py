from __future__ import annotations

import argparse
import asyncio
import tempfile
from pathlib import Path

from reeloom.evals.dataset import EvalDataset
from reeloom.evals.reporting import render_eval_report
from reeloom.evals.runner import run_eval_dataset
from reeloom.observability.pricing import TokenPricing

_DEFAULT_DATASET = (
    Path(__file__).resolve().parents[1]
    / "evals"
    / "datasets"
    / "m7-baseline-v1.json"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic offline Reeloom eval baseline."
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=_DEFAULT_DATASET,
    )
    parser.add_argument("--input-usd-per-million")
    parser.add_argument("--output-usd-per-million")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if (args.input_usd_per_million is None) != (
        args.output_usd_per_million is None
    ):
        raise SystemExit("both token prices must be provided together")
    pricing = (
        TokenPricing.from_strings(
            input_usd_per_million=args.input_usd_per_million,
            output_usd_per_million=args.output_usd_per_million,
        )
        if args.input_usd_per_million is not None
        else None
    )
    dataset = EvalDataset.load(args.dataset.absolute())
    with tempfile.TemporaryDirectory(prefix="reeloom-eval-") as directory:
        results = asyncio.run(
            run_eval_dataset(
                dataset,
                workspace=Path(directory).resolve(),
                pricing=pricing,
            )
        )
    print(
        render_eval_report(
            dataset_hash=dataset.dataset_hash,
            results=results,
        )
    )
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
