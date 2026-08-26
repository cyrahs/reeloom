"""Offline stand-ins for the database and the external services.

The real repository is exercised under the ``postgres`` marker; everything
else runs against these so the default suite needs no network and no server.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Sequence

from reeloom.adapters.llm import Conversation, ModelReply, ToolCall
from reeloom.adapters.tmdb import TmdbHit
from reeloom.models import (
    ExecutedMove,
    MediaType,
    Plan,
    Run,
    RunResult,
    RunState,
    SnapshotFile,
    SubtitleVariant,
    WatchConfig,
)


class FakeDatabase:
    """In-memory mirror of the handful of queries the worker performs."""

    def __init__(self, configs: Sequence[WatchConfig] = ()) -> None:
        self.configs: dict[str, WatchConfig] = {c.id: c for c in configs}
        self.runs: dict[str, Run] = {}
        self.logs: list[tuple[str, str]] = []
        self.interactions: dict[str, list[dict[str, Any]]] = {}
        self.settings: dict[str, Any] = {}

    async def list_configs(self, *, enabled_only: bool = False) -> list[WatchConfig]:
        return [
            config
            for config in self.configs.values()
            if config.enabled or not enabled_only
        ]

    async def get_config(self, config_id: str) -> WatchConfig | None:
        return self.configs.get(config_id)

    async def create_run(
        self, *, config_id: str, folder_name: str, snapshot: Sequence[SnapshotFile]
    ) -> Run | None:
        for run in self.runs.values():
            if (
                run.config_id == config_id
                and run.folder_name == folder_name
                and not run.state.is_terminal
            ):
                return None
        now = datetime.now(timezone.utc)
        run = Run(
            id=str(uuid.uuid4()),
            config_id=config_id,
            folder_name=folder_name,
            state=RunState.PENDING,
            snapshot=tuple(snapshot),
            created_at=now,
            updated_at=now,
        )
        self.runs[run.id] = run
        return run

    async def get_run(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    async def list_runs(
        self, *, states: Sequence[RunState] | None = None, limit: int = 100
    ) -> list[Run]:
        runs = list(self.runs.values())
        if states:
            runs = [run for run in runs if run.state in states]
        return runs[:limit]

    async def open_folder_names(self, config_id: str) -> set[str]:
        return {
            run.folder_name
            for run in self.runs.values()
            if run.config_id == config_id and not run.state.is_terminal
        }

    async def last_snapshot(
        self, config_id: str, folder_name: str
    ) -> tuple[SnapshotFile, ...] | None:
        for run in reversed(list(self.runs.values())):
            if run.config_id == config_id and run.folder_name == folder_name:
                return run.snapshot
        return None

    async def next_active_run(self) -> Run | None:
        for run in self.runs.values():
            if run.state.is_active:
                return run
        return None

    async def set_state(
        self,
        run_id: str,
        state: RunState,
        *,
        error: dict[str, Any] | None = None,
        bump_attempts: bool = False,
    ) -> None:
        run = self.runs[run_id]
        self.runs[run_id] = replace(
            run,
            state=state,
            error=error,
            attempts=run.attempts + (1 if bump_attempts else 0),
            updated_at=datetime.now(timezone.utc),
        )

    async def set_plan(self, run_id: str, plan: Plan | None) -> None:
        self.runs[run_id] = replace(self.runs[run_id], plan=plan)

    async def set_result(self, run_id: str, result: RunResult) -> None:
        self.runs[run_id] = replace(self.runs[run_id], result=result)

    async def set_extra(self, run_id: str, extra: dict[str, Any]) -> None:
        self.runs[run_id] = replace(self.runs[run_id], extra=extra)

    async def set_snapshot(
        self, run_id: str, snapshot: Sequence[SnapshotFile]
    ) -> None:
        self.runs[run_id] = replace(self.runs[run_id], snapshot=tuple(snapshot))

    async def append_executed(self, run_id: str, executed: ExecutedMove) -> None:
        run = self.runs[run_id]
        self.runs[run_id] = replace(
            run, executed_moves=run.executed_moves + (executed,)
        )

    async def clear_executed(self, run_id: str) -> None:
        self.runs[run_id] = replace(self.runs[run_id], executed_moves=())

    async def log(
        self,
        run_id: str,
        message: str,
        *,
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> None:
        self.logs.append((run_id, message))

    async def list_logs(self, run_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return [
            {"message": message, "level": "info"}
            for stored_id, message in self.logs
            if stored_id == run_id
        ]

    async def add_interaction(self, run_id: str, role: str, content: str) -> None:
        self.interactions.setdefault(run_id, []).append(
            {"role": role, "content": content}
        )

    async def list_interactions(self, run_id: str) -> list[dict[str, Any]]:
        return list(self.interactions.get(run_id, []))

    async def get_settings(self) -> dict[str, Any]:
        return dict(self.settings)

    async def update_settings(self, values: dict[str, Any]) -> None:
        allowed = {
            "tmdb_api_key",
            "llm_base_url",
            "llm_api_key",
            "llm_model",
            "llm_reasoning_effort",
            "telegram_bot_token",
            "telegram_chat_id",
            "telegram_pin_alerts",
            "trash_retention_days",
        }
        self.settings.update(
            {key: value for key, value in values.items() if key in allowed}
        )

    async def create_config(self, config: WatchConfig) -> WatchConfig:
        self.configs[config.id] = config
        return config

    async def update_config(self, config_id: str, values: dict[str, Any]) -> None:
        config = self.configs[config_id]
        if "media_type" in values:
            values = {**values, "media_type": MediaType(values["media_type"])}
        if "subtitle_variant" in values:
            values = {
                **values,
                "subtitle_variant": SubtitleVariant(values["subtitle_variant"]),
            }
        if "replace_extra_dirs" in values:
            values = {
                **values,
                "replace_extra_dirs": tuple(values["replace_extra_dirs"]),
            }
        self.configs[config_id] = replace(config, **values)

    async def delete_config(self, config_id: str) -> None:
        self.configs.pop(config_id, None)

    async def delete_run(self, run_id: str) -> None:
        self.runs.pop(run_id, None)


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[Run] = []

    async def run_settled(self, run: Run, config: WatchConfig) -> None:
        self.sent.append(run)


class ScriptedModel:
    """Replays a fixed sequence of replies through the real tool loop.

    Every ``complete`` call first checks the conversation the way OpenAI
    does: an assistant message with tool calls must be answered by matching
    ``tool`` messages before anything else, or the real API rejects the
    request with "No tool output found for function call".
    """

    def __init__(self, *replies: ModelReply) -> None:
        self.replies = list(replies)
        self.seen: list[Conversation] = []

    async def complete(self, conversation, tools):
        assert_tool_calls_answered(conversation.messages)
        self.tools = tools
        self.seen.append(list(conversation.messages))
        if not self.replies:
            return ModelReply(content="I have nothing further.")
        return self.replies.pop(0)


def assert_tool_calls_answered(messages: list[dict[str, Any]]) -> None:
    index = 0
    while index < len(messages):
        message = messages[index]
        index += 1
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        expected = sorted(item["id"] for item in message["tool_calls"])
        answered = []
        while index < len(messages) and messages[index].get("role") == "tool":
            answered.append(messages[index].get("tool_call_id"))
            index += 1
        assert sorted(answered) == expected, (
            f"assistant tool calls {expected} answered by {sorted(answered)};"
            " the real API rejects this conversation"
        )


def call(name: str, **arguments: Any) -> ModelReply:
    """A model reply that invokes one tool."""

    return ModelReply(
        tool_calls=(
            ToolCall(id=f"call_{name}", name=name, arguments=json.dumps(arguments)),
        )
    )


class FakeTmdb:
    """Scripted TMDB with the same surface as the real client."""

    SERIES = {
        "tmdb_id": 123,
        "title": "Show",
        "original_title": "Show",
        "year": 2024,
        "overview": "",
        "seasons": [
            {"season": 1, "name": "Season 1", "episode_count": 12, "air_year": 2024}
        ],
    }
    MOVIE = {
        "tmdb_id": 456,
        "title": "Feature",
        "original_title": "Feature",
        "year": 2016,
        "runtime": 106,
        "overview": "",
    }

    def __init__(self, *, series=None, movie=None, poster=None) -> None:
        self.series = series if series is not None else dict(self.SERIES)
        self.movie = movie if movie is not None else dict(self.MOVIE)
        self.poster = poster
        self.calls: list[str] = []

    async def search(self, query: str, *, movie: bool):
        self.calls.append(f"search:{query}")
        source = self.movie if movie else self.series
        return [
            TmdbHit(
                tmdb_id=source["tmdb_id"],
                title=source["title"],
                original_title=source["original_title"],
                year=source["year"],
                overview="",
            )
        ]

    async def get_series(self, tmdb_id: int):
        self.calls.append(f"get_series:{tmdb_id}")
        return self.series

    async def get_movie(self, tmdb_id: int):
        self.calls.append(f"get_movie:{tmdb_id}")
        return self.movie

    async def poster_url(self, tmdb_id: int, *, movie: bool):
        self.calls.append(f"poster_url:{tmdb_id}")
        return self.poster

    async def get_season(self, tmdb_id: int, season: int):
        self.calls.append(f"get_season:{tmdb_id}:{season}")
        return {
            "tmdb_id": tmdb_id,
            "season": season,
            "name": f"Season {season}",
            "episodes": [
                {"episode": index, "name": f"Episode {index}", "air_date": ""}
                for index in range(1, 13)
            ],
        }


class StubClients:
    def __init__(self, model, tmdb) -> None:
        self._model = model
        self._tmdb = tmdb

    async def model(self):
        return self._model

    async def tmdb(self):
        return self._tmdb


class FakeProber:
    """Path-keyed probe results standing in for ffprobe."""

    def __init__(self, probes: dict[str, Any] | None = None) -> None:
        self.probes = probes or {}
        self.seen: list[str] = []

    async def __call__(self, path):
        self.seen.append(str(path))
        for key, probe in self.probes.items():
            if str(path).endswith(key):
                return probe
        return None
