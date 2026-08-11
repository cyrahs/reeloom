#!/usr/bin/env python3
"""Run identification against the real TMDB and a real model. Read-only.

    REELOOM_TMDB_API_KEY=... REELOOM_LLM_API_KEY=... \
    REELOOM_LLM_BASE_URL=https://api.openai.com/v1 \
    REELOOM_LLM_MODEL=gpt-5 \
    python scripts/live_smoke.py --live --folder /media/inbound/"[Group] Show"

Prints the plan it would build. It never moves a file: the executor is not
reachable from here.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from reeloom.adapters.llm import OpenAICompatibleModel  # noqa: E402
from reeloom.adapters.tmdb import TmdbClient  # noqa: E402
from reeloom.agent.identify import AgentIdentifier  # noqa: E402
from reeloom.models import MediaType, Run, RunState, WatchConfig  # noqa: E402
from reeloom.scanner import snapshot_folder  # noqa: E402


class LiveClients:
    def __init__(self, model, tmdb) -> None:
        self._model = model
        self._tmdb = tmdb

    async def model(self):
        return self._model

    async def tmdb(self):
        return self._tmdb


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument(
        "--media-type", choices=[item.value for item in MediaType], default="anime"
    )
    parser.add_argument("--library", type=Path, default=Path("/nonexistent"))
    arguments = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    tmdb = TmdbClient(_require("REELOOM_TMDB_API_KEY"))
    model = OpenAICompatibleModel(
        base_url=_require("REELOOM_LLM_BASE_URL"),
        api_key=_require("REELOOM_LLM_API_KEY"),
        model=_require("REELOOM_LLM_MODEL"),
    )

    snapshot = snapshot_folder(arguments.folder)
    print(f"{len(snapshot)} candidate(s) in {arguments.folder.name}\n")

    run = Run(
        id="live",
        config_id="live",
        folder_name=arguments.folder.name,
        state=RunState.IDENTIFYING,
        snapshot=tuple(snapshot),
    )
    config = WatchConfig(
        id="live",
        name="live",
        inbound_root=str(arguments.folder.parent),
        library_root=str(arguments.library),
        media_type=MediaType(arguments.media_type),
    )

    try:
        plan = await AgentIdentifier(LiveClients(model, tmdb)).identify(run, config)
    finally:
        await model.aclose()
        await tmdb.aclose()

    print(f"\n{plan.identity.title} ({plan.identity.year}) tmdb-{plan.identity.tmdb_id}")
    for move in plan.moves:
        print(f"  {move.source_path}\n    -> {move.dest_path}")
    if plan.unmapped:
        print(f"  unmapped: {', '.join(plan.unmapped)}")
    if plan.notes:
        print(f"  notes: {plan.notes}")
    return 0


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not set")
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
