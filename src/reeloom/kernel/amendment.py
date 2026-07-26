from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath

from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.rename_plan import RootBinding

CURRENT_AMENDMENT_SCHEMA_VERSION = "1"
CURRENT_AMENDMENT_POLICY_VERSION = "m8-v1"
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_TRANSACTION = re.compile(r"^txn-v1-[0-9a-f]{64}$")


def _relative(value: object) -> PurePosixPath:
    if (
        not isinstance(value, PurePosixPath)
        or value.is_absolute()
        or not value.parts
        or ".." in value.parts
        or any(
            part in {"", ".", ".."}
            or part.casefold().startswith(".env")
            or len(part.encode("utf-8")) > 255
            for part in value.parts
        )
    ):
        raise DomainError(ErrorCode.INVALID_DESTINATION)
    return value


@dataclass(frozen=True, slots=True)
class CompletedLayoutFile:
    candidate_id: CandidateId
    kind: CandidateKind
    relative_path: PurePosixPath
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int
    sample_digest: str | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.candidate_id, CandidateId)
            or self.candidate_id.kind is not self.kind
            or not isinstance(self.kind, CandidateKind)
            or any(
                type(value) is not int or value < 0
                for value in (
                    self.size_bytes,
                    self.device,
                    self.inode,
                    self.mtime_ns,
                    self.ctime_ns,
                )
            )
            or (
                self.kind is CandidateKind.SUBTITLE
                and (
                    not isinstance(self.sample_digest, str)
                    or re.fullmatch(
                        r"[0-9a-f]{64}", self.sample_digest
                    )
                    is None
                )
            )
            or (
                self.kind is CandidateKind.VIDEO
                and self.sample_digest is not None
            )
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        _relative(self.relative_path)


@dataclass(frozen=True, slots=True)
class CompletedLayout:
    run_id: str
    original_plan_hash: str
    transaction_id: str
    root: RootBinding
    files: tuple[CompletedLayoutFile, ...]

    def __post_init__(self) -> None:
        ids = tuple(item.candidate_id for item in self.files)
        paths = tuple(item.relative_path for item in self.files)
        if (
            not isinstance(self.run_id, str)
            or not self.run_id
            or _HASH.fullmatch(self.original_plan_hash) is None
            or _TRANSACTION.fullmatch(self.transaction_id) is None
            or not isinstance(self.root, RootBinding)
            or not isinstance(self.files, tuple)
            or not self.files
            or any(
                not isinstance(item, CompletedLayoutFile)
                for item in self.files
            )
            or len(set(ids)) != len(ids)
            or len(set(paths)) != len(paths)
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)


@dataclass(frozen=True, slots=True)
class DesiredLayoutMove:
    source_id: CandidateId
    video_id: CandidateId
    destination: PurePosixPath
    season: int
    episode_start: int
    episode_end: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_id, CandidateId)
            or not isinstance(self.video_id, CandidateId)
            or self.video_id.kind is not CandidateKind.VIDEO
            or type(self.season) is not int
            or not 0 <= self.season <= 999
            or type(self.episode_start) is not int
            or type(self.episode_end) is not int
            or not 1 <= self.episode_start <= self.episode_end
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        _relative(self.destination)


