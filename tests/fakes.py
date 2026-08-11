"""Offline stand-ins for the database and the external services.

The real repository is exercised under the ``postgres`` marker; everything
else runs against these so the default suite needs no network and no server.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any, Sequence

from reeloom.models import (
    ExecutedMove,
    Plan,
    Run,
    RunResult,
    RunState,
    SnapshotFile,
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
        run = Run(
            id=str(uuid.uuid4()),
            config_id=config_id,
            folder_name=folder_name,
            state=RunState.PENDING,
            snapshot=tuple(snapshot),
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
        )

    async def set_plan(self, run_id: str, plan: Plan | None) -> None:
        self.runs[run_id] = replace(self.runs[run_id], plan=plan)

    async def set_result(self, run_id: str, result: RunResult) -> None:
        self.runs[run_id] = replace(self.runs[run_id], result=result)

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

    async def set_extra(self, run_id: str, values: dict[str, Any]) -> None:
        run = self.runs[run_id]
        self.runs[run_id] = replace(run, extra={**run.extra, **values})

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
        return self.interactions.get(run_id, [])

    async def get_settings(self) -> dict[str, Any]:
        return dict(self.settings)


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[Run] = []

    async def run_settled(self, run: Run, config: WatchConfig) -> None:
        self.sent.append(run)
