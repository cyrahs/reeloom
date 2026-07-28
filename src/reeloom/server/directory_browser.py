from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

from reeloom.kernel.errors import DomainError
from reeloom.policy.path_policy import (
    is_forbidden_env_name,
    validate_relative_path,
)
from reeloom.server.errors import ServerError, ServerErrorCode

_MAX_DIRECTORIES = 1_000


class PodDirectoryBrowser:
    """List directories visible inside this process without following links."""

    def __init__(self, root: Path = Path("/")) -> None:
        if not root.is_absolute():
            raise ServerError(ServerErrorCode.INVALID_SETTINGS)
        self._root = root

    def list(self, relative_path: str) -> dict[str, object]:
        relative = self._relative(relative_path)
        directory_fd = self._open(relative)
        try:
            directories: list[dict[str, str]] = []
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    name = entry.name
                    if is_forbidden_env_name(name):
                        continue
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                        name.encode("utf-8", errors="strict")
                    except (OSError, UnicodeError):
                        continue
                    if not stat.S_ISDIR(metadata.st_mode):
                        continue
                    child = (
                        relative / name
                        if relative.parts
                        else PurePosixPath(name)
                    )
                    try:
                        validate_relative_path(child.as_posix())
                    except DomainError:
                        continue
                    directories.append(
                        {"name": name, "path": child.as_posix()}
                    )
                    if len(directories) > _MAX_DIRECTORIES:
                        raise ServerError(
                            ServerErrorCode.DIRECTORY_TOO_LARGE
                        )
            directories.sort(
                key=lambda item: (item["name"].casefold(), item["name"])
            )
            path = "" if not relative.parts else relative.as_posix()
            absolute = self._root if not path else self._root / path
            parent = (
                None
                if not relative.parts
                else (
                    ""
                    if len(relative.parts) == 1
                    else relative.parent.as_posix()
                )
            )
            return {
                "path": path,
                "absolute_path": absolute.as_posix(),
                "parent": parent,
                "directories": directories,
            }
        finally:
            os.close(directory_fd)

    @staticmethod
    def _relative(value: str) -> PurePosixPath:
        if value == "":
            return PurePosixPath()
        try:
            return validate_relative_path(value)
        except DomainError:
            raise ServerError(
                ServerErrorCode.INVALID_DIRECTORY_PATH
            ) from None

    def _open(self, relative: PurePosixPath) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            directory_fd = os.open(self._root, flags)
            for part in relative.parts:
                next_fd = os.open(part, flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            return directory_fd
        except OSError:
            if "directory_fd" in locals():
                os.close(directory_fd)
            raise ServerError(ServerErrorCode.DIRECTORY_NOT_FOUND) from None
