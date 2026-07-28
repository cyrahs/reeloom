from __future__ import annotations

import asyncio
import concurrent.futures
import os
import queue
import stat
import threading
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TypeVar

from reeloom.kernel.archive_directory import ArchiveDirectoryCapability
from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.file_types import candidate_kind_for_filename
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import (
    AuthorizedRoot,
    is_forbidden_env_name,
    validate_relative_path,
)
from reeloom.ports.archive_directory import ArchiveDirectoryError

_MAX_ROOT_ENTRIES = 10_000
_MAX_SEARCH_RESULTS = 50
_MAX_CAPABILITIES = 256
_MAX_VIDEOS = 2_000
_MAX_DEPTH = 3
_IO_TIMEOUT_SECONDS = 10.0
_T = TypeVar("_T")


class _DirectoryIOLane:
    """One daemon worker; a timed-out network mount cannot multiply threads."""

    def __init__(self) -> None:
        self._tasks: queue.Queue[
            tuple[Callable[[], object], concurrent.futures.Future[object]]
        ] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._busy = False
        threading.Thread(
            target=self._work,
            name="reeloom-directory-io",
            daemon=True,
        ).start()

    async def run(self, operation: Callable[[], _T]) -> _T:
        future: concurrent.futures.Future[object] = (
            concurrent.futures.Future()
        )
        with self._lock:
            if self._busy:
                raise ArchiveDirectoryError(
                    "directory_io_busy",
                    retryable=True,
                )
            self._busy = True
        self._tasks.put_nowait((operation, future))
        try:
            return await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)),
                timeout=_IO_TIMEOUT_SECONDS,
            )  # type: ignore[return-value]
        except TimeoutError:
            raise ArchiveDirectoryError(
                "directory_io_timeout",
                retryable=True,
            ) from None

    def _work(self) -> None:
        while True:
            operation, future = self._tasks.get()
            try:
                future.set_result(operation())
            except BaseException as error:
                future.set_exception(error)
            finally:
                with self._lock:
                    self._busy = False


_IO_LANE = _DirectoryIOLane()


@dataclass(frozen=True, slots=True)
class _Entry:
    name: str
    kind: str
    metadata: os.stat_result


