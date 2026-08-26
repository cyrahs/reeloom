"""CloudDrive2 adapter: sync client with an injected stub, async facade."""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from reeloom.adapters.clouddrive import (
    AsyncCloudDrive,
    CloudDriveClient,
    CloudDriveError,
    OfflineStatus,
    clouddrive_pb2 as pb,
    validate_api_path,
    validate_path_segment,
)
from reeloom.adapters.clouddrive.client import (
    GRPC_TIMEOUT_SECONDS,
    MOVE_TIMEOUT_SECONDS,
)


class FakeRpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode, details: str = "") -> None:
        self._code = code
        self._details = details

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return self._details


class RecordingStub:
    """Stands in for CloudDriveFileSrvStub: records every call, replays
    scripted responses or errors."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, list | None, float | None]] = []
        self.responses: dict[str, Any] = {}
        self.errors: dict[str, Exception] = {}

    def __getattr__(self, method: str):
        def call(request: Any, metadata: Any = None, timeout: Any = None) -> Any:
            self.calls.append((method, request, metadata, timeout))
            if method in self.errors:
                raise self.errors[method]
            return self.responses.get(method)

        return call

    def last(self, method: str) -> tuple[str, Any, list | None, float | None]:
        for call in reversed(self.calls):
            if call[0] == method:
                return call
        raise AssertionError(f"{method} was never called")


def make_client() -> tuple[CloudDriveClient, RecordingStub]:
    stub = RecordingStub()
    client = CloudDriveClient(
        address="cloud.internal:19798", api_token="secret-token", stub=stub
    )
    return client, stub


def make_cloud() -> tuple[AsyncCloudDrive, RecordingStub]:
    client, stub = make_client()
    return AsyncCloudDrive(client), stub


# ---- sync client ------------------------------------------------------


def test_every_rpc_but_system_info_carries_the_bearer() -> None:
    client, stub = make_client()
    stub.responses["GetSubFiles"] = [pb.SubFilesReply()]
    client.get_system_info()
    client.get_sub_files("/x")
    client.create_folder("/x", "y")
    client.move_file(["/x/a"], "/y", 2)
    client.add_offline_file("magnet:?x", "/x")
    stub.responses["ListOfflineFilesByPath"] = pb.OfflineFileListResult()
    client.list_offline_files_by_path("/x")
    client.remove_offline_files(["ABC"], "/x", delete_files=True)

    for method, _, metadata, _ in stub.calls:
        if method == "GetSystemInfo":
            assert metadata is None
        else:
            assert metadata == [("authorization", "Bearer secret-token")]


def test_move_file_gets_its_own_timeout() -> None:
    client, stub = make_client()
    client.move_file(["/x/a"], "/y", 2)
    client.create_folder("/x", "y")
    assert stub.last("MoveFile")[3] == MOVE_TIMEOUT_SECONDS
    assert stub.last("CreateFolder")[3] == GRPC_TIMEOUT_SECONDS


def test_add_offline_joins_urls_and_keeps_check_folder_nonzero() -> None:
    """checkFolderAfterSecs=0 disables the re-check, and with a persistent
    directory cache (115) the finished download would stay invisible."""

    client, stub = make_client()
    client.add_offline_file(["magnet:?a", "magnet:?b"], "/dl/in_progress")
    request = stub.last("AddOfflineFiles")[1]
    assert request.urls == "magnet:?a\nmagnet:?b"
    assert request.toFolder == "/dl/in_progress"
    assert request.checkFolderAfterSecs > 0


def test_repr_redacts_the_token() -> None:
    client, _ = make_client()
    assert "secret-token" not in repr(client)


# ---- path validation --------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["relative", "//double", "/a/../b", "/a/", "/a\\b", "/a\x00b", "/a/./b"],
)
def test_invalid_api_paths_are_rejected(value: str) -> None:
    with pytest.raises(CloudDriveError) as info:
        validate_api_path(value, allow_root=False)
    assert info.value.code == "clouddrive_invalid_path"


def test_root_path_needs_explicit_permission() -> None:
    assert validate_api_path("/", allow_root=True) == "/"
    with pytest.raises(CloudDriveError):
        validate_api_path("/", allow_root=False)


@pytest.mark.parametrize("value", ["", ".", "..", "a/b", "a\\b", "a\x00b", "a\x1fb"])
def test_invalid_path_segments_are_rejected(value: str) -> None:
    with pytest.raises(CloudDriveError) as info:
        validate_path_segment(value)
    assert info.value.code == "clouddrive_invalid_name"


# ---- async facade -----------------------------------------------------


async def test_rpc_errors_become_stable_codes() -> None:
    cases = {
        grpc.StatusCode.UNAUTHENTICATED: "clouddrive_unauthorized",
        grpc.StatusCode.PERMISSION_DENIED: "clouddrive_unauthorized",
        grpc.StatusCode.UNAVAILABLE: "clouddrive_unreachable",
        grpc.StatusCode.DEADLINE_EXCEEDED: "clouddrive_timeout",
        grpc.StatusCode.NOT_FOUND: "clouddrive_path_not_found",
        grpc.StatusCode.INTERNAL: "clouddrive_error",
    }
    for status, expected in cases.items():
        cloud, stub = make_cloud()
        stub.errors["GetSubFiles"] = FakeRpcError(status, "boom")
        with pytest.raises(CloudDriveError) as info:
            await cloud.list_directory("/x")
        assert info.value.code == expected


async def test_duplicate_task_rejection_counts_as_success() -> None:
    cloud, stub = make_cloud()
    stub.errors["AddOfflineFiles"] = FakeRpcError(
        grpc.StatusCode.INTERNAL, "添加离线任务失败: 任务已存在"
    )
    result = await cloud.add_offline_files(["magnet:?x"], "/dl/in_progress")
    assert result == {"success": True, "duplicate": True, "error_message": ""}


async def test_add_offline_reports_the_servers_answer() -> None:
    cloud, stub = make_cloud()
    stub.responses["AddOfflineFiles"] = pb.FileOperationResult(
        success=False, errorMessage="quota exceeded"
    )
    result = await cloud.add_offline_files(["magnet:?x"], "/dl/in_progress")
    assert result["success"] is False
    assert result["error_message"] == "quota exceeded"


async def test_offline_tasks_are_normalized() -> None:
    cloud, stub = make_cloud()
    stub.responses["ListOfflineFilesByPath"] = pb.OfflineFileListResult(
        offlineFiles=[
            pb.OfflineFile(
                name="Show S01",
                size=1234,
                status=pb.OfflineFileStatus.OFFLINE_DOWNLOADING,
                infoHash="c9e15763f722f23e98a29decdfae341b98d53056",
                percendDone=33.33,
            ),
            pb.OfflineFile(name="odd", status=4),
        ]
    )
    tasks = await cloud.list_offline_files("/dl/in_progress")
    assert tasks[0]["info_hash"] == "C9E15763F722F23E98A29DECDFAE341B98D53056"
    assert tasks[0]["status"] is OfflineStatus.DOWNLOADING
    assert tasks[0]["progress"] == pytest.approx(33.33)
    assert tasks[1]["status"] is OfflineStatus.UNKNOWN


async def test_ensure_directory_trusts_the_listing_not_the_rpc() -> None:
    """A create that raced an existing folder still counts once the fresh
    listing shows the directory."""

    cloud, stub = make_cloud()
    existing = pb.SubFilesReply(
        subFiles=[
            pb.CloudDriveFile(name="in_progress", fullPathName="/dl/in_progress", isDirectory=True)
        ]
    )
    stub.responses["GetSubFiles"] = [existing]
    stub.errors["CreateFolder"] = FakeRpcError(grpc.StatusCode.INTERNAL, "exists")
    result = await cloud.ensure_directory("/dl", "in_progress")
    assert result["path"] == "/dl/in_progress"
    assert result["created"] is False


async def test_check_pings_then_lists_root() -> None:
    cloud, stub = make_cloud()
    stub.responses["GetSystemInfo"] = pb.CloudDriveSystemInfo()
    stub.responses["GetSubFiles"] = [pb.SubFilesReply()]
    result = await cloud.check()
    assert result == {"reachable": True, "authenticated": True}
    assert [call[0] for call in stub.calls] == ["GetSystemInfo", "GetSubFiles"]


async def test_move_file_uses_conflict_skip() -> None:
    cloud, stub = make_cloud()
    stub.responses["MoveFile"] = pb.FileOperationResult(success=True)
    await cloud.move_file("/dl/in_progress/Show", "/dl")
    request = stub.last("MoveFile")[1]
    assert request.conflictPolicy == pb.MoveFileRequest.ConflictPolicy.Skip
    assert list(request.theFilePaths) == ["/dl/in_progress/Show"]
