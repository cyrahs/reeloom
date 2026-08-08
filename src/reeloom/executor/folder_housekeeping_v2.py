from __future__ import annotations

import errno
import hashlib
import os
import stat
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from reeloom.executor.atomic_rename import (
    AtomicRenameFailure,
    classify_atomic_rename_error,
    rename_noreplace,
)
from reeloom.executor.effect_mutex import effect_mutex
from reeloom.kernel.naming import filesystem_name_key


class FolderHousekeepingOutcome(StrEnum):
    COMPLETED = "completed"
    COLLISION = "collision"
    UNSAFE = "unsafe"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class FolderHousekeepingResult:
    outcome: FolderHousekeepingOutcome
    warning: str | None = None


def housekeeping_target_name(source_folder: str, run_id: str) -> str:
    if not _valid_component(source_folder) or not run_id:
        raise ValueError("invalid housekeeping identity")
    suffix = ".reeloom-" + hashlib.sha256(run_id.encode()).hexdigest()[:12]
    limit = 255 - len(suffix.encode())
    prefix = source_folder
    while len(prefix.encode("utf-8", errors="surrogateescape")) > limit:
        prefix = prefix[:-1]
    if not prefix:
        raise ValueError("invalid housekeeping target")
    return prefix + suffix


def _valid_component(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and len(value.encode("utf-8", errors="surrogateescape")) <= 255
        and not any(
            unicodedata.category(character).startswith("C")
            for character in value
        )
    )


