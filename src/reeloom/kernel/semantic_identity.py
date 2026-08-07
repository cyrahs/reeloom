from __future__ import annotations

import hashlib
import json
import re
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

CURRENT_SEMANTIC_SNAPSHOT_SCHEMA_VERSION = "2"

_SNAPSHOT_PREFIX = "candidate-snapshot-v2:"
_SNAPSHOT_PATTERN = re.compile(
    rf"^{re.escape(_SNAPSHOT_PREFIX)}[0-9a-f]{{64}}$"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_PATH_BYTES = 4_096


def _candidate_sort_key(candidate_id: CandidateId) -> tuple[int, int]:
    return (
        0 if candidate_id.kind is CandidateKind.VIDEO else 1,
        candidate_id.ordinal,
    )


def validate_semantic_relative_path(value: object) -> PurePosixPath:
    if (
        not isinstance(value, PurePosixPath)
        or value.is_absolute()
        or not value.parts
        or ".." in value.parts
        or any("\\" in part for part in value.parts)
    ):
        raise DomainError(ErrorCode.PATH_ESCAPE)
    if any(part.casefold().startswith(".env") for part in value.parts):
        raise DomainError(ErrorCode.ENV_PATH_FORBIDDEN)
    if (
        len(value.as_posix().encode("utf-8", errors="surrogateescape"))
        > _MAX_PATH_BYTES
    ):
        raise DomainError(ErrorCode.SCAN_LIMIT_EXCEEDED)
    return value


@dataclass(frozen=True, slots=True)
class SemanticRootBinding:
    """A v2 authorized root bound only to its configured no-follow path."""

    path: PurePosixPath

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, PurePosixPath)
            or not self.path.is_absolute()
            or ".." in self.path.parts
            or any("\\" in part for part in self.path.parts)
        ):
            raise DomainError(ErrorCode.PATH_NOT_ABSOLUTE)
        if any(
            part.casefold().startswith(".env") for part in self.path.parts
        ):
            raise DomainError(ErrorCode.ENV_PATH_FORBIDDEN)
        if (
            len(
                self.path.as_posix().encode(
                    "utf-8", errors="surrogateescape"
                )
            )
            > _MAX_PATH_BYTES
        ):
            raise DomainError(ErrorCode.SCAN_LIMIT_EXCEEDED)

    @classmethod
    def from_payload(cls, value: object) -> SemanticRootBinding:
        if not isinstance(value, dict) or set(value) != {"path"}:
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        path = value["path"]
        if not isinstance(path, str):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        return cls(PurePosixPath(path))

    def payload(self) -> dict[str, object]:
        return {"path": self.path.as_posix()}


@dataclass(frozen=True, slots=True)
class SemanticSourceIdentity:
    """Persisted v2 identity: path and size for video, full hash for subtitle."""

    candidate_id: CandidateId
    kind: CandidateKind
    relative_path: PurePosixPath
    size_bytes: int
    sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.candidate_id, CandidateId)
            or not isinstance(self.kind, CandidateKind)
            or self.candidate_id.kind is not self.kind
            or type(self.size_bytes) is not int
            or self.size_bytes < 0
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        validate_semantic_relative_path(self.relative_path)
        if self.kind is CandidateKind.VIDEO:
            if self.sha256 is not None:
                raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
            return
        if (
            not isinstance(self.sha256, str)
            or _SHA256_PATTERN.fullmatch(self.sha256) is None
        ):
            raise DomainError(ErrorCode.INCOMPLETE_SOURCE_IDENTITY)

    @classmethod
    def from_payload(cls, value: object) -> SemanticSourceIdentity:
        if not isinstance(value, dict):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        common_fields = {
            "candidate_id",
            "file_type",
            "kind",
            "relative_path",
            "size_bytes",
        }
        if value.get("file_type") != "regular":
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        try:
            candidate_id = CandidateId.parse(value["candidate_id"])
            kind = CandidateKind(value["kind"])
        except (DomainError, KeyError, TypeError, ValueError):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE) from None
        expected_fields = common_fields | (
            {"sha256"} if kind is CandidateKind.SUBTITLE else set()
        )
        if set(value) != expected_fields:
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        relative_path = value["relative_path"]
        if not isinstance(relative_path, str):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        return cls(
            candidate_id=candidate_id,
            kind=kind,
            relative_path=PurePosixPath(relative_path),
            size_bytes=value["size_bytes"],  # type: ignore[arg-type]
            sha256=value.get("sha256"),  # type: ignore[arg-type]
        )

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "candidate_id": str(self.candidate_id),
            "file_type": "regular",
            "kind": self.kind.value,
            "relative_path": self.relative_path.as_posix(),
            "size_bytes": self.size_bytes,
        }
        if self.kind is CandidateKind.SUBTITLE:
            payload["sha256"] = self.sha256
        return payload


@dataclass(frozen=True, slots=True, init=False)
class SemanticCandidateSnapshot:
    schema_version: str
    snapshot_id: str
    sources: tuple[SemanticSourceIdentity, ...]

    @classmethod
    def create(
        cls,
        sources: Iterable[SemanticSourceIdentity],
    ) -> SemanticCandidateSnapshot:
        source_items = tuple(sources)
        if any(
            not isinstance(item, SemanticSourceIdentity)
            for item in source_items
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        ordered = tuple(
            sorted(
                source_items,
                key=lambda item: _candidate_sort_key(item.candidate_id),
            )
        )
        candidate_ids = tuple(item.candidate_id for item in ordered)
        paths = tuple(item.relative_path for item in ordered)
        if len(set(candidate_ids)) != len(candidate_ids):
            raise DomainError(ErrorCode.DUPLICATE_CANDIDATE_ID)
        if len(set(paths)) != len(paths):
            raise DomainError(ErrorCode.DUPLICATE_SCANNED_PATH)
        canonical = json.dumps(
            [item.payload() for item in ordered],
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        snapshot = object.__new__(cls)
        object.__setattr__(
            snapshot,
            "schema_version",
            CURRENT_SEMANTIC_SNAPSHOT_SCHEMA_VERSION,
        )
        object.__setattr__(
            snapshot,
            "snapshot_id",
            _SNAPSHOT_PREFIX + hashlib.sha256(canonical).hexdigest(),
        )
        object.__setattr__(snapshot, "sources", ordered)
        return snapshot

    @classmethod
    def from_payload(
        cls,
        value: object,
        *,
        snapshot_id: object,
    ) -> SemanticCandidateSnapshot:
        if not isinstance(value, list):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        restored = cls.create(
            SemanticSourceIdentity.from_payload(item) for item in value
        )
        if (
            not isinstance(snapshot_id, str)
            or _SNAPSHOT_PATTERN.fullmatch(snapshot_id) is None
            or restored.snapshot_id != snapshot_id
        ):
            raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)
        return restored

    def payload(self) -> list[dict[str, object]]:
        return [item.payload() for item in self.sources]

    @property
    def candidates(self) -> CandidateSnapshot:
        return CandidateSnapshot.create(
            Candidate(
                id=source.candidate_id,
                kind=source.kind,
                display_name=source.relative_path.as_posix(),
            )
            for source in self.sources
        )

    def source_for(self, candidate_id: CandidateId) -> SemanticSourceIdentity:
        for source in self.sources:
            if source.candidate_id == candidate_id:
                return source
        raise DomainError(
            ErrorCode.UNKNOWN_CANDIDATE_ID,
            context={"candidate_id": str(candidate_id)},
        )


def is_valid_semantic_snapshot_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and _SNAPSHOT_PATTERN.fullmatch(value) is not None
    )