class FilesystemArchiveDirectoryBrowser:
    """A shallow, opaque-ID browser scoped to one authorized archive root."""

    def __init__(
        self,
        *,
        run_id: str,
        root: AuthorizedRoot,
        exclude_paths: frozenset[PurePosixPath] = frozenset(),
    ) -> None:
        self._run_id = run_id
        self._root = root
        self._exclude_paths = exclude_paths
        self._capabilities: dict[str, ArchiveDirectoryCapability] = {}
        self._by_path: dict[PurePosixPath, str] = {}
        self._root_cache: tuple[_Entry, ...] | None = None
        self._listing_cache: dict[str, tuple[_Entry, ...]] = {}
        self._depth_limited: set[str] = set()
        self._observed_videos: set[PurePosixPath] = set()

    def restore(
        self,
        capabilities: tuple[ArchiveDirectoryCapability, ...],
    ) -> None:
        if len(capabilities) > _MAX_CAPABILITIES:
            raise ArchiveDirectoryError(
                "directory_capability_limit",
                retryable=False,
            )
        for capability in capabilities:
            if capability.run_id != self._run_id:
                raise ArchiveDirectoryError(
                    "directory_capability_wrong_run",
                    retryable=False,
                )
            existing = self._capabilities.get(capability.directory_id)
            path_owner = self._by_path.get(capability.relative_path)
            if (
                existing is not None
                and existing != capability
                or path_owner is not None
                and path_owner != capability.directory_id
            ):
                raise ArchiveDirectoryError(
                    "directory_capability_stale",
                    retryable=False,
                )
            self._capabilities[capability.directory_id] = capability
            self._by_path[capability.relative_path] = (
                capability.directory_id
            )

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
    ]:
        del work_type
        if self._root_cache is None:
            self._root_cache = await _IO_LANE.run(self._scan_root)
        query = (
            f"tmdb-{tmdb_id}"
            if mode == "selected_tmdb_id"
            else str(name)
        )
        matches = tuple(
            entry
            for entry in self._root_cache
            if (
                _matches_tmdb_id(entry.name, tmdb_id)
                if mode == "selected_tmdb_id"
                else _fold(str(name)) in _fold(entry.name)
            )
        )
        bounded = matches[:_MAX_SEARCH_RESULTS]
        page = bounded[cursor : cursor + limit]
        capabilities = tuple(
            self._capability(
                parent_id=None,
                parent_path=PurePosixPath(),
                entry=entry,
                depth=1,
            )
            for entry in page
        )
        end = cursor + len(page)
        next_cursor = end if end < len(bounded) else None
        complete = next_cursor is None and len(matches) <= _MAX_SEARCH_RESULTS
        return capabilities, next_cursor, complete, query

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
    ]:
        capability = self._capabilities.get(directory_id)
        if capability is None:
            raise ArchiveDirectoryError(
                "unknown_directory_id",
                retryable=False,
            )
        entries = self._listing_cache.get(directory_id)
        if entries is None:
            entries = await _IO_LANE.run(
                lambda: self._scan_capability(capability)
            )
            self._listing_cache[directory_id] = entries
        else:
            await _IO_LANE.run(
                lambda: self._validate_capability(capability)
            )
        page = entries[cursor : cursor + limit]
        children: list[ArchiveDirectoryCapability] = []
        videos: list[str] = []
        for entry in page:
            relative = capability.relative_path / entry.name
            if entry.kind == "directory":
                if capability.depth >= _MAX_DEPTH:
                    self._depth_limited.add(directory_id)
                    continue
                children.append(
                    self._capability(
                        parent_id=directory_id,
                        parent_path=capability.relative_path,
                        entry=entry,
                        depth=capability.depth + 1,
                    )
                )
            else:
                if relative in self._exclude_paths:
                    continue
                self._observed_videos.add(relative)
                if len(self._observed_videos) > _MAX_VIDEOS:
                    raise ArchiveDirectoryError(
                        "directory_video_limit",
                        retryable=False,
                    )
                videos.append(entry.name)
        end = cursor + len(page)
        next_cursor = end if end < len(entries) else None
        return (
            tuple(children),
            tuple(videos),
            next_cursor,
            next_cursor is None
            and directory_id not in self._depth_limited,
        )

    def _scan_root(self) -> tuple[_Entry, ...]:
        descriptor = self._open_root()
        try:
            entries = self._scan(
                descriptor,
                max_entries=_MAX_ROOT_ENTRIES,
            )
        finally:
            os.close(descriptor)
        return tuple(item for item in entries if item.kind == "directory")

    def _scan_capability(
        self,
        capability: ArchiveDirectoryCapability,
    ) -> tuple[_Entry, ...]:
        descriptor = self._open_capability(capability)
        try:
            return self._scan(
                descriptor,
                max_entries=_MAX_CAPABILITIES + _MAX_VIDEOS,
            )
        except ArchiveDirectoryError:
            raise
        except OSError:
            raise ArchiveDirectoryError(
                "directory_capability_stale",
                retryable=True,
            ) from None
        finally:
            os.close(descriptor)

    def _validate_capability(
        self,
        capability: ArchiveDirectoryCapability,
    ) -> None:
        os.close(self._open_capability(capability))

    def _open_capability(
        self,
        capability: ArchiveDirectoryCapability,
    ) -> int:
        descriptor = self._open_root()
        try:
            for part in capability.relative_path.parts:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
            current = os.fstat(descriptor)
            if (
                current.st_dev != capability.device
                or current.st_ino != capability.inode
                or current.st_mtime_ns != capability.mtime_ns
                or current.st_ctime_ns != capability.ctime_ns
            ):
                raise ArchiveDirectoryError(
                    "directory_capability_stale",
                    retryable=True,
                )
            return descriptor
        except ArchiveDirectoryError:
            os.close(descriptor)
            raise
        except OSError:
            os.close(descriptor)
            raise ArchiveDirectoryError(
                "directory_capability_stale",
                retryable=True,
            ) from None

    def _open_root(self) -> int:
        try:
            descriptor = os.open(
                self._root.path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            current = os.fstat(descriptor)
            if (
                current.st_dev != self._root.device
                or current.st_ino != self._root.inode
            ):
                os.close(descriptor)
                raise ArchiveDirectoryError(
                    "directory_root_stale",
                    retryable=True,
                )
            return descriptor
        except ArchiveDirectoryError:
            raise
        except OSError:
            raise ArchiveDirectoryError(
                "directory_io_failed",
                retryable=True,
            ) from None

    @staticmethod
    def _scan(
        descriptor: int,
        *,
        max_entries: int,
    ) -> tuple[_Entry, ...]:
        result: list[_Entry] = []
        observed = 0
        try:
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    observed += 1
                    if observed > max_entries:
                        raise ArchiveDirectoryError(
                            "directory_too_large",
                            retryable=False,
                        )
                    name = entry.name
                    try:
                        encoded_name = name.encode("utf-8")
                    except UnicodeError:
                        continue
                    if (
                        name.startswith(".")
                        or "\\" in name
                        or is_forbidden_env_name(name)
                        or len(encoded_name) > 255
                    ):
                        continue
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        kind = "directory"
                    elif (
                        stat.S_ISREG(metadata.st_mode)
                        and candidate_kind_for_filename(name)
                        is CandidateKind.VIDEO
                    ):
                        kind = "video"
                    else:
                        continue
                    result.append(_Entry(name, kind, metadata))
        except ArchiveDirectoryError:
            raise
        except OSError:
            raise ArchiveDirectoryError(
                "directory_io_failed",
                retryable=True,
            ) from None
        result.sort(key=lambda item: (_fold(item.name), item.name))
        return tuple(result)

    def _capability(
        self,
        *,
        parent_id: str | None,
        parent_path: PurePosixPath,
        entry: _Entry,
        depth: int,
    ) -> ArchiveDirectoryCapability:
        relative = (
            parent_path / entry.name
            if parent_path.parts
            else PurePosixPath(entry.name)
        )
        validate_relative_path(relative.as_posix())
        existing_id = self._by_path.get(relative)
        if existing_id is not None:
            existing = self._capabilities[existing_id]
            if (
                existing.parent_id != parent_id
                or existing.device != entry.metadata.st_dev
                or existing.inode != entry.metadata.st_ino
                or existing.mtime_ns != entry.metadata.st_mtime_ns
                or existing.ctime_ns != entry.metadata.st_ctime_ns
            ):
                raise ArchiveDirectoryError(
                    "directory_capability_stale",
                    retryable=True,
                )
            return existing
        if len(self._capabilities) >= _MAX_CAPABILITIES:
            raise ArchiveDirectoryError(
                "directory_capability_limit",
                retryable=False,
            )
        capability = ArchiveDirectoryCapability(
            run_id=self._run_id,
            directory_id=f"dir-{uuid.uuid4().hex}",
            parent_id=parent_id,
            relative_path=relative,
            name=entry.name,
            depth=depth,
            device=entry.metadata.st_dev,
            inode=entry.metadata.st_ino,
            mtime_ns=entry.metadata.st_mtime_ns,
            ctime_ns=entry.metadata.st_ctime_ns,
        )
        self._capabilities[capability.directory_id] = capability
        self._by_path[relative] = capability.directory_id
        return capability


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _matches_tmdb_id(name: str, tmdb_id: int) -> bool:
    folded = _fold(name)
    needle = f"tmdb-{tmdb_id}"
    start = 0
    while (index := folded.find(needle, start)) >= 0:
        before = folded[index - 1] if index else ""
        end = index + len(needle)
        after = folded[end] if end < len(folded) else ""
        if (not before or not before.isalnum()) and (
            not after or not after.isdigit()
        ):
            return True
        start = index + 1
    return False
