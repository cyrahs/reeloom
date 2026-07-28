from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from reeloom.kernel.tmdb import TmdbWorkType

ArchiveSearchMode = Literal["selected_tmdb_id", "name"]


def _bounded_text(value: object, *, maximum: int = 4_096) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= maximum
    )


@dataclass(frozen=True, slots=True)
class ArchiveDirectoryCapability:
    run_id: str
    directory_id: str
    parent_id: str | None
    relative_path: PurePosixPath
    name: str
    depth: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int

    def __post_init__(self) -> None:
        if (
            not _bounded_text(self.run_id, maximum=128)
            or not _bounded_text(self.directory_id, maximum=128)
            or (
                self.parent_id is not None
                and not _bounded_text(self.parent_id, maximum=128)
            )
            or not isinstance(self.relative_path, PurePosixPath)
            or self.relative_path.is_absolute()
            or not self.relative_path.parts
            or ".." in self.relative_path.parts
            or not _bounded_text(self.name, maximum=255)
            or self.name != self.relative_path.name
            or type(self.depth) is not int
            or not 1 <= self.depth <= 3
            or len(self.relative_path.parts) != self.depth
            or any(
                type(value) is not int or value < 0
                for value in (
                    self.device,
                    self.inode,
                    self.mtime_ns,
                    self.ctime_ns,
                )
            )
        ):
            raise ValueError("invalid archive directory capability")


@dataclass(frozen=True, slots=True)
class ArchiveSearchRecord:
    call_id: str
    mode: ArchiveSearchMode
    query: str
    tmdb_id: int
    work_type: TmdbWorkType
    directory_ids: tuple[str, ...]
    cursor: int
    next_cursor: int | None
    complete: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        if (
            not _bounded_text(self.call_id, maximum=128)
            or self.mode not in {"selected_tmdb_id", "name"}
            or not _bounded_text(self.query, maximum=256)
            or type(self.tmdb_id) is not int
            or not 1 <= self.tmdb_id <= 9_999_999_999
            or not isinstance(self.work_type, TmdbWorkType)
            or not isinstance(self.directory_ids, tuple)
            or len(self.directory_ids) > 50
            or len(set(self.directory_ids)) != len(self.directory_ids)
            or any(
                not _bounded_text(item, maximum=128)
                for item in self.directory_ids
            )
            or type(self.cursor) is not int
            or not 0 <= self.cursor <= 50
            or (
                self.next_cursor is not None
                and (
                    type(self.next_cursor) is not int
                    or not self.cursor < self.next_cursor <= 50
                    or self.complete
                )
            )
            or type(self.complete) is not bool
            or not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValueError("invalid archive search record")


@dataclass(frozen=True, slots=True)
class ArchiveDirectoryListing:
    call_id: str
    directory_id: str
    child_ids: tuple[str, ...]
    videos: tuple[str, ...]
    occupied: tuple[tuple[int, int], ...]
    cursor: int
    next_cursor: int | None
    complete: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        if (
            not _bounded_text(self.call_id, maximum=128)
            or not _bounded_text(self.directory_id, maximum=128)
            or not isinstance(self.child_ids, tuple)
            or len(set(self.child_ids)) != len(self.child_ids)
            or any(
                not _bounded_text(item, maximum=128)
                for item in self.child_ids
            )
            or not isinstance(self.videos, tuple)
            or any(
                not _bounded_text(item, maximum=255)
                for item in self.videos
            )
            or not isinstance(self.occupied, tuple)
            or tuple(sorted(set(self.occupied))) != self.occupied
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or type(item[0]) is not int
                or not 0 <= item[0] <= 999
                or type(item[1]) is not int
                or not 1 <= item[1] <= 100_000
                for item in self.occupied
            )
            or type(self.cursor) is not int
            or not 0 <= self.cursor <= 2_256
            or (
                self.next_cursor is not None
                and (
                    type(self.next_cursor) is not int
                    or not self.cursor < self.next_cursor <= 2_256
                    or self.complete
                )
            )
            or type(self.complete) is not bool
            or not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValueError("invalid archive directory listing")
