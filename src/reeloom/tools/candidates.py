from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from reeloom.kernel.candidates import (
    Candidate,
    CandidateId,
    CandidateKind,
    CandidateSnapshot,
)
from reeloom.kernel.errors import DomainError
from reeloom.kernel.scanner import ScannedCandidateSnapshot
from reeloom.kernel.semantic_identity import SemanticCandidateSnapshot
from reeloom.runtime.errors import (
    RuntimeDomainError,
    RuntimeErrorCode,
)
from reeloom.runtime.tool_runtime import ToolRuntime

_TOOL_NAME = "list_candidates"
MAX_PAGE_SIZE = 50
MAX_CURSOR = (1 << 31) - 1
_MAX_DISPLAY_NAME_BYTES = 240
_MAX_OBSERVATION_BYTES = 64 * 1024


class ToolFailureCode(StrEnum):
    TEMPORARY_UNAVAILABLE = "temporary_unavailable"
    SOURCE_FAILURE = "source_failure"
    INVALID_CURSOR = "invalid_cursor"


class ToolExecutionError(RuntimeError):
    def __init__(
        self,
        code: ToolFailureCode,
        *,
        retryable: bool,
    ) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class CandidatePage:
    items: tuple[Candidate, ...]
    next_cursor: int | None


class CandidateSource(Protocol):
    @property
    def snapshot_id(self) -> str: ...

    @property
    def candidate_count(self) -> int: ...

    async def page(
        self,
        *,
        kind: CandidateKind,
        cursor: int,
        limit: int,
    ) -> CandidatePage: ...


def _candidate_snapshot_id(snapshot: CandidateSnapshot) -> str:
    canonical = json.dumps(
        [
            {
                "id": str(candidate.id),
                "kind": candidate.kind.value,
                "display_name": candidate.display_name,
            }
            for candidate in snapshot.candidates
        ],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "candidate-snapshot-v1:" + hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class SnapshotCandidateSource:
    """A bounded view over one immutable, run-scoped snapshot."""

    snapshot: CandidateSnapshot
    snapshot_id: str

    def __init__(self, snapshot: CandidateSnapshot) -> None:
        if not isinstance(snapshot, CandidateSnapshot):
            raise TypeError("snapshot must be CandidateSnapshot")
        object.__setattr__(self, "snapshot", snapshot)
        object.__setattr__(
            self,
            "snapshot_id",
            _candidate_snapshot_id(snapshot),
        )

    @classmethod
    def from_scanned(
        cls,
        snapshot: ScannedCandidateSnapshot,
    ) -> SnapshotCandidateSource:
        instance = object.__new__(cls)
        object.__setattr__(instance, "snapshot", snapshot.candidates)
        object.__setattr__(instance, "snapshot_id", snapshot.snapshot_id)
        return instance

    @classmethod
    def from_semantic(
        cls,
        snapshot: SemanticCandidateSnapshot,
    ) -> SnapshotCandidateSource:
        instance = object.__new__(cls)
        object.__setattr__(instance, "snapshot", snapshot.candidates)
        object.__setattr__(instance, "snapshot_id", snapshot.snapshot_id)
        return instance

    @property
    def candidate_count(self) -> int:
        return len(self.snapshot.candidates)

    async def page(
        self,
        *,
        kind: CandidateKind,
        cursor: int,
        limit: int,
    ) -> CandidatePage:
        candidates = tuple(
            candidate
            for candidate in self.snapshot.candidates
            if candidate.kind is kind
        )
        if cursor > len(candidates):
            raise ToolExecutionError(
                ToolFailureCode.INVALID_CURSOR,
                retryable=True,
            )
        items = candidates[cursor : cursor + limit]
        next_offset = cursor + len(items)
        return CandidatePage(
            items=items,
            next_cursor=(
                next_offset if next_offset < len(candidates) else None
            ),
        )


def _bounded_display_name(value: str) -> str:
    visible = "".join(
        character
        if not unicodedata.category(character).startswith("C")
        else "\N{REPLACEMENT CHARACTER}"
        for character in value
    )
    encoded = visible.encode("utf-8")
    if len(encoded) <= _MAX_DISPLAY_NAME_BYTES:
        return visible
    return encoded[:_MAX_DISPLAY_NAME_BYTES].decode(
        "utf-8",
        errors="ignore",
    )


def _error_observation(
    code: str,
    *,
    retryable: bool,
) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": {"code": code, "retryable": retryable},
        },
        separators=(",", ":"),
        sort_keys=True,
    )


