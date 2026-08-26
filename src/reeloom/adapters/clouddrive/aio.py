"""Async, cancellation-safe facade over the synchronous CloudDrive client.

Every call runs the blocking gRPC operation in a worker thread and, when the
awaiting task is cancelled, still waits for that thread to finish before
re-raising the cancellation: a caller can never observe a cancelled
move/create while the underlying RPC is still in flight.

gRPC errors are translated here into :class:`CloudDriveError` with stable
codes, so nothing above this module imports grpc.
"""

from __future__ import annotations

import asyncio
import posixpath
from collections.abc import Callable
from contextlib import suppress
from enum import StrEnum
from typing import Any

import grpc

from reeloom.adapters.clouddrive import clouddrive_pb2
from reeloom.adapters.clouddrive.client import CloudDriveClient
from reeloom.models import ReeloomError

CloudFile = dict[str, object]
OfflineTask = dict[str, object]

_DETAILS_LIMIT = 200


class CloudDriveError(ReeloomError):
    pass


class OfflineStatus(StrEnum):
    """CloudDrive's offline-task status, by proto enum name."""

    INIT = "OFFLINE_INIT"
    DOWNLOADING = "OFFLINE_DOWNLOADING"
    FINISHED = "OFFLINE_FINISHED"
    ERROR = "OFFLINE_ERROR"
    UNKNOWN = "OFFLINE_UNKNOWN"


_ASCII_CONTROL_LIMIT = 32
_ASCII_DELETE = 127
MOVE_CONFLICT_SKIP = 2

#: CloudDrive's own duplicate detection, surfaced as an RpcError detail. The
#: string match is brittle but load-bearing: tracking is keyed on the info
#: hash, so "already queued" is as good as a fresh submission.
_DUPLICATE_TASK_DETAIL = "任务已存在"


def validate_api_path(value: str, *, allow_root: bool) -> str:
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\x00" in value
        or "\\" in value
        or any(
            ord(character) < _ASCII_CONTROL_LIMIT or ord(character) == _ASCII_DELETE
            for character in value
        )
        or posixpath.normpath(value) != value
        or (not allow_root and value == "/")
    ):
        raise CloudDriveError("clouddrive_invalid_path", path=value)
    return value


def validate_path_segment(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or any(
            ord(character) < _ASCII_CONTROL_LIMIT or ord(character) == _ASCII_DELETE
            for character in value
        )
    ):
        raise CloudDriveError("clouddrive_invalid_name", name=value[:80])
    return value


def _translate(error: grpc.RpcError) -> CloudDriveError:
    code = getattr(error, "code", lambda: None)()
    details = str(getattr(error, "details", lambda: "")() or "")[:_DETAILS_LIMIT]
    if code in (grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED):
        return CloudDriveError("clouddrive_unauthorized")
    if code == grpc.StatusCode.UNAVAILABLE:
        return CloudDriveError("clouddrive_unreachable", details=details)
    if code == grpc.StatusCode.DEADLINE_EXCEEDED:
        return CloudDriveError("clouddrive_timeout")
    if code == grpc.StatusCode.NOT_FOUND:
        return CloudDriveError("clouddrive_path_not_found", details=details)
    return CloudDriveError("clouddrive_error", details=details)


def _cloud_file_to_dict(file: Any) -> CloudFile:
    return {
        "id": str(file.id),
        "name": str(file.name),
        "full_path": str(file.fullPathName),
        "size": int(file.size),
        "is_directory": bool(file.isDirectory),
    }


def _offline_file_to_dict(file: Any) -> OfflineTask:
    try:
        status = OfflineStatus(clouddrive_pb2.OfflineFileStatus.Name(file.status))
    except ValueError:
        status = OfflineStatus.UNKNOWN
    return {
        "name": str(file.name),
        "size": int(file.size),
        "url": str(file.url),
        "status": status,
        # Normalized to match the hashes extracted from our own magnets.
        "info_hash": str(file.infoHash).upper(),
        "file_id": str(file.fileId),
        "add_time": int(file.add_time),
        # The typo is in the proto. A percentage (0-100), not a fraction.
        "progress": float(file.percendDone),
        "peers": int(file.peers),
    }


async def _run_sync_complete(
    function: Callable[..., Any], *args: object, **kwargs: object
) -> Any:
    """Wait for a sync gRPC call to finish even when its caller is cancelled."""

    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    completed = asyncio.get_running_loop().create_future()

    def notify_done(_task: asyncio.Task[Any]) -> None:
        if not completed.done():
            completed.set_result(None)

    task.add_done_callback(notify_done)
    cancelled = False
    while not completed.done():
        try:
            await asyncio.shield(completed)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        with suppress(Exception):
            task.result()
        raise asyncio.CancelledError
    try:
        return task.result()
    except grpc.RpcError as error:
        raise _translate(error) from error


