from __future__ import annotations

import hashlib
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

from reeloom.kernel.subtitle_acquisition import (
    MAX_ARCHIVE_VOLUMES,
    SubtitleArchiveSetCapability,
    SubtitleArchiveVolume,
)
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.ports.subtitle_acquisition import (
    DownloadedArchiveVolume,
    DownloadedSubtitleArchiveSet,
    SubtitleArchiveError,
    SubtitleArchiveErrorCode,
)

_CACHE_PREFIX = "archive-volume-sha256-"
_CACHE_NAME_LENGTH = len(_CACHE_PREFIX) + 64


def _cache_error() -> SubtitleArchiveError:
    return SubtitleArchiveError(
        SubtitleArchiveErrorCode.CONTENT_DRIFT,
        retryable=False,
    )


@dataclass(frozen=True, slots=True)
class FilesystemSubtitleArchiveCache:
    """Never-overwrite cache keyed only by the verified volume SHA-256."""

    root: AuthorizedRoot
    _gate = threading.Lock()

    def __post_init__(self) -> None:
        if not isinstance(self.root, AuthorizedRoot):
            raise TypeError("root must be an AuthorizedRoot")

    @property
    def cache_root(self) -> Path:
        return self.root.path

    def store(
        self,
        downloaded: DownloadedSubtitleArchiveSet,
    ) -> DownloadedSubtitleArchiveSet:
        if not isinstance(downloaded, DownloadedSubtitleArchiveSet):
            raise _cache_error()
        with self._gate:
            for item in downloaded.volumes:
                content = self._read_verified_source(item)
                self._store_content(item.volume.sha256, content)
        restored = self.load(
            downloaded.capability,
            tuple(item.volume for item in downloaded.volumes),
        )
        if restored is None:
            raise _cache_error()
        return restored

    def load(
        self,
        capability: SubtitleArchiveSetCapability,
        volumes: tuple[SubtitleArchiveVolume, ...],
    ) -> DownloadedSubtitleArchiveSet | None:
        if (
            not isinstance(capability, SubtitleArchiveSetCapability)
            or not isinstance(volumes, tuple)
            or not 1 <= len(volumes) <= MAX_ARCHIVE_VOLUMES
            or any(not isinstance(item, SubtitleArchiveVolume) for item in volumes)
            or tuple(item.index for item in volumes)
            != tuple(range(1, len(volumes) + 1))
            or tuple(item.attachment_id for item in volumes)
            != capability.attachment_ids
        ):
            raise _cache_error()
        root_fd = self._open_root()
        try:
            restored: list[DownloadedArchiveVolume] = []
            for volume in volumes:
                name = self._name(volume.sha256)
                state = self._read_cache_entry(
                    root_fd,
                    name,
                    expected_size=volume.size_bytes,
                    expected_sha256=volume.sha256,
                )
                if state is None:
                    return None
                metadata = os.stat(
                    name,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
                restored.append(
                    DownloadedArchiveVolume(
                        volume=volume,
                        path=self.root.path / name,
                        device=metadata.st_dev,
                        inode=metadata.st_ino,
                        mtime_ns=metadata.st_mtime_ns,
                        ctime_ns=metadata.st_ctime_ns,
                    )
                )
            return DownloadedSubtitleArchiveSet(capability, tuple(restored))
        finally:
            os.close(root_fd)

    def _store_content(self, sha256: str, content: bytes) -> None:
        root_fd = self._open_root()
        attempt_fd: int | None = None
        try:
            name = self._name(sha256)
            existing = self._read_cache_entry(
                root_fd,
                name,
                expected_size=len(content),
                expected_sha256=sha256,
            )
            if existing is not None:
                return
            attempt_name = f".cache-attempt-{sha256}-{os.urandom(12).hex()}"
            attempt_fd = os.open(
                attempt_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=root_fd,
            )
            remaining = memoryview(content)
            while remaining:
                written = os.write(attempt_fd, remaining)
                if written <= 0:
                    raise OSError("short cache write")
                remaining = remaining[written:]
            try:
                os.fsync(attempt_fd)
            except OSError:
                pass
            os.close(attempt_fd)
            attempt_fd = None
            try:
                os.link(
                    attempt_name,
                    name,
                    src_dir_fd=root_fd,
                    dst_dir_fd=root_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
            if self._read_cache_entry(
                root_fd,
                name,
                expected_size=len(content),
                expected_sha256=sha256,
            ) is None:
                raise _cache_error()
            try:
                os.fsync(root_fd)
            except OSError:
                pass
        except SubtitleArchiveError:
            raise
        except OSError:
            raise SubtitleArchiveError(
                SubtitleArchiveErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None
        finally:
            if attempt_fd is not None:
                os.close(attempt_fd)
            os.close(root_fd)

    def _read_verified_source(self, item: DownloadedArchiveVolume) -> bytes:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                item.path,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            return self._read_descriptor(
                descriptor,
                expected_size=item.volume.size_bytes,
                expected_sha256=item.volume.sha256,
            )
        except SubtitleArchiveError:
            raise
        except OSError:
            raise _cache_error() from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _open_root(self) -> int:
        try:
            return os.open(
                self.root.path,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError:
            raise SubtitleArchiveError(
                SubtitleArchiveErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None

    @classmethod
    def _read_cache_entry(
        cls,
        root_fd: int,
        name: str,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> bytes | None:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=root_fd,
            )
            return cls._read_descriptor(
                descriptor,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
        except FileNotFoundError:
            return None
        except SubtitleArchiveError:
            raise
        except OSError:
            raise _cache_error() from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _read_descriptor(
        descriptor: int,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> bytes:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise _cache_error()
        content = bytearray()
        digest = hashlib.sha256()
        while len(content) < expected_size:
            chunk = os.read(
                descriptor,
                min(64 * 1024, expected_size - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            len(content) != expected_size
            or os.read(descriptor, 1)
            or digest.hexdigest() != expected_sha256
            or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
        ):
            raise _cache_error()
        return bytes(content)

    @staticmethod
    def _name(sha256: str) -> str:
        name = _CACHE_PREFIX + sha256
        if len(name) != _CACHE_NAME_LENGTH:
            raise _cache_error()
        return name
