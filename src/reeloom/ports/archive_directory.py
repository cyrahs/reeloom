from __future__ import annotations

from typing import Protocol

from reeloom.kernel.archive_directory import ArchiveDirectoryCapability
from reeloom.kernel.tmdb import TmdbWorkType


class ArchiveDirectoryError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class ArchiveDirectoryBrowser(Protocol):
    def restore(
        self,
        capabilities: tuple[ArchiveDirectoryCapability, ...],
    ) -> None: ...

    async def search(
        self,
        *,
        work_type: TmdbWorkType,
        tmdb_id: int,
        mode: str,
        name: str | None,
        cursor: int,
        limit: int,
    ) -> tuple[
        tuple[ArchiveDirectoryCapability, ...],
        int | None,
        bool,
        str,
    ]: ...

    async def list(
        self,
        *,
        directory_id: str,
        cursor: int,
        limit: int,
    ) -> tuple[
        tuple[ArchiveDirectoryCapability, ...],
        tuple[str, ...],
        int | None,
        bool,
    ]: ...
