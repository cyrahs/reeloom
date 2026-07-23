from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.schema import check_fields

_CANDIDATE_ID_PATTERN = re.compile(r"^(video|subtitle):([1-9][0-9]*)$")
_CANDIDATE_FIELDS = frozenset({"id", "kind", "display_name"})
_MAX_CANDIDATE_ORDINAL = (1 << 63) - 1
_MAX_CANDIDATE_ORDINAL_DIGITS = len(str(_MAX_CANDIDATE_ORDINAL))
_MAX_CANDIDATE_ID_LENGTH = len("subtitle:") + _MAX_CANDIDATE_ORDINAL_DIGITS


class CandidateKind(StrEnum):
    VIDEO = "video"
    SUBTITLE = "subtitle"


@dataclass(frozen=True, slots=True)
class CandidateId:
    """A run-scoped identifier that contains no filesystem location."""

    kind: CandidateKind
    ordinal: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CandidateKind):
            raise DomainError(ErrorCode.INVALID_CANDIDATE_KIND)
        if (
            type(self.ordinal) is not int
            or self.ordinal < 1
            or self.ordinal > _MAX_CANDIDATE_ORDINAL
        ):
            raise DomainError(ErrorCode.INVALID_CANDIDATE_ID)

    @classmethod
    def parse(cls, value: object) -> CandidateId:
        if not isinstance(value, str):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "id", "expected": "str"},
            )
        if len(value) > _MAX_CANDIDATE_ID_LENGTH:
            raise DomainError(ErrorCode.INVALID_CANDIDATE_ID)

        match = _CANDIDATE_ID_PATTERN.fullmatch(value)
        if match is None:
            raise DomainError(ErrorCode.INVALID_CANDIDATE_ID)

        raw_ordinal = match.group(2)
        if len(raw_ordinal) > _MAX_CANDIDATE_ORDINAL_DIGITS:
            raise DomainError(ErrorCode.INVALID_CANDIDATE_ID)
        ordinal = int(raw_ordinal)
        if ordinal > _MAX_CANDIDATE_ORDINAL:
            raise DomainError(ErrorCode.INVALID_CANDIDATE_ID)

        return cls(
            kind=CandidateKind(match.group(1)),
            ordinal=ordinal,
        )

    def __str__(self) -> str:
        return f"{self.kind.value}:{self.ordinal}"


@dataclass(frozen=True, slots=True)
class Candidate:
    """Minimal candidate metadata safe to expose outside the filesystem adapter."""

    id: CandidateId
    kind: CandidateKind
    display_name: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, CandidateId):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "id", "expected": "CandidateId"},
            )
        if not isinstance(self.kind, CandidateKind):
            raise DomainError(ErrorCode.INVALID_CANDIDATE_KIND)
        if not isinstance(self.display_name, str):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "display_name", "expected": "str"},
            )
        if self.id.kind is not self.kind:
            raise DomainError(
                ErrorCode.CANDIDATE_KIND_MISMATCH,
                context={
                    "candidate_id": str(self.id),
                    "declared_kind": self.kind.value,
                },
            )

    @classmethod
    def from_dict(cls, payload: object) -> Candidate:
        payload = check_fields(payload, _CANDIDATE_FIELDS, field="candidate")
        raw_kind = payload["kind"]
        if not isinstance(raw_kind, str):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "kind", "expected": "str"},
            )
        try:
            kind = CandidateKind(raw_kind)
        except ValueError as error:
            raise DomainError(ErrorCode.INVALID_CANDIDATE_KIND) from error

        return cls(
            id=CandidateId.parse(payload["id"]),
            kind=kind,
            display_name=payload["display_name"],
        )


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    """An immutable collection whose IDs are unique within one run."""

    candidates: tuple[Candidate, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, tuple):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "candidates", "expected": "tuple"},
            )

        seen: set[CandidateId] = set()
        for candidate in self.candidates:
            if not isinstance(candidate, Candidate):
                raise DomainError(
                    ErrorCode.INVALID_FIELD_TYPE,
                    context={"field": "candidates", "expected": "Candidate"},
                )
            if candidate.id in seen:
                raise DomainError(
                    ErrorCode.DUPLICATE_CANDIDATE_ID,
                    context={"candidate_id": str(candidate.id)},
                )
            seen.add(candidate.id)

    @classmethod
    def create(cls, candidates: Iterable[Candidate]) -> CandidateSnapshot:
        return cls(candidates=tuple(candidates))
