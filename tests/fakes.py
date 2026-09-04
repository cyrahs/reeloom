"""Offline stand-ins for the database and the external services.

The real repository is exercised under the ``postgres`` marker; everything
else runs against these so the default suite needs no network and no server.
"""

from __future__ import annotations

import json
import posixpath
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from reeloom.adapters.clouddrive import CloudDriveError, OfflineStatus
from reeloom.adapters.llm import Conversation, ModelReply, ToolCall
from reeloom.adapters.tmdb import TmdbHit
from reeloom.models import (
    DownloadState,
    ExecutedMove,
    MagnetDownload,
    MediaType,
    Plan,
    Run,
    RunResult,
    RunState,
    SnapshotFile,
    SubtitleVariant,
    WatchConfig,
)

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


class FakeDatabase:
    """In-memory mirror of the handful of queries the worker performs."""

    def __init__(self, configs: Sequence[WatchConfig] = ()) -> None:
        self.configs: dict[str, WatchConfig] = {c.id: c for c in configs}
        self.runs: dict[str, Run] = {}
        self.logs: list[tuple[str, str]] = []
        self.interactions: dict[str, list[dict[str, Any]]] = {}
        self.settings: dict[str, Any] = {}
        self.downloads: dict[str, MagnetDownload] = {}

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
            "clouddrive_address",
            "clouddrive_api_token",
            "clouddrive_secure",
            "download_stall_hours",
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

    # ---- magnet downloads (semantics mirror db.py, CAS included) -------

    async def create_magnet_download(
        self, *, magnet: str, info_hash: str, download_dir: str
    ) -> MagnetDownload | None:
        for download in self.downloads.values():
            if download.info_hash == info_hash and download.state.is_live:
                return None
        now = datetime.now(timezone.utc)
        download = MagnetDownload(
            id=str(uuid.uuid4()),
            magnet=magnet,
            info_hash=info_hash,
            download_dir=download_dir,
            state=DownloadState.SUBMITTED,
            submitted_at=now,
            created_at=now,
            updated_at=now,
        )
        self.downloads[download.id] = download
        return download

    async def get_magnet_download(self, download_id: str) -> MagnetDownload | None:
        return self.downloads.get(download_id)

    async def list_magnet_downloads(self, limit: int = 100) -> list[MagnetDownload]:
        ordered = sorted(
            self.downloads.values(),
            key=lambda item: item.updated_at or _EPOCH,
            reverse=True,
        )
        return ordered[:limit]

    async def live_magnet_downloads(self) -> list[MagnetDownload]:
        return sorted(
            (item for item in self.downloads.values() if item.state.is_live),
            key=lambda item: item.created_at or _EPOCH,
        )

    async def record_download_progress(
        self,
        download_id: str,
        *,
        state: DownloadState,
        progress: float | None,
        size_bytes: int | None,
        name: str | None,
        expected: Sequence[DownloadState],
    ) -> bool:
        download = self.downloads.get(download_id)
        if download is None or download.state not in expected:
            return False
        # The distinct-guard is load-bearing: updated_at must move only when
        # state or progress actually changed, or stall detection breaks.
        if download.state == state and download.progress == progress:
            return False
        self.downloads[download_id] = replace(
            download,
            state=state,
            progress=progress,
            size_bytes=size_bytes if size_bytes is not None else download.size_bytes,
            name=name if name is not None else download.name,
            updated_at=datetime.now(timezone.utc),
        )
        return True

    async def transition_download(
        self,
        download_id: str,
        *,
        expected: Sequence[DownloadState],
        target: DownloadState,
        error: str | None = None,
        final_path: str | None = None,
        name: str | None = None,
        mark_submitted: bool = False,
    ) -> bool:
        download = self.downloads.get(download_id)
        if download is None or download.state not in expected:
            return False
        now = datetime.now(timezone.utc)
        self.downloads[download_id] = replace(
            download,
            state=target,
            error=error,
            final_path=final_path if final_path is not None else download.final_path,
            name=download.name if download.name is not None else name,
            submitted_at=now if mark_submitted else download.submitted_at,
            progress=None if mark_submitted else download.progress,
            updated_at=now,
        )
        return True

    async def delete_magnet_download(self, download_id: str) -> None:
        self.downloads.pop(download_id, None)

    async def magnet_download_dirs(self, limit: int = 10) -> list[str]:
        latest: dict[str, datetime] = {}
        for download in self.downloads.values():
            created = download.created_at or _EPOCH
            if download.download_dir not in latest:
                latest[download.download_dir] = created
            else:
                latest[download.download_dir] = max(
                    latest[download.download_dir], created
                )
        ordered = sorted(latest, key=lambda key: latest[key], reverse=True)
        return ordered[:limit]


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[Run] = []

    async def run_settled(self, run: Run, config: WatchConfig) -> None:
        self.sent.append(run)