@dataclass(frozen=True, slots=True)
class AmendmentPlan:
    plan_hash: str
    run_id: str
    parent_plan_hash: str
    completed_transaction_id: str
    created_at: datetime
    source_root: RootBinding
    output_root: RootBinding
    sources: tuple[CompletedLayoutFile, ...]
    moves: tuple[DesiredLayoutMove, ...]

    def _payload(self) -> dict[str, object]:
        return {
            "completed_transaction_id": self.completed_transaction_id,
            "created_at": (
                self.created_at.astimezone(UTC)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            ),
            "moves": [
                {
                    "destination": item.destination.as_posix(),
                    "destination_preflight": "absent",
                    "episode_end": item.episode_end,
                    "episode_start": item.episode_start,
                    "season": item.season,
                    "source_id": str(item.source_id),
                    "video_id": str(item.video_id),
                }
                for item in self.moves
            ],
            "parent_plan_hash": self.parent_plan_hash,
            "policy_version": CURRENT_AMENDMENT_POLICY_VERSION,
            "roots": {
                "output": {
                    "device": self.output_root.device,
                    "inode": self.output_root.inode,
                    "path": self.output_root.path.as_posix(),
                },
                "source": {
                    "device": self.source_root.device,
                    "inode": self.source_root.inode,
                    "path": self.source_root.path.as_posix(),
                },
            },
            "run_id": self.run_id,
            "schema_version": CURRENT_AMENDMENT_SCHEMA_VERSION,
            "sources": [
                {
                    "candidate_id": str(item.candidate_id),
                    "ctime_ns": item.ctime_ns,
                    "device": item.device,
                    "inode": item.inode,
                    "kind": item.kind.value,
                    "mtime_ns": item.mtime_ns,
                    "relative_path": item.relative_path.as_posix(),
                    "sample_digest": item.sample_digest,
                    "size_bytes": item.size_bytes,
                }
                for item in self.sources
            ],
            "unmapped_candidate_ids": [
                str(item.candidate_id)
                for item in self.sources
                if item.candidate_id
                not in {move.source_id for move in self.moves}
            ],
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self._payload(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

    def verify_hash(self) -> bool:
        return self.plan_hash == (
            "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()
        )


def compile_amendment(
    *,
    layout: CompletedLayout,
    desired: tuple[DesiredLayoutMove, ...],
    created_at: datetime,
) -> AmendmentPlan | None:
    if (
        not isinstance(layout, CompletedLayout)
        or not isinstance(desired, tuple)
        or any(not isinstance(item, DesiredLayoutMove) for item in desired)
        or not isinstance(created_at, datetime)
        or created_at.tzinfo is None
    ):
        raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
    files = {item.candidate_id: item for item in layout.files}
    desired_ids = tuple(item.source_id for item in desired)
    destinations = tuple(item.destination for item in desired)
    if (
        set(desired_ids) != set(files)
        or len(set(desired_ids)) != len(desired_ids)
        or len(set(destinations)) != len(destinations)
    ):
        raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)
    occupied = {
        item.relative_path: item.candidate_id for item in layout.files
    }
    changed: list[DesiredLayoutMove] = []
    for item in desired:
        source = files[item.source_id]
        if item.destination == source.relative_path:
            continue
        occupying = occupied.get(item.destination)
        if occupying is not None and occupying != item.source_id:
            raise DomainError(ErrorCode.DESTINATION_COLLISION)
        changed.append(item)
    if not changed:
        return None
    provisional = AmendmentPlan(
        plan_hash="",
        run_id=layout.run_id,
        parent_plan_hash=layout.original_plan_hash,
        completed_transaction_id=layout.transaction_id,
        created_at=created_at,
        source_root=layout.root,
        output_root=layout.root,
        sources=layout.files,
        moves=tuple(changed),
    )
    plan_hash = (
        "sha256:"
        + hashlib.sha256(provisional.canonical_bytes()).hexdigest()
    )
    return AmendmentPlan(
        plan_hash=plan_hash,
        run_id=provisional.run_id,
        parent_plan_hash=provisional.parent_plan_hash,
        completed_transaction_id=provisional.completed_transaction_id,
        created_at=provisional.created_at,
        source_root=provisional.source_root,
        output_root=provisional.output_root,
        sources=provisional.sources,
        moves=provisional.moves,
    )


def verify_amendment_bytes(content: bytes, plan_hash: str) -> bool:
    if (
        not isinstance(content, bytes)
        or not isinstance(plan_hash, str)
        or _HASH.fullmatch(plan_hash) is None
        or plan_hash
        != "sha256:" + hashlib.sha256(content).hexdigest()
    ):
        return False
    try:
        payload = json.loads(content.decode("ascii"))
        return (
            isinstance(payload, dict)
            and payload.get("schema_version")
            == CURRENT_AMENDMENT_SCHEMA_VERSION
            and payload.get("policy_version")
            == CURRENT_AMENDMENT_POLICY_VERSION
            and "completed_transaction_id" in payload
            and json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            == content
        )
    except (UnicodeError, ValueError):
        return False
