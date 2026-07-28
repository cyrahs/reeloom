from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath

from reeloom.adapters.filesystem import (
    FilesystemScanner,
    ScanLimits,
)
from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.file_types import candidate_kind_for_filename
from reeloom.kernel.naming import filesystem_name_key
from reeloom.kernel.scanner import ScannedFile, build_candidate_snapshot
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.policy.path_policy import is_forbidden_env_name

_RESERVED_FOLDERS = frozenset({"archive", "fail"})
_INVENTORY_SCHEMA = "folder-inventory-v1"
_MAX_FOLDERS = 1_000


class FolderEntryKind(StrEnum):
    DIRECTORY = "directory"
    FILE = "file"
    SYMLINK = "symlink"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class FolderEntry:
    relative_path: PurePosixPath
    kind: FolderEntryKind
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int

    @property
    def payload(self) -> dict[str, object]:
        return {
            "ctime_ns": self.ctime_ns,
            "device": self.device,
            "inode": self.inode,
            "kind": self.kind.value,
            "mtime_ns": self.mtime_ns,
            "relative_path": self.relative_path.as_posix(),
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class BlockedFolder:
    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class FolderSnapshot:
    name: str
    device: int
    inode: int
    inventory_id: str
    entries: tuple[FolderEntry, ...]
    candidates: WatchSnapshot

    @classmethod
    def create(
        cls,
        *,
        name: str,
        device: int,
        inode: int,
        entries: tuple[FolderEntry, ...],
        candidates: WatchSnapshot,
    ) -> FolderSnapshot:
        canonical = json.dumps(
            {
                "candidate_snapshot_id": candidates.snapshot_id,
                "device": device,
                "entries": [item.payload for item in entries],
                "inode": inode,
                "name": name,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return cls(
            name=name,
            device=device,
            inode=inode,
            inventory_id=(
                f"{_INVENTORY_SCHEMA}:{hashlib.sha256(canonical).hexdigest()}"
            ),
            entries=entries,
            candidates=candidates,
        )

    @property
    def disposition_inventory_id(self) -> str:
        """Identity after approved child moves may update directory times."""

        normalized = tuple(
            (
                FolderEntry(
                    relative_path=item.relative_path,
                    kind=item.kind,
                    size_bytes=item.size_bytes,
                    device=item.device,
                    inode=item.inode,
                    mtime_ns=0,
                    ctime_ns=0,
                )
                if item.kind is FolderEntryKind.DIRECTORY
                else item
            )
            for item in self.entries
        )
        return FolderSnapshot.create(
            name=self.name,
            device=self.device,
            inode=self.inode,
            entries=normalized,
            candidates=self.candidates,
        ).inventory_id


@dataclass(frozen=True, slots=True)
class FolderScan:
    folders: tuple[FolderSnapshot, ...]
    blocked: tuple[BlockedFolder, ...] = ()


class _Blocked(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class WatchFile:
    relative_path: PurePosixPath
    kind: CandidateKind
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int
    sample_digest: str | None

    @property
    def identity(self) -> tuple[object, ...]:
        return (
            self.kind,
            self.size_bytes,
            self.device,
            self.inode,
            self.mtime_ns,
            self.ctime_ns,
            self.sample_digest,
        )


@dataclass(frozen=True, slots=True)
class WatchSnapshot:
    snapshot_id: str
    files: tuple[WatchFile, ...]


@dataclass(frozen=True, slots=True)
class NoFollowWatcher:
    limits: ScanLimits = ScanLimits()

    def scan(self, root: AuthorizedRoot) -> WatchSnapshot:
        result = FilesystemScanner(limits=self.limits).scan(root)
        files: list[WatchFile] = []
        for record in result.snapshot.records:
            if any(
                value is None
                for value in (
                    record.device,
                    record.inode,
                    record.mtime_ns,
                    record.ctime_ns,
                )
            ):
                raise RuntimeError("filesystem scan omitted identity")
            files.append(
                WatchFile(
                    relative_path=record.relative_path,
                    kind=record.candidate.kind,
                    size_bytes=record.size_bytes,
                    device=record.device,
                    inode=record.inode,
                    mtime_ns=record.mtime_ns,
                    ctime_ns=record.ctime_ns,
                    sample_digest=record.sample_digest,
                )
            )
        return WatchSnapshot(
            snapshot_id=result.snapshot.snapshot_id,
            files=tuple(files),
        )

    def scan_folders(self, root: AuthorizedRoot) -> FolderScan:
        """Scan direct child folders without changing the media scanner."""

        root_fd = FilesystemScanner._open_root(root)
        names: list[str] = []
        blocked: list[BlockedFolder] = []
        try:
            try:
                with os.scandir(root_fd) as entries:
                    for index, entry in enumerate(entries):
                        if index >= self.limits.max_entries:
                            raise RuntimeError(
                                "watch root entry limit exceeded"
                            )
                        if (
                            entry.name.startswith(".")
                            or filesystem_name_key(entry.name)
                            in _RESERVED_FOLDERS
                            or entry.is_symlink()
                            or not entry.is_dir(follow_symlinks=False)
                        ):
                            continue
                        if len(names) >= _MAX_FOLDERS:
                            blocked.append(
                                BlockedFolder(
                                    entry.name,
                                    "folder_limit_exceeded",
                                )
                            )
                            break
                        names.append(entry.name)
            except OSError as error:
                raise RuntimeError("watch root scan failed") from error
        finally:
            os.close(root_fd)
        folders: list[FolderSnapshot] = []
        for name in sorted(
            names,
            key=lambda value: (filesystem_name_key(value), value),
        ):
            try:
                folders.append(
                    self.scan_folder(
                        root,
                        PurePosixPath(name),
                        logical_name=name,
                    )
                )
            except _Blocked as error:
                blocked.append(BlockedFolder(name, error.reason))
            except (OSError, RuntimeError):
                blocked.append(BlockedFolder(name, "scan_failed"))
        return FolderScan(tuple(folders), tuple(blocked))

    def scan_folder(
        self,
        root: AuthorizedRoot,
        relative_directory: PurePosixPath,
        *,
        logical_name: str,
    ) -> FolderSnapshot:
        first = self._scan_folder_once(
            root,
            relative_directory,
            logical_name=logical_name,
        )
        second = self._scan_folder_once(
            root,
            relative_directory,
            logical_name=logical_name,
        )
        if (
            first.device != second.device
            or first.inode != second.inode
            or first.inventory_id != second.inventory_id
        ):
            raise RuntimeError("folder changed during scan")
        return second

    def _scan_folder_once(
        self,
        root: AuthorizedRoot,
        relative_directory: PurePosixPath,
        *,
        logical_name: str,
    ) -> FolderSnapshot:
        if (
            not isinstance(relative_directory, PurePosixPath)
            or relative_directory.is_absolute()
            or not relative_directory.parts
            or ".." in relative_directory.parts
        ):
            raise RuntimeError("invalid folder path")
        root_fd = FilesystemScanner._open_root(root)
        current_fd = root_fd
        try:
            for part in relative_directory.parts:
                try:
                    next_fd = FilesystemScanner._open_directory(
                        part,
                        parent_fd=current_fd,
                    )
                except Exception:
                    raise RuntimeError("folder identity drift") from None
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = next_fd
            opened = os.fstat(current_fd)
            folder_entries: list[FolderEntry] = []
            candidates: list[WatchFile] = []
            self._scan_folder_directory(
                directory_fd=current_fd,
                folder_name=logical_name,
                relative_directory=None,
                depth=0,
                entries=folder_entries,
                candidates=candidates,
            )
            self._sample_subtitles(
                folder_fd=current_fd,
                candidates=candidates,
            )
            return FolderSnapshot.create(
                name=logical_name,
                device=opened.st_dev,
                inode=opened.st_ino,
                entries=tuple(folder_entries),
                candidates=self._candidate_snapshot(candidates),
            )
        finally:
            if current_fd != root_fd:
                os.close(current_fd)
            os.close(root_fd)

    def _scan_folder_directory(
        self,
        *,
        directory_fd: int,
        folder_name: str,
        relative_directory: PurePosixPath | None,
        depth: int,
        entries: list[FolderEntry],
        candidates: list[WatchFile],
    ) -> None:
        if depth > self.limits.max_depth:
            raise _Blocked("scan_limit_exceeded")
        remaining = self.limits.max_entries - len(entries)
        names: list[str] = []
        try:
            with os.scandir(directory_fd) as children:
                for index, child in enumerate(children):
                    if index >= remaining:
                        raise _Blocked("scan_limit_exceeded")
                    names.append(child.name)
        except _Blocked:
            raise
        except OSError:
            raise _Blocked("scan_failed") from None
        for name in sorted(
            names,
            key=lambda value: (filesystem_name_key(value), value),
        ):
            if is_forbidden_env_name(name):
                raise _Blocked("env_path_forbidden")
            relative = (
                PurePosixPath(name)
                if relative_directory is None
                else relative_directory / name
            )
            if (
                len(
                    relative.as_posix().encode(
                        "utf-8",
                        errors="surrogateescape",
                    )
                )
                > 4_096
            ):
                raise _Blocked("scan_limit_exceeded")
            try:
                metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError:
                raise _Blocked("scan_failed") from None
            kind = self._entry_kind(metadata)
            entries.append(
                FolderEntry(
                    relative_path=relative,
                    kind=kind,
                    size_bytes=metadata.st_size,
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                    mtime_ns=metadata.st_mtime_ns,
                    ctime_ns=metadata.st_ctime_ns,
                )
            )
            if kind is FolderEntryKind.SYMLINK:
                continue
            if kind is FolderEntryKind.DIRECTORY:
                try:
                    child_fd = FilesystemScanner._open_directory(
                        name,
                        parent_fd=directory_fd,
                    )
                except Exception:
                    raise _Blocked("scan_failed") from None
                try:
                    opened = os.fstat(child_fd)
                    if (
                        opened.st_dev != metadata.st_dev
                        or opened.st_ino != metadata.st_ino
                    ):
                        raise _Blocked("scan_failed")
                    self._scan_folder_directory(
                        directory_fd=child_fd,
                        folder_name=folder_name,
                        relative_directory=relative,
                        depth=depth + 1,
                        entries=entries,
                        candidates=candidates,
                    )
                finally:
                    os.close(child_fd)
                continue
            if kind is not FolderEntryKind.FILE:
                continue
            candidate_kind = candidate_kind_for_filename(name)
            if candidate_kind is None:
                continue
            if len(candidates) >= self.limits.max_candidates:
                raise _Blocked("scan_limit_exceeded")
            candidates.append(
                WatchFile(
                    relative_path=PurePosixPath(folder_name) / relative,
                    kind=candidate_kind,
                    size_bytes=metadata.st_size,
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                    mtime_ns=metadata.st_mtime_ns,
                    ctime_ns=metadata.st_ctime_ns,
                    sample_digest=None,
                )
            )

    @staticmethod
    def _sample_subtitles(
        *,
        folder_fd: int,
        candidates: list[WatchFile],
    ) -> None:
        for index, candidate in enumerate(candidates):
            if candidate.kind is not CandidateKind.SUBTITLE:
                continue
            parts = candidate.relative_path.parts[1:]
            current_fd = folder_fd
            try:
                for part in parts[:-1]:
                    next_fd = FilesystemScanner._open_directory(
                        part,
                        parent_fd=current_fd,
                    )
                    if current_fd != folder_fd:
                        os.close(current_fd)
                    current_fd = next_fd
                metadata = os.stat(
                    parts[-1],
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size != candidate.size_bytes
                    or metadata.st_dev != candidate.device
                    or metadata.st_ino != candidate.inode
                    or metadata.st_mtime_ns != candidate.mtime_ns
                    or metadata.st_ctime_ns != candidate.ctime_ns
                ):
                    raise _Blocked("scan_failed")
                candidates[index] = replace(
                    candidate,
                    sample_digest=FilesystemScanner._subtitle_sample_digest(
                        directory_fd=current_fd,
                        name=parts[-1],
                        expected=metadata,
                    ),
                )
            except _Blocked:
                raise
            except Exception:
                raise _Blocked("scan_failed") from None
            finally:
                if current_fd != folder_fd:
                    os.close(current_fd)

    @staticmethod
    def _entry_kind(metadata: os.stat_result) -> FolderEntryKind:
        if stat.S_ISDIR(metadata.st_mode):
            return FolderEntryKind.DIRECTORY
        if stat.S_ISREG(metadata.st_mode):
            return FolderEntryKind.FILE
        if stat.S_ISLNK(metadata.st_mode):
            return FolderEntryKind.SYMLINK
        return FolderEntryKind.OTHER

    @staticmethod
    def _candidate_snapshot(files: list[WatchFile]) -> WatchSnapshot:
        snapshot = build_candidate_snapshot(
            ScannedFile(
                relative_path=item.relative_path,
                kind=item.kind,
                size_bytes=item.size_bytes,
                device=item.device,
                inode=item.inode,
                mtime_ns=item.mtime_ns,
                ctime_ns=item.ctime_ns,
                sample_digest=item.sample_digest,
            )
            for item in files
        )
        return WatchSnapshot(snapshot.snapshot_id, tuple(files))
