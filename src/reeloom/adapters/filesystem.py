from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.file_types import candidate_kind_for_filename
from reeloom.kernel.scanner import (
    CandidateRecord,
    ScannedCandidateSnapshot,
    ScannedFile,
    build_candidate_snapshot,
)
from reeloom.policy.path_policy import (
    AuthorizedRoot,
    is_forbidden_env_name,
)
from reeloom.ports.subtitles import SubtitleSample


def _read_prefix(file_descriptor: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max_bytes
    while remaining:
        chunk = os.read(file_descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


@dataclass(frozen=True, slots=True)
class ScanLimits:
    max_candidates: int = 10_000
    max_entries: int = 50_000
    max_depth: int = 32

    def __post_init__(self) -> None:
        if (
            type(self.max_candidates) is not int
            or self.max_candidates < 1
            or type(self.max_entries) is not int
            or self.max_entries < 1
            or type(self.max_depth) is not int
            or self.max_depth < 1
        ):
            raise DomainError(ErrorCode.SCAN_LIMIT_EXCEEDED)


@dataclass(slots=True)
class _ScanProgress:
    entries_seen: int = 0


@dataclass(frozen=True, slots=True)
class FilesystemScanResult:
    authorized_root: AuthorizedRoot
    snapshot: ScannedCandidateSnapshot


@dataclass(frozen=True, slots=True)
class FilesystemSubtitleSampleProvider:
    """Read only a bounded prefix for a subtitle already in one scan."""

    scan: FilesystemScanResult

    @property
    def snapshot_id(self) -> str:
        return self.scan.snapshot.snapshot_id

    @property
    def candidate_count(self) -> int:
        return len(self.scan.snapshot.records)

    async def sample(
        self,
        subtitle_id: CandidateId,
        *,
        max_bytes: int,
    ) -> SubtitleSample:
        if (
            not isinstance(subtitle_id, CandidateId)
            or subtitle_id.kind is not CandidateKind.SUBTITLE
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= 64 * 1024
        ):
            raise DomainError(ErrorCode.INVALID_SUBTITLE_VARIANT)
        record = self.scan.snapshot.record_for(subtitle_id)
        if any(
            value is None
            for value in (
                record.device,
                record.inode,
                record.mtime_ns,
                record.ctime_ns,
                record.sample_digest,
            )
        ):
            raise DomainError(ErrorCode.SCAN_FAILED)
        root_fd = FilesystemScanner._open_root(self.scan.authorized_root)
        current_fd = root_fd
        try:
            for part in record.relative_path.parts[:-1]:
                next_fd = FilesystemScanner._open_directory(
                    part,
                    parent_fd=current_fd,
                )
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = next_fd

            no_follow = getattr(os, "O_NOFOLLOW", None)
            if no_follow is None:
                raise DomainError(ErrorCode.SCAN_FAILED)
            flags = (
                os.O_RDONLY
                | no_follow
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            file_fd: int | None = None
            try:
                file_fd = os.open(
                    record.relative_path.name,
                    flags,
                    dir_fd=current_fd,
                )
                metadata = os.fstat(file_fd)
                if not self._matches_record(metadata, record):
                    raise DomainError(ErrorCode.SCAN_FAILED)
                content = _read_prefix(file_fd, 64 * 1024)
                if not self._matches_record(os.fstat(file_fd), record):
                    raise DomainError(ErrorCode.SCAN_FAILED)
                if (
                    hashlib.sha256(content).hexdigest()
                    != record.sample_digest
                ):
                    raise DomainError(ErrorCode.SCAN_FAILED)
            except OSError:
                raise DomainError(ErrorCode.SCAN_FAILED) from None
            finally:
                if file_fd is not None:
                    os.close(file_fd)
        finally:
            if current_fd != root_fd:
                os.close(current_fd)
            os.close(root_fd)

        return SubtitleSample(
            display_name=record.candidate.display_name,
            content=content[:max_bytes],
        )

    @staticmethod
    def _matches_record(
        metadata: os.stat_result,
        record: CandidateRecord,
    ) -> bool:
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_size == record.size_bytes
            and metadata.st_dev == record.device
            and metadata.st_ino == record.inode
            and metadata.st_mtime_ns == record.mtime_ns
            and metadata.st_ctime_ns == record.ctime_ns
        )


@dataclass(frozen=True, slots=True)
class FilesystemScanner:
    limits: ScanLimits = ScanLimits()

    def scan(self, root: AuthorizedRoot) -> FilesystemScanResult:
        files: list[ScannedFile] = []
        directory_fd = self._open_root(root)
        try:
            self._scan_directory(
                directory_fd=directory_fd,
                relative_directory=None,
                depth=0,
                files=files,
                progress=_ScanProgress(),
            )
        finally:
            os.close(directory_fd)
        return FilesystemScanResult(
            authorized_root=root,
            snapshot=build_candidate_snapshot(files),
        )

    @classmethod
    def _open_root(cls, root: AuthorizedRoot) -> int:
        current_fd = cls._open_directory(Path(root.path.anchor))
        try:
            for part in root.path.parts[1:]:
                next_fd = cls._open_directory(
                    part,
                    parent_fd=current_fd,
                )
                os.close(current_fd)
                current_fd = next_fd
            root_stat = os.fstat(current_fd)
            if (
                root_stat.st_dev != root.device
                or root_stat.st_ino != root.inode
            ):
                raise DomainError(ErrorCode.SCAN_FAILED)
            return current_fd
        except Exception:
            os.close(current_fd)
            raise

    @staticmethod
    def _open_directory(
        path: Path | str,
        *,
        parent_fd: int | None = None,
    ) -> int:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        directory = getattr(os, "O_DIRECTORY", None)
        if no_follow is None or directory is None:
            raise DomainError(ErrorCode.SCAN_FAILED)
        flags = os.O_RDONLY | no_follow | directory
        try:
            return os.open(path, flags, dir_fd=parent_fd)
        except OSError as error:
            raise DomainError(ErrorCode.SCAN_FAILED) from error

    def _scan_directory(
        self,
        *,
        directory_fd: int,
        relative_directory: PurePosixPath | None,
        depth: int,
        files: list[ScannedFile],
        progress: _ScanProgress,
    ) -> None:
        if depth > self.limits.max_depth:
            raise DomainError(ErrorCode.SCAN_LIMIT_EXCEEDED)

        try:
            with os.scandir(directory_fd) as entries:
                ordered_entries = []
                for entry in entries:
                    progress.entries_seen += 1
                    if progress.entries_seen > self.limits.max_entries:
                        raise DomainError(ErrorCode.SCAN_LIMIT_EXCEEDED)
                    ordered_entries.append(entry)
                ordered_entries.sort(key=lambda entry: entry.name)
        except OSError as error:
            raise DomainError(ErrorCode.SCAN_FAILED) from error

        for entry in ordered_entries:
            if is_forbidden_env_name(entry.name):
                continue
            try:
                if entry.is_symlink():
                    continue
                relative_path = (
                    PurePosixPath(entry.name)
                    if relative_directory is None
                    else relative_directory / entry.name
                )
                if entry.is_dir(follow_symlinks=False):
                    child_fd = self._open_directory(
                        entry.name,
                        parent_fd=directory_fd,
                    )
                    try:
                        self._scan_directory(
                            directory_fd=child_fd,
                            relative_directory=relative_path,
                            depth=depth + 1,
                            files=files,
                            progress=progress,
                        )
                    finally:
                        os.close(child_fd)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                kind = candidate_kind_for_filename(entry.name)
                if kind is None:
                    continue
                file_stat = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
            except OSError as error:
                raise DomainError(ErrorCode.SCAN_FAILED) from error

            files.append(
                ScannedFile(
                    relative_path=relative_path,
                    kind=kind,
                    size_bytes=file_stat.st_size,
                    device=file_stat.st_dev,
                    inode=file_stat.st_ino,
                    mtime_ns=file_stat.st_mtime_ns,
                    ctime_ns=file_stat.st_ctime_ns,
                    sample_digest=(
                        self._subtitle_sample_digest(
                            directory_fd=directory_fd,
                            name=entry.name,
                            expected=file_stat,
                        )
                        if kind is CandidateKind.SUBTITLE
                        else None
                    ),
                )
            )
            if len(files) > self.limits.max_candidates:
                raise DomainError(ErrorCode.SCAN_LIMIT_EXCEEDED)

    @staticmethod
    def _subtitle_sample_digest(
        *,
        directory_fd: int,
        name: str,
        expected: os.stat_result,
    ) -> str:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise DomainError(ErrorCode.SCAN_FAILED)
        flags = (
            os.O_RDONLY
            | no_follow
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        file_fd: int | None = None
        try:
            file_fd = os.open(name, flags, dir_fd=directory_fd)
            before = os.fstat(file_fd)
            if not FilesystemScanner._same_identity(before, expected):
                raise DomainError(ErrorCode.SCAN_FAILED)
            content = _read_prefix(file_fd, 64 * 1024)
            if not FilesystemScanner._same_identity(
                os.fstat(file_fd),
                expected,
            ):
                raise DomainError(ErrorCode.SCAN_FAILED)
            return hashlib.sha256(content).hexdigest()
        except OSError:
            raise DomainError(ErrorCode.SCAN_FAILED) from None
        finally:
            if file_fd is not None:
                os.close(file_fd)

    @staticmethod
    def _same_identity(
        first: os.stat_result,
        second: os.stat_result,
    ) -> bool:
        return (
            stat.S_ISREG(first.st_mode)
            and stat.S_ISREG(second.st_mode)
            and first.st_dev == second.st_dev
            and first.st_ino == second.st_ino
            and first.st_size == second.st_size
            and first.st_mtime_ns == second.st_mtime_ns
            and first.st_ctime_ns == second.st_ctime_ns
        )
