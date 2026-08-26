"""Synchronous CloudDrive2 gRPC client.

CloudDrive2 speaks gRPC, not HTTP, so this adapter cannot ride httpx like the
others. The generated stubs are vendored (``clouddrive_pb2*.py``); everything
above this module talks to :class:`AsyncCloudDrive` in ``aio.py`` and never
imports grpc directly.
"""

from __future__ import annotations

from typing import Any

import grpc
from google.protobuf import empty_pb2

from reeloom.adapters.clouddrive import clouddrive_pb2, clouddrive_pb2_grpc

GRPC_TIMEOUT_SECONDS = 30.0

#: MoveFile gets its own budget: a cloud-side move is executed by the provider
#: and can take far longer than a listing. Cutting it off at the general
#: timeout leaves the move in an unknown state that then has to be observed to
#: a conclusion.
MOVE_TIMEOUT_SECONDS = 300.0

#: How soon CloudDrive re-checks the destination folder after an offline task
#: is added. Zero disables the check, and with a persistent directory cache
#: (115) nothing else ever expires the folder listing, so the finished
#: download would stay invisible to both the API and the mount.
CHECK_FOLDER_AFTER_SECONDS = 10


class CloudDriveClient:
    def __init__(
        self,
        *,
        address: str,
        api_token: str,
        secure: bool = True,
        stub: Any | None = None,
    ) -> None:
        """gRPC client for one CloudDrive server.

        ``stub`` is the test seam, the way ``transport=`` is for the httpx
        adapters: when given, no channel is built and every RPC goes to the
        injected object.
        """

        self.__api_token = api_token
        if stub is not None:
            self._channel = None
            self._stub = stub
            return
        self._channel = (
            grpc.secure_channel(address, grpc.ssl_channel_credentials())
            if secure
            else grpc.insecure_channel(address)
        )
        self._stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(self._channel)

    def __repr__(self) -> str:
        return "CloudDriveClient(address=<redacted>, api_token=<redacted>)"

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()

    def _metadata(self) -> list[tuple[str, str]]:
        return [("authorization", f"Bearer {self.__api_token}")]

    def get_system_info(self) -> Any:
        """Unauthenticated server ping."""

        return self._stub.GetSystemInfo(
            empty_pb2.Empty(), timeout=GRPC_TIMEOUT_SECONDS
        )

    def get_sub_files(self, path: str, *, force_refresh: bool = False) -> list[Any]:
        request = clouddrive_pb2.ListSubFileRequest(
            path=path, forceRefresh=force_refresh
        )
        files: list[Any] = []
        for response in self._stub.GetSubFiles(
            request, metadata=self._metadata(), timeout=GRPC_TIMEOUT_SECONDS
        ):
            files.extend(response.subFiles)
        return files

    def create_folder(self, parent_path: str, folder_name: str) -> Any:
        request = clouddrive_pb2.CreateFolderRequest(
            parentPath=parent_path, folderName=folder_name
        )
        return self._stub.CreateFolder(
            request, metadata=self._metadata(), timeout=GRPC_TIMEOUT_SECONDS
        )

    def move_file(
        self, source_paths: list[str], dest_path: str, conflict_policy: int
    ) -> Any:
        """conflict_policy: 0=overwrite, 1=rename, 2=skip."""

        request = clouddrive_pb2.MoveFileRequest(
            theFilePaths=source_paths,
            destPath=dest_path,
            conflictPolicy=conflict_policy,
        )
        return self._stub.MoveFile(
            request, metadata=self._metadata(), timeout=MOVE_TIMEOUT_SECONDS
        )

    def add_offline_file(
        self,
        urls: str | list[str],
        dst_dir: str,
        *,
        check_folder_after_secs: int = CHECK_FOLDER_AFTER_SECONDS,
    ) -> Any:
        if isinstance(urls, str):
            urls = [urls]
        request = clouddrive_pb2.AddOfflineFileRequest(
            urls="\n".join(urls),
            toFolder=dst_dir,
            checkFolderAfterSecs=check_folder_after_secs,
        )
        return self._stub.AddOfflineFiles(
            request, metadata=self._metadata(), timeout=GRPC_TIMEOUT_SECONDS
        )

    def list_offline_files_by_path(self, path: str) -> list[Any]:
        """Every offline task under path, whatever its status."""

        request = clouddrive_pb2.FileRequest(path=path)
        result = self._stub.ListOfflineFilesByPath(
            request, metadata=self._metadata(), timeout=GRPC_TIMEOUT_SECONDS
        )
        return list(result.offlineFiles)

    def remove_offline_files(
        self, info_hashes: list[str], path: str, *, delete_files: bool
    ) -> Any:
        """Drop offline tasks by info hash; the path names the cloud they live in."""

        request = clouddrive_pb2.RemoveOfflineFilesRequest(
            infoHashes=info_hashes,
            path=path,
            deleteFiles=delete_files,
        )
        return self._stub.RemoveOfflineFiles(
            request, metadata=self._metadata(), timeout=GRPC_TIMEOUT_SECONDS
        )