async def list_candidates(
    runtime: ToolRuntime,
    source: CandidateSource,
    *,
    call_id: str,
    kind: CandidateKind,
    cursor: int,
    limit: int,
) -> str:
    """Return only run-scoped IDs; never accept or reveal filesystem paths."""

    try:
        runtime.begin(call_id=call_id, tool_name=_TOOL_NAME)
    except RuntimeDomainError as error:
        if error.code in {
            RuntimeErrorCode.TOOL_NOT_ALLOWED,
            RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE,
        }:
            return _error_observation(
                error.code.value,
                retryable=(
                    error.code is RuntimeErrorCode.TOOL_NOT_ALLOWED
                ),
            )
        raise

    state = runtime.state
    if (
        source.snapshot_id != state.candidate_snapshot_id
        or source.candidate_count != state.candidate_count
    ):
        runtime.reject(
            call_id=call_id,
            tool_name=_TOOL_NAME,
            code=RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE.value,
            retryable=False,
        )
        return _error_observation(
            RuntimeErrorCode.CAPABILITY_NOT_AVAILABLE.value,
            retryable=False,
        )

    if (
        not isinstance(kind, CandidateKind)
        or type(cursor) is not int
        or not 0 <= cursor <= MAX_CURSOR
        or type(limit) is not int
        or not 1 <= limit <= MAX_PAGE_SIZE
    ):
        runtime.reject(
            call_id=call_id,
            tool_name=_TOOL_NAME,
            code=RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value,
            retryable=True,
        )
        return _error_observation(
            RuntimeErrorCode.INVALID_TOOL_ARGUMENTS.value,
            retryable=True,
        )

    try:
        page = await source.page(
            kind=kind,
            cursor=cursor,
            limit=limit,
        )
    except ToolExecutionError as error:
        runtime.reject(
            call_id=call_id,
            tool_name=_TOOL_NAME,
            code=error.code.value,
            retryable=error.retryable,
        )
        if error.retryable:
            return _error_observation(error.code.value, retryable=True)
        runtime.fail(code=error.code.value)
        raise

    source_is_valid = (
        isinstance(page, CandidatePage)
        and type(page.items) is tuple
        and len(page.items) <= limit
        and all(
            isinstance(candidate, Candidate)
            and candidate.kind is kind
            for candidate in page.items
        )
        and (
            page.next_cursor is None
            or (
                type(page.next_cursor) is int
                and len(page.items) > 0
                and page.next_cursor > cursor
                and page.next_cursor == cursor + len(page.items)
                and page.next_cursor <= MAX_CURSOR
            )
        )
    )
    try:
        canonical_ids = (
            tuple(
                str(CandidateId.parse(str(candidate.id)))
                for candidate in page.items
            )
            if source_is_valid
            else ()
        )
        source_is_valid = source_is_valid and (
            len(set(canonical_ids)) == len(canonical_ids)
        )
    except DomainError:
        source_is_valid = False
        canonical_ids = ()

    if not source_is_valid:
        error = ToolExecutionError(
            ToolFailureCode.SOURCE_FAILURE,
            retryable=False,
        )
        runtime.reject(
            call_id=call_id,
            tool_name=_TOOL_NAME,
            code=error.code.value,
            retryable=False,
        )
        runtime.fail(code=error.code.value)
        raise error

    observation = json.dumps(
        {
            "ok": True,
            "items": [
                {
                    "id": candidate_id,
                    "kind": candidate.kind.value,
                    "display_name": _bounded_display_name(
                        candidate.display_name
                    ),
                }
                for candidate_id, candidate in zip(
                    canonical_ids,
                    page.items,
                    strict=True,
                )
            ],
            "next_cursor": page.next_cursor,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(observation.encode("utf-8")) > _MAX_OBSERVATION_BYTES:
        runtime.reject(
            call_id=call_id,
            tool_name=_TOOL_NAME,
            code=ToolFailureCode.SOURCE_FAILURE.value,
            retryable=False,
        )
        runtime.fail(code=ToolFailureCode.SOURCE_FAILURE.value)
        raise ToolExecutionError(
            ToolFailureCode.SOURCE_FAILURE,
            retryable=False,
        )
    runtime.succeed(call_id=call_id, tool_name=_TOOL_NAME)
    return observation