class AsyncCloudDrive:
    def __init__(self, client: CloudDriveClient) -> None:
        self._client = client

    def __repr__(self) -> str:
        return f"AsyncCloudDrive({self._client!r})"

    async def aclose(self) -> None:
        await asyncio.to_thread(self._client.close)

    async def check(self) -> dict[str, object]:
        """Connectivity probe: unauthenticated ping plus one authenticated listing."""

        await _run_sync_complete(self._client.get_system_info)
        await self.list_directory("/")
        return {"reachable": True, "authenticated": True}

    async def list_directory(
        self, api_dir: str, *, force_refresh: bool = True
    ) -> tuple[CloudFile, ...]:
        """Return CloudDrive metadata for one API-native directory path."""

        directory = validate_api_path(api_dir, allow_root=True)
        files = await _run_sync_complete(
            self._client.get_sub_files, directory, force_refresh=force_refresh
        )
        return tuple(_cloud_file_to_dict(file) for file in files)

    async def ensure_directory(
        self, parent_api_dir: str, folder_name: str
    ) -> dict[str, object]:
        """Ensure one direct child directory exists, verified by a fresh listing.

        The follow-up listing, not the RPC result, decides the outcome: a
        create that timed out or raced an existing folder still counts once
        the listing shows the directory.
        """

        parent = validate_api_path(parent_api_dir, allow_root=True)
        name = validate_path_segment(folder_name)
        expected_path = posixpath.join(parent, name)

        def find(files: tuple[CloudFile, ...]) -> CloudFile | None:
            return next(
                (
                    file
                    for file in files
                    if file["full_path"] == expected_path and file["name"] == name
                ),
                None,
            )

        existing = find(await self.list_directory(parent))
        if existing is not None:
            if not existing["is_directory"]:
                raise CloudDriveError("clouddrive_not_a_directory", path=expected_path)
            return {"created": False, "path": expected_path}

        create_error: Exception | None = None
        try:
            await _run_sync_complete(self._client.create_folder, parent, name)
        except CloudDriveError as exc:
            create_error = exc
        created = find(await self.list_directory(parent))
        if created is not None and bool(created["is_directory"]):
            return {"created": True, "path": expected_path}
        if create_error is not None:
            raise create_error
        raise CloudDriveError("clouddrive_create_failed", path=expected_path)

    async def move_file(
        self, source_api_path: str, destination_api_dir: str
    ) -> dict[str, object]:
        """Move one CloudDrive file without overwriting an existing destination."""

        source = validate_api_path(source_api_path, allow_root=False)
        destination = validate_api_path(destination_api_dir, allow_root=True)
        result = await _run_sync_complete(
            self._client.move_file, [source], destination, MOVE_CONFLICT_SKIP
        )
        return {
            "success": bool(result.success),
            "error_message": str(result.errorMessage),
        }

    async def add_offline_files(
        self, urls: list[str], dst_dir: str
    ) -> dict[str, object]:
        """Submit magnets as offline tasks. CloudDrive's own "task already
        exists" rejection counts as success, flagged ``duplicate``."""

        directory = validate_api_path(dst_dir, allow_root=False)
        try:
            result = await _run_sync_complete(
                self._client.add_offline_file, urls, directory
            )
        except CloudDriveError as exc:
            if _DUPLICATE_TASK_DETAIL in str(exc.context.get("details", "")):
                return {"success": True, "duplicate": True, "error_message": ""}
            raise
        return {
            "success": bool(result.success),
            "duplicate": False,
            "error_message": str(result.errorMessage),
        }

    async def list_offline_files(self, path: str) -> tuple[OfflineTask, ...]:
        """Every offline task under path, whatever its status.

        This is the poll's view of what CloudDrive is doing: each task carries
        the infoHash that ties it back to a magnet_download row, along with
        the progress needed to tell a slow download from a stalled one.
        """

        directory = validate_api_path(path, allow_root=True)
        files = await _run_sync_complete(
            self._client.list_offline_files_by_path, directory
        )
        return tuple(_offline_file_to_dict(file) for file in files)

    async def remove_offline_files(
        self, info_hashes: list[str], path: str, *, delete_files: bool
    ) -> None:
        """Drop offline tasks by info hash, optionally with their data.

        The path names the cloud the tasks live in, the same way the listing
        addresses it; any directory of that cloud works.
        """

        directory = validate_api_path(path, allow_root=True)
        await _run_sync_complete(
            self._client.remove_offline_files,
            info_hashes,
            directory,
            delete_files=delete_files,
        )