class StubDownloadClients:
    """The two Clients accessors DownloadService uses."""

    def __init__(self, cloud: Any = None, *, stall_hours: int = 24) -> None:
        self.cloud = cloud
        self.stall_hours = stall_hours

    async def clouddrive(self) -> Any:
        from reeloom.server.composition import NotConfigured

        if self.cloud is None:
            raise NotConfigured("clouddrive_not_configured")
        return self.cloud

    async def download_stall_hours(self) -> int:
        return self.stall_hours


class RecordingDownloadNotifier:
    def __init__(self) -> None:
        self.alerts: list[MagnetDownload] = []

    async def download_trouble(self, download: MagnetDownload) -> None:
        self.alerts.append(download)


class FakeCloudDrive:
    """AsyncCloudDrive surface over a tmp_path tree and a scripted task list.

    API paths map onto the local filesystem under ``root``, and ``move_file``
    performs a real rename — exactly what the FUSE mount would observe — so
    journey tests can point a watch root at the same tree.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root
        self.tasks: dict[str, dict[str, Any]] = {}
        self.added: list[tuple[list[str], str]] = []
        self.removed: list[tuple[list[str], str, bool]] = []
        self.ensured: list[tuple[str, str]] = []
        self.moves: list[tuple[str, str]] = []
        self.fail_add: Exception | None = None
        self.fail_list_offline: Exception | None = None
        self.add_result: dict[str, Any] = {
            "success": True,
            "duplicate": False,
            "error_message": "",
        }

    def script_task(
        self,
        info_hash: str,
        *,
        name: str,
        status: OfflineStatus,
        progress: float = 0.0,
        size: int = 0,
    ) -> None:
        self.tasks[info_hash.upper()] = {
            "name": name,
            "size": size,
            "url": "",
            "status": status,
            "info_hash": info_hash.upper(),
            "file_id": "",
            "add_time": 0,
            "progress": progress,
            "peers": 0,
        }

    def _local(self, api_path: str) -> Path:
        assert self.root is not None, "this FakeCloudDrive has no filesystem"
        return self.root / api_path.lstrip("/")

    async def check(self) -> dict[str, Any]:
        return {"reachable": True, "authenticated": True}

    async def list_directory(
        self, api_dir: str, *, force_refresh: bool = True
    ) -> tuple[dict[str, Any], ...]:
        base = self._local(api_dir)
        if not base.is_dir():
            raise CloudDriveError("clouddrive_path_not_found", details=api_dir)
        return tuple(
            {
                "id": "",
                "name": entry.name,
                "full_path": posixpath.join(api_dir, entry.name),
                "size": entry.stat().st_size if entry.is_file() else 0,
                "is_directory": entry.is_dir(),
            }
            for entry in sorted(base.iterdir())
        )

    async def ensure_directory(
        self, parent_api_dir: str, folder_name: str
    ) -> dict[str, Any]:
        self.ensured.append((parent_api_dir, folder_name))
        path = posixpath.join(parent_api_dir, folder_name)
        local = self._local(path)
        created = not local.is_dir()
        local.mkdir(parents=True, exist_ok=True)
        return {"created": created, "path": path}

    async def move_file(
        self, source_api_path: str, destination_api_dir: str
    ) -> dict[str, Any]:
        self.moves.append((source_api_path, destination_api_dir))
        source = self._local(source_api_path)
        destination = self._local(destination_api_dir) / source.name
        if destination.exists():
            # Conflict policy skip: report success, move nothing.
            return {"success": True, "error_message": ""}
        source.rename(destination)
        return {"success": True, "error_message": ""}

    async def add_offline_files(
        self, urls: list[str], dst_dir: str
    ) -> dict[str, Any]:
        if self.fail_add is not None:
            raise self.fail_add
        self.added.append((list(urls), dst_dir))
        return dict(self.add_result)

    async def list_offline_files(self, path: str) -> tuple[dict[str, Any], ...]:
        if self.fail_list_offline is not None:
            raise self.fail_list_offline
        return tuple(dict(task) for task in self.tasks.values())

    async def remove_offline_files(
        self, info_hashes: list[str], path: str, *, delete_files: bool
    ) -> None:
        self.removed.append((list(info_hashes), path, delete_files))
        for info_hash in info_hashes:
            self.tasks.pop(info_hash.upper(), None)


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
