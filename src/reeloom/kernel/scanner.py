from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from reeloom.kernel.candidates import (
    Candidate,
    CandidateId,
    CandidateKind,
    CandidateSnapshot,
)
from reeloom.kernel.errors import DomainError, ErrorCode

_SNAPSHOT_SCHEMA = "candidate-snapshot-v1"
_MAX_RELATIVE_PATH_BYTES = 4096


@dataclass(frozen=True, slots=True)
class ScannedFile:
    relative_path: PurePosixPath
    kind: CandidateKind
    size_bytes: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.relative_path, PurePosixPath)
            or self.relative_path.is_absolute()
            or ".." in self.relative_path.parts
            or not self.relative_path.parts
            or any("\\" in part for part in self.relative_path.parts)
        ):
            raise DomainError(ErrorCode.PATH_ESCAPE)
        if (
            len(
                self.relative_path.as_posix().encode(
                    "utf-8",
                    errors="surrogateescape",
                )
            )
            > _MAX_RELATIVE_PATH_BYTES
        ):
            raise DomainError(ErrorCode.SCAN_LIMIT_EXCEEDED)
        if any(
            part.casefold().startswith(".env")
            for part in self.relative_path.parts
        ):
            raise DomainError(ErrorCode.ENV_PATH_FORBIDDEN)
        if not isinstance(self.kind, CandidateKind):
            raise DomainError(ErrorCode.INVALID_CANDIDATE_KIND)
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "size_bytes", "expected": "non_negative_int"},
            )


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    candidate: Candidate
    relative_path: PurePosixPath
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ScannedCandidateSnapshot:
    """Public candidates plus an internal exact relative-path capability table."""

    snapshot_id: str
    candidates: CandidateSnapshot
    records: tuple[CandidateRecord, ...]

    def __post_init__(self) -> None:
        if not self.snapshot_id.startswith(f"{_SNAPSHOT_SCHEMA}:"):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        if tuple(record.candidate for record in self.records) != (
            self.candidates.candidates
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)

    def record_for(self, candidate_id: CandidateId) -> CandidateRecord:
        for record in self.records:
            if record.candidate.id == candidate_id:
                return record
        raise DomainError(
            ErrorCode.UNKNOWN_CANDIDATE_ID,
            context={"candidate_id": str(candidate_id)},
        )


def _sort_key(scanned_file: ScannedFile) -> tuple[int, str, str]:
    raw_path = scanned_file.relative_path.as_posix()
    kind_order = 0 if scanned_file.kind is CandidateKind.VIDEO else 1
    return (kind_order, raw_path.casefold(), raw_path)


def _snapshot_id(records: tuple[CandidateRecord, ...]) -> str:
    payload = [
        {
            "id": str(record.candidate.id),
            "kind": record.candidate.kind.value,
            "relative_path": record.relative_path.as_posix(),
            "size_bytes": record.size_bytes,
        }
        for record in records
    ]
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"{_SNAPSHOT_SCHEMA}:{hashlib.sha256(canonical).hexdigest()}"


def build_candidate_snapshot(
    scanned_files: Iterable[ScannedFile],
) -> ScannedCandidateSnapshot:
    """Assign deterministic, per-kind ordinals after a stable path sort."""

    ordered = tuple(sorted(tuple(scanned_files), key=_sort_key))
    relative_paths = tuple(item.relative_path for item in ordered)
    if len(set(relative_paths)) != len(relative_paths):
        raise DomainError(ErrorCode.DUPLICATE_SCANNED_PATH)

    ordinals = {
        CandidateKind.VIDEO: 0,
        CandidateKind.SUBTITLE: 0,
    }
    records: list[CandidateRecord] = []
    for scanned_file in ordered:
        ordinals[scanned_file.kind] += 1
        candidate = Candidate(
            id=CandidateId(
                kind=scanned_file.kind,
                ordinal=ordinals[scanned_file.kind],
            ),
            kind=scanned_file.kind,
            display_name=scanned_file.relative_path.as_posix(),
        )
        records.append(
            CandidateRecord(
                candidate=candidate,
                relative_path=scanned_file.relative_path,
                size_bytes=scanned_file.size_bytes,
            )
        )

    frozen_records = tuple(records)
    return ScannedCandidateSnapshot(
        snapshot_id=_snapshot_id(frozen_records),
        candidates=CandidateSnapshot.create(
            record.candidate for record in frozen_records
        ),
        records=frozen_records,
    )
