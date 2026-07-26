from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from reeloom.adapters.filesystem import (
    FilesystemScanner,
    ScanLimits,
)
from reeloom.kernel.candidates import CandidateKind
from reeloom.policy.path_policy import AuthorizedRoot


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