class _Unsafe(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FolderHousekeepingExecutor:
    sleeper: Callable[[float], None] = time.sleep
    observation_delays: tuple[float, ...] = (0.0, 0.05, 0.2, 0.5)

    def execute(
        self,
        *,
        root: Path,
        source_folder: str,
        target_folder: str,
        action: str,
    ) -> FolderHousekeepingResult:
        if (
            not isinstance(root, Path)
            or not root.is_absolute()
            or not _valid_component(source_folder)
            or not _valid_component(target_folder)
            or action not in {"archive", "fail"}
        ):
            return FolderHousekeepingResult(
                FolderHousekeepingOutcome.UNSAFE, "invalid_binding"
            )
        try:
            root_fd = self._open_root(root)
        except _Unsafe:
            return FolderHousekeepingResult(
                FolderHousekeepingOutcome.UNSAFE, "unsafe_root"
            )
        except OSError:
            return FolderHousekeepingResult(
                FolderHousekeepingOutcome.UNAVAILABLE, "root_unavailable"
            )
        bucket_fd: int | None = None
        try:
            try:
                os.mkdir(action, 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            self._require_exact(root_fd, action)
            bucket_fd = self._open_directory(root_fd, action)
            initial = self._observe(
                root_fd, bucket_fd, source_folder, target_folder
            )
            if initial == ("absent", "directory"):
                return FolderHousekeepingResult(
                    FolderHousekeepingOutcome.COMPLETED
                )
            if initial == ("absent", "absent"):
                return FolderHousekeepingResult(
                    FolderHousekeepingOutcome.COMPLETED, "source_absent"
                )
            if "unsafe" in initial:
                return FolderHousekeepingResult(
                    FolderHousekeepingOutcome.UNSAFE, "unsafe_entry"
                )
            if initial[1] != "absent":
                return FolderHousekeepingResult(
                    FolderHousekeepingOutcome.COLLISION,
                    "destination_collision",
                )
            with effect_mutex():
                current = self._observe(
                    root_fd, bucket_fd, source_folder, target_folder
                )
                if current != ("directory", "absent"):
                    return self._from_observation(current)
                try:
                    rename_noreplace(
                        root_fd, source_folder, bucket_fd, target_folder
                    )
                except OSError as error:
                    if (
                        classify_atomic_rename_error(error)
                        is AtomicRenameFailure.UNSUPPORTED
                    ):
                        current = self._observe(
                            root_fd,
                            bucket_fd,
                            source_folder,
                            target_folder,
                        )
                        if current == ("directory", "absent"):
                            try:
                                os.rename(
                                    source_folder,
                                    target_folder,
                                    src_dir_fd=root_fd,
                                    dst_dir_fd=bucket_fd,
                                )
                            except OSError:
                                pass
                    # A remote filesystem may report failure after applying.
            warning = self._best_effort_fsync(root_fd, bucket_fd)
            final = initial
            for delay in self.observation_delays:
                if delay:
                    self.sleeper(delay)
                final = self._observe(
                    root_fd, bucket_fd, source_folder, target_folder
                )
                if final == ("absent", "directory"):
                    return FolderHousekeepingResult(
                        FolderHousekeepingOutcome.COMPLETED, warning
                    )
                if "unsafe" in final or final[1] == "directory":
                    break
            result = self._from_observation(final)
            return FolderHousekeepingResult(
                result.outcome, result.warning or warning
            )
        except _Unsafe:
            return FolderHousekeepingResult(
                FolderHousekeepingOutcome.UNSAFE, "unsafe_entry"
            )
        except OSError:
            return FolderHousekeepingResult(
                FolderHousekeepingOutcome.UNAVAILABLE, "io_unavailable"
            )
        finally:
            if bucket_fd is not None:
                os.close(bucket_fd)
            os.close(root_fd)

    @staticmethod
    def _from_observation(
        observed: tuple[str, str],
    ) -> FolderHousekeepingResult:
        source, target = observed
        if source == "absent" and target == "directory":
            return FolderHousekeepingResult(
                FolderHousekeepingOutcome.COMPLETED
            )
        if "unsafe" in observed:
            return FolderHousekeepingResult(
                FolderHousekeepingOutcome.UNSAFE, "unsafe_entry"
            )
        if target != "absent":
            return FolderHousekeepingResult(
                FolderHousekeepingOutcome.COLLISION,
                "destination_collision",
            )
        return FolderHousekeepingResult(
            FolderHousekeepingOutcome.UNAVAILABLE, "io_unavailable"
        )

    @staticmethod
    def _open_root(root: Path) -> int:
        before = os.lstat(root)
        if not stat.S_ISDIR(before.st_mode):
            raise _Unsafe
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise _Unsafe
        descriptor = os.open(root, flags | no_follow)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            os.close(descriptor)
            raise _Unsafe
        return descriptor

    @classmethod
    def _open_directory(cls, parent_fd: int, name: str) -> int:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise _Unsafe
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise _Unsafe
        descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_DIRECTORY
            | no_follow
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            os.close(descriptor)
            raise _Unsafe
        return descriptor

    @staticmethod
    def _require_exact(parent_fd: int, name: str) -> None:
        matches = [
            item
            for item in os.listdir(parent_fd)
            if filesystem_name_key(item) == filesystem_name_key(name)
        ]
        if any(item != name for item in matches):
            raise _Unsafe

    @classmethod
    def _entry(cls, parent_fd: int, name: str) -> str:
        cls._require_exact(parent_fd, name)
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return "absent"
        return "directory" if stat.S_ISDIR(metadata.st_mode) else "unsafe"

    @classmethod
    def _observe(
        cls,
        root_fd: int,
        bucket_fd: int,
        source: str,
        target: str,
    ) -> tuple[str, str]:
        return cls._entry(root_fd, source), cls._entry(bucket_fd, target)

    @staticmethod
    def _best_effort_fsync(*descriptors: int) -> str | None:
        warning: str | None = None
        for descriptor in descriptors:
            try:
                os.fsync(descriptor)
            except OSError as error:
                warning = (
                    "directory_fsync_unsupported"
                    if error.errno
                    in {
                        errno.EINVAL,
                        errno.ENOSYS,
                        errno.EOPNOTSUPP,
                        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
                    }
                    else "directory_fsync_failed"
                )
        return warning
