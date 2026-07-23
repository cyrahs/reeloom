from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.file_types import candidate_kind_for_filename
from reeloom.kernel.scanner import (
    ScannedCandidateSnapshot,
    ScannedFile,
    build_candidate_snapshot,
)
from reeloom.policy.path_policy import (
    AuthorizedRoot,
    is_forbidden_env_name,
)


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

    def source_path(self, candidate_id: CandidateId) -> Path:
        record = self.snapshot.record_for(candidate_id)
        return self.authorized_root.resolve_existing(
            record.relative_path.as_posix()
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
                )
            )
            if len(files) > self.limits.max_candidates:
                raise DomainError(ErrorCode.SCAN_LIMIT_EXCEEDED)
