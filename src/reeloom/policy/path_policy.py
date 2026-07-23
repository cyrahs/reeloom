from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from reeloom.kernel.errors import DomainError, ErrorCode


def is_forbidden_env_name(name: str) -> bool:
    """Match .env and every .env* variant without touching the filesystem."""

    return name.casefold().startswith(".env")


def _reject_env_components(parts: tuple[str, ...]) -> None:
    if any(is_forbidden_env_name(part) for part in parts):
        raise DomainError(ErrorCode.ENV_PATH_FORBIDDEN)


def validate_relative_path(value: object) -> PurePosixPath:
    """Accept one canonical, relative POSIX path with no escape semantics."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise DomainError(ErrorCode.PATH_ESCAPE)
    if "\\" in value or PureWindowsPath(value).is_absolute():
        raise DomainError(ErrorCode.PATH_ESCAPE)

    raw_parts = tuple(value.split("/"))
    if (
        value.startswith("/")
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise DomainError(ErrorCode.PATH_ESCAPE)
    _reject_env_components(raw_parts)
    return PurePosixPath(*raw_parts)


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except (FileNotFoundError, NotADirectoryError) as error:
        raise DomainError(ErrorCode.PATH_NOT_FOUND) from error
    except OSError as error:
        raise DomainError(ErrorCode.PATH_NOT_FOUND) from error


def _reject_symlink(path: Path) -> os.stat_result:
    result = _lstat(path)
    if stat.S_ISLNK(result.st_mode):
        raise DomainError(ErrorCode.SYMLINK_NOT_ALLOWED)
    return result


@dataclass(frozen=True, slots=True, init=False)
class AuthorizedRoot:
    """An absolute existing directory whose full ancestor chain has no symlink."""

    path: Path
    device: int
    inode: int

    @classmethod
    def create(cls, value: Path) -> AuthorizedRoot:
        if not isinstance(value, Path) or not value.is_absolute():
            raise DomainError(ErrorCode.PATH_NOT_ABSOLUTE)
        if ".." in value.parts:
            raise DomainError(ErrorCode.PATH_ESCAPE)
        _reject_env_components(tuple(part for part in value.parts if part != value.anchor))

        current = Path(value.anchor)
        root_stat = _reject_symlink(current)
        for part in value.parts[1:]:
            current = current / part
            root_stat = _reject_symlink(current)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise DomainError(ErrorCode.PATH_NOT_DIRECTORY)
        instance = object.__new__(cls)
        object.__setattr__(instance, "path", value)
        object.__setattr__(instance, "device", root_stat.st_dev)
        object.__setattr__(instance, "inode", root_stat.st_ino)
        return instance

    def resolve_existing(self, relative_path: object) -> Path:
        relative = validate_relative_path(relative_path)
        candidate = self.path.joinpath(*relative.parts)
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise DomainError(ErrorCode.PATH_NOT_FOUND) from None
        if not resolved.is_relative_to(self.path):
            raise DomainError(ErrorCode.PATH_ESCAPE)
        return resolved
