from __future__ import annotations

import os
import stat
from enum import StrEnum

from reeloom.adapters.filesystem import FilesystemScanner
from reeloom.kernel.errors import DomainError
from reeloom.policy.path_policy import AuthorizedRoot


class ImmutableFileErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    EXISTS = "exists"
    INVALID = "invalid"
    IO = "io"


class ImmutableFileError(RuntimeError):
    def __init__(self, code: ImmutableFileErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


def open_root(root: AuthorizedRoot) -> int:
    try:
        return FilesystemScanner._open_root(root)
    except (DomainError, OSError):
        raise ImmutableFileError(ImmutableFileErrorCode.IO) from None


def read_at(root_fd: int, name: str, *, limit: int) -> bytes:
    file_fd: int | None = None
    try:
        file_fd = os.open(name, _read_flags(), dir_fd=root_fd)
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= limit
        ):
            raise ImmutableFileError(ImmutableFileErrorCode.INVALID)
        content = _read_bounded(file_fd, limit)
        after = os.fstat(file_fd)
        if (
            len(content) != before.st_size
            or not _same_identity(before, after)
        ):
            raise ImmutableFileError(ImmutableFileErrorCode.INVALID)
        return content
    except FileNotFoundError:
        raise ImmutableFileError(
            ImmutableFileErrorCode.NOT_FOUND
        ) from None
    except ImmutableFileError:
        raise
    except OSError:
        raise ImmutableFileError(ImmutableFileErrorCode.IO) from None
    finally:
        if file_fd is not None:
            os.close(file_fd)


def write_once_at(
    root_fd: int,
    name: str,
    content: bytes,
    *,
    limit: int,
) -> None:
    if not 0 < len(content) <= limit:
        raise ImmutableFileError(ImmutableFileErrorCode.INVALID)
    file_fd: int | None = None
    try:
        file_fd = os.open(
            name,
            _write_flags(),
            0o600,
            dir_fd=root_fd,
        )
        remaining = memoryview(content)
        while remaining:
            written = os.write(file_fd, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(file_fd)
        os.fsync(root_fd)
    except FileExistsError:
        raise ImmutableFileError(
            ImmutableFileErrorCode.EXISTS
        ) from None
    except ImmutableFileError:
        raise
    except OSError:
        raise ImmutableFileError(ImmutableFileErrorCode.IO) from None
    finally:
        if file_fd is not None:
            os.close(file_fd)


def _read_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ImmutableFileError(ImmutableFileErrorCode.IO)
    return (
        os.O_RDONLY
        | no_follow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _write_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ImmutableFileError(ImmutableFileErrorCode.IO)
    return (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | no_follow
        | getattr(os, "O_CLOEXEC", 0)
    )


def _read_bounded(file_fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(file_fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    content = b"".join(chunks)
    if len(content) > limit:
        raise ImmutableFileError(ImmutableFileErrorCode.INVALID)
    return content


def _same_identity(
    before: os.stat_result,
    after: os.stat_result,
) -> bool:
    return (
        stat.S_ISREG(after.st_mode)
        and before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns
    )
