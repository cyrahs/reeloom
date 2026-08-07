from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.naming import SeriesIdentity, SubtitleVariant
from reeloom.kernel.plan import PlanDraft, PlannedMove
from reeloom.kernel.semantic_identity import (
    SemanticCandidateSnapshot,
    SemanticRootBinding,
)
from reeloom.kernel.tmdb import TmdbWorkType

CURRENT_FORWARD_PLAN_SCHEMA_VERSION = "2"
CURRENT_FORWARD_PLAN_POLICY_VERSION = "m14-v1"
CURRENT_EXECUTION_OPERATION_SCHEMA_VERSION = "2"

_PLAN_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_CANONICAL_BYTES = 8 * 1024 * 1024
_MAX_TEXT_BYTES = 128
_PLAN_FIELDS = frozenset(
    {
        "candidate_snapshot_id",
        "config_revision",
        "created_at",
        "draft_policy_version",
        "draft_schema_version",
        "mapping",
        "moves",
        "policy_version",
        "roots",
        "run_id",
        "schema_version",
        "series",
        "sources",
        "subtitle_variants",
        "unmapped_candidate_ids",
        "watch_id",
        "work_type",
    }
)


class PathObservationState(StrEnum):
    ABSENT = "absent"
    MATCHING = "matching"
    MISMATCHED = "mismatched"
    UNSAFE = "unsafe"
    UNAVAILABLE = "unavailable"


class ForwardMoveDecision(StrEnum):
    MOVE = "move"
    SATISFIED = "satisfied"
    STALE = "stale"
    COLLISION = "collision"
    UNSAFE = "unsafe"
    UNAVAILABLE = "unavailable"


class ExecutionItemOutcome(StrEnum):
    SATISFIED = "satisfied"
    STALE = "stale"
    COLLISION = "collision"
    UNSAFE = "unsafe"
    UNAVAILABLE = "unavailable"


class ExecutionOperationStatus(StrEnum):
    AUTHORIZED = "authorized"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    STALE = "stale"
    COLLISION = "collision"
    UNSAFE = "unsafe"
    UNAVAILABLE = "unavailable"
    SUPERSEDED = "superseded"

    @property
    def terminal(self) -> bool:
        return self not in {
            ExecutionOperationStatus.AUTHORIZED,
            ExecutionOperationStatus.RUNNING,
        }


def _validate_text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_TEXT_BYTES
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
    return value


@dataclass(frozen=True, slots=True, init=False)
class ExecutionOperation:
    """Pure v2 lifecycle; persistence and leasing arrive in M14.2."""

    schema_version: str
    operation_id: str
    run_id: str
    plan_hash: str
    status: ExecutionOperationStatus
    attempt_count: int
    outcomes: tuple[ExecutionItemOutcome, ...]

    @property
    def terminal(self) -> bool:
        return self.status.terminal

    @classmethod
    def authorized(
        cls,
        *,
        operation_id: str,
        run_id: str,
        plan_hash: str,
    ) -> ExecutionOperation:
        return cls._create(
            operation_id=operation_id,
            run_id=run_id,
            plan_hash=plan_hash,
            status=ExecutionOperationStatus.AUTHORIZED,
            attempt_count=0,
            outcomes=(),
        )

    @classmethod
    def _create(
        cls,
        *,
        operation_id: str,
        run_id: str,
        plan_hash: str,
        status: ExecutionOperationStatus,
        attempt_count: int,
        outcomes: tuple[ExecutionItemOutcome, ...],
    ) -> ExecutionOperation:
        if (
            not isinstance(status, ExecutionOperationStatus)
            or type(attempt_count) is not int
            or attempt_count < 0
            or not isinstance(outcomes, tuple)
            or any(
                not isinstance(item, ExecutionItemOutcome)
                for item in outcomes
            )
            or not isinstance(plan_hash, str)
            or _PLAN_HASH.fullmatch(plan_hash) is None
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        operation = object.__new__(cls)
        object.__setattr__(
            operation,
            "schema_version",
            CURRENT_EXECUTION_OPERATION_SCHEMA_VERSION,
        )
        object.__setattr__(
            operation, "operation_id", _validate_text(operation_id)
        )
        object.__setattr__(operation, "run_id", _validate_text(run_id))
        object.__setattr__(operation, "plan_hash", plan_hash)
        object.__setattr__(operation, "status", status)
        object.__setattr__(operation, "attempt_count", attempt_count)
        object.__setattr__(operation, "outcomes", outcomes)
        return operation

    def begin_or_reconcile(self) -> ExecutionOperation:
        if self.status not in {
            ExecutionOperationStatus.AUTHORIZED,
            ExecutionOperationStatus.RUNNING,
        }:
            raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)
        return self._create(
            operation_id=self.operation_id,
            run_id=self.run_id,
            plan_hash=self.plan_hash,
            status=ExecutionOperationStatus.RUNNING,
            attempt_count=self.attempt_count + 1,
            outcomes=(),
        )

    def settle(
        self,
        outcomes: Iterable[ExecutionItemOutcome],
    ) -> ExecutionOperation:
        if self.status is not ExecutionOperationStatus.RUNNING:
            raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)
        settled = tuple(outcomes)
        return self._create(
            operation_id=self.operation_id,
            run_id=self.run_id,
            plan_hash=self.plan_hash,
            status=reduce_execution_status(settled),
            attempt_count=self.attempt_count,
            outcomes=settled,
        )

    def supersede(self) -> ExecutionOperation:
        if self.status.terminal:
            raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)
        return self._create(
            operation_id=self.operation_id,
            run_id=self.run_id,
            plan_hash=self.plan_hash,
            status=ExecutionOperationStatus.SUPERSEDED,
            attempt_count=self.attempt_count,
            outcomes=(),
        )


def decide_forward_move(
    source: PathObservationState,
    destination: PathObservationState,
) -> ForwardMoveDecision:
    """Resolve one move from current path state, never from journal history."""

    if not isinstance(source, PathObservationState) or not isinstance(
        destination, PathObservationState
    ):
        raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
    if PathObservationState.UNAVAILABLE in {source, destination}:
        return ForwardMoveDecision.UNAVAILABLE
    if PathObservationState.UNSAFE in {source, destination}:
        return ForwardMoveDecision.UNSAFE
    if destination is PathObservationState.MISMATCHED:
        return ForwardMoveDecision.COLLISION
    if source is PathObservationState.MATCHING:
        if destination is PathObservationState.ABSENT:
            return ForwardMoveDecision.MOVE
        return ForwardMoveDecision.COLLISION
    if source is PathObservationState.ABSENT:
        if destination is PathObservationState.MATCHING:
            return ForwardMoveDecision.SATISFIED
        return ForwardMoveDecision.STALE
    return ForwardMoveDecision.STALE


def reduce_execution_status(
    outcomes: Iterable[ExecutionItemOutcome],
) -> ExecutionOperationStatus:
    items = tuple(outcomes)
    if not items or any(
        not isinstance(item, ExecutionItemOutcome) for item in items
    ):
        raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
    satisfied = sum(
        item is ExecutionItemOutcome.SATISFIED for item in items
    )
    if satisfied == len(items):
        return ExecutionOperationStatus.COMPLETED
    if satisfied:
        return ExecutionOperationStatus.PARTIAL
    for outcome, status in (
        (ExecutionItemOutcome.UNSAFE, ExecutionOperationStatus.UNSAFE),
        (
            ExecutionItemOutcome.COLLISION,
            ExecutionOperationStatus.COLLISION,
        ),
        (ExecutionItemOutcome.STALE, ExecutionOperationStatus.STALE),
        (
            ExecutionItemOutcome.UNAVAILABLE,
            ExecutionOperationStatus.UNAVAILABLE,
        ),
    ):
        if outcome in items:
            return status
    raise DomainError(ErrorCode.INVALID_FIELD_TYPE)


def _candidate_sort_key(candidate_id: CandidateId) -> tuple[int, int]:
    return (
        0 if candidate_id.kind is CandidateKind.VIDEO else 1,
        candidate_id.ordinal,
    )


def _validated_variants(
    variants: Iterable[tuple[CandidateId, SubtitleVariant]],
    *,
    mapping: MappingDraft,
    candidates: SemanticCandidateSnapshot,
) -> tuple[tuple[CandidateId, SubtitleVariant], ...]:
    variant_tuple = tuple(variants)
    if any(
        not isinstance(item, tuple)
        or len(item) != 2
        or not isinstance(item[0], CandidateId)
        or item[0].kind is not CandidateKind.SUBTITLE
        or not isinstance(item[1], SubtitleVariant)
        for item in variant_tuple
    ):
        raise DomainError(ErrorCode.INVALID_SUBTITLE_VARIANT)
    variant_map = dict(variant_tuple)
    mapped_ids = {
        subtitle.subtitle_id for subtitle in mapping.subtitles
    }
    candidate_ids = {
        source.candidate_id for source in candidates.sources
    }
    if (
        len(variant_map) != len(variant_tuple)
        or set(variant_map) != mapped_ids
        or not set(variant_map).issubset(candidate_ids)
    ):
        raise DomainError(ErrorCode.SUBTITLE_VARIANT_REQUIRED)
    return tuple(
        sorted(variant_map.items(), key=lambda item: _candidate_sort_key(item[0]))
    )


def compile_plan_draft_v2(
    *,
    series: SeriesIdentity,
    mapping: MappingDraft,
    candidates: SemanticCandidateSnapshot,
    subtitle_variants: Iterable[tuple[CandidateId, SubtitleVariant]],
) -> PlanDraft:
    """Compile destinations from semantic sources, never caller paths."""

    if (
        not isinstance(series, SeriesIdentity)
        or not isinstance(mapping, MappingDraft)
        or not isinstance(candidates, SemanticCandidateSnapshot)
    ):
        raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
    variants = dict(
        _validated_variants(
            subtitle_variants,
            mapping=mapping,
            candidates=candidates,
        )
    )
    video_mappings = {
        video.video_id: video for video in mapping.videos
    }
    moves: list[PlannedMove] = []
    for video in mapping.videos:
        source = candidates.source_for(video.video_id)
        moves.append(
            PlannedMove.for_video(
                source_id=video.video_id,
                series=series,
                span=video.span,
                extension=source.relative_path.suffix,
            )
        )
    for subtitle in mapping.subtitles:
        source = candidates.source_for(subtitle.subtitle_id)
        moves.append(
            PlannedMove.for_subtitle(
                source_id=subtitle.subtitle_id,
                video_id=subtitle.video_id,
                series=series,
                span=video_mappings[subtitle.video_id].span,
                variant=variants[subtitle.subtitle_id],
                extension=source.relative_path.suffix,
            )
        )
    return PlanDraft.create(
        moves,
        series=series,
        mapping=mapping,
        candidates=candidates.candidates,
    )


def _canonical_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
    try:
        if value.utcoffset() is None:
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        return (
            value.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    except DomainError:
        raise
    except Exception:
        raise DomainError(ErrorCode.INVALID_FIELD_TYPE) from None


def _series_payload(series: SeriesIdentity) -> dict[str, object]:
    return {
        "title_zh_cn": series.title_zh_cn,
        "tmdb_id": series.tmdb_id,
        "year": series.year,
    }


def _mapping_payload(mapping: MappingDraft) -> dict[str, object]:
    return {
        "subtitles": [
            {
                "subtitle_id": str(item.subtitle_id),
                "video_id": str(item.video_id),
            }
            for item in sorted(
                mapping.subtitles,
                key=lambda item: _candidate_sort_key(item.subtitle_id),
            )
        ],
        "videos": [
            {
                "episode_end": item.span.episode_end,
                "episode_start": item.span.episode_start,
                "season": item.span.season,
                "video_id": str(item.video_id),
            }
            for item in sorted(
                mapping.videos,
                key=lambda item: _candidate_sort_key(item.video_id),
            )
        ],
    }


def _move_payload(move: PlannedMove) -> dict[str, object]:
    return {
        "destination": move.destination.as_posix(),
        "episode_end": move.span.episode_end,
        "episode_start": move.span.episode_start,
        "season": move.span.season,
        "source_id": str(move.source_id),
        "video_id": str(move.video_id),
    }


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _plan_hash(canonical: bytes) -> str:
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _require_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
    return value


def _parse_mapping(
    value: object,
    *,
    candidates: SemanticCandidateSnapshot,
) -> MappingDraft:
    if not isinstance(value, dict) or set(value) != {"subtitles", "videos"}:
        raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
    videos = _require_list(value["videos"])
    counts: dict[int, int] = {}
    for item in videos:
        if not isinstance(item, dict):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        episode_end = item.get("episode_end")
        season = item.get("season")
        if type(episode_end) is not int or type(season) is not int:
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        counts[season] = max(counts.get(season, 0), episode_end)
    return MappingDraft.from_dict(
        value,
        candidates=candidates.candidates,
        catalog=EpisodeCatalog.from_counts(counts),
    )


def _parse_variants(
    value: object,
) -> tuple[tuple[CandidateId, SubtitleVariant], ...]:
    result: list[tuple[CandidateId, SubtitleVariant]] = []
    for item in _require_list(value):
        if not isinstance(item, dict) or set(item) != {
            "subtitle_id",
            "variant",
        }:
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        try:
            result.append(
                (
                    CandidateId.parse(item["subtitle_id"]),
                    SubtitleVariant(item["variant"]),
                )
            )
        except (TypeError, ValueError):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE) from None
    return tuple(result)


@dataclass(frozen=True, slots=True, init=False)
class RenamePlanV2:
    """Canonical semantic plan rebuilt through deterministic naming."""

    schema_version: str
    policy_version: str
    run_id: str
    config_revision: int
    watch_id: str
    work_type: TmdbWorkType
    created_at: str
    source_root: SemanticRootBinding
    output_root: SemanticRootBinding
    candidate_snapshot: SemanticCandidateSnapshot
    subtitle_variants: tuple[tuple[CandidateId, SubtitleVariant], ...]
    draft: PlanDraft
    plan_hash: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        config_revision: int,
        watch_id: str,
        work_type: TmdbWorkType,
        created_at: datetime,
        source_root: SemanticRootBinding,
        output_root: SemanticRootBinding,
        candidate_snapshot: SemanticCandidateSnapshot,
        subtitle_variants: Iterable[tuple[CandidateId, SubtitleVariant]],
        draft: PlanDraft,
    ) -> RenamePlanV2:
        if (
            type(config_revision) is not int
            or config_revision < 1
            or not isinstance(work_type, TmdbWorkType)
            or not work_type.supports_episodes
            or not isinstance(source_root, SemanticRootBinding)
            or not isinstance(output_root, SemanticRootBinding)
            or not isinstance(candidate_snapshot, SemanticCandidateSnapshot)
            or not isinstance(draft, PlanDraft)
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        variants = _validated_variants(
            subtitle_variants,
            mapping=draft.mapping,
            candidates=candidate_snapshot,
        )
        expected = compile_plan_draft_v2(
            series=draft.series,
            mapping=draft.mapping,
            candidates=candidate_snapshot,
            subtitle_variants=variants,
        )
        if expected != draft:
            raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)

        plan = object.__new__(cls)
        object.__setattr__(
            plan, "schema_version", CURRENT_FORWARD_PLAN_SCHEMA_VERSION
        )
        object.__setattr__(
            plan, "policy_version", CURRENT_FORWARD_PLAN_POLICY_VERSION
        )
        object.__setattr__(plan, "run_id", _validate_text(run_id))
        object.__setattr__(plan, "config_revision", config_revision)
        object.__setattr__(plan, "watch_id", _validate_text(watch_id))
        object.__setattr__(plan, "work_type", work_type)
        object.__setattr__(
            plan, "created_at", _canonical_timestamp(created_at)
        )
        object.__setattr__(plan, "source_root", source_root)
        object.__setattr__(plan, "output_root", output_root)
        object.__setattr__(plan, "candidate_snapshot", candidate_snapshot)
        object.__setattr__(plan, "subtitle_variants", variants)
        object.__setattr__(plan, "draft", draft)
        object.__setattr__(plan, "plan_hash", _plan_hash(plan.canonical_bytes()))
        return plan

    @classmethod
    def from_canonical_bytes(
        cls,
        canonical: bytes,
        *,
        plan_hash: str,
    ) -> RenamePlanV2:
        if (
            not isinstance(canonical, bytes)
            or not 0 < len(canonical) <= _MAX_CANONICAL_BYTES
            or not isinstance(plan_hash, str)
            or _PLAN_HASH.fullmatch(plan_hash) is None
            or not hmac.compare_digest(_plan_hash(canonical), plan_hash)
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        try:
            raw = json.loads(
                canonical,
                object_pairs_hook=_reject_duplicate_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE) from None
        if not isinstance(raw, dict):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        extra = set(raw) - _PLAN_FIELDS
        missing = _PLAN_FIELDS - set(raw)
        if extra:
            raise DomainError(
                ErrorCode.EXTRA_KEYS, context={"keys": tuple(sorted(extra))}
            )
        if missing:
            raise DomainError(
                ErrorCode.MISSING_KEYS,
                context={"keys": tuple(sorted(missing))},
            )
        roots = raw["roots"]
        if not isinstance(roots, dict) or set(roots) != {"output", "source"}:
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        snapshot = SemanticCandidateSnapshot.from_payload(
            raw["sources"],
            snapshot_id=raw["candidate_snapshot_id"],
        )
        mapping = _parse_mapping(raw["mapping"], candidates=snapshot)
        try:
            series = SeriesIdentity.from_dict(raw["series"])
            variants = _parse_variants(raw["subtitle_variants"])
            draft = compile_plan_draft_v2(
                series=series,
                mapping=mapping,
                candidates=snapshot,
                subtitle_variants=variants,
            )
            created_at = raw["created_at"]
            if not isinstance(created_at, str):
                raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
            restored = cls.create(
                run_id=raw["run_id"],  # type: ignore[arg-type]
                config_revision=raw["config_revision"],  # type: ignore[arg-type]
                watch_id=raw["watch_id"],  # type: ignore[arg-type]
                work_type=TmdbWorkType(raw["work_type"]),
                created_at=datetime.fromisoformat(
                    created_at.replace("Z", "+00:00")
                ),
                source_root=SemanticRootBinding.from_payload(roots["source"]),
                output_root=SemanticRootBinding.from_payload(roots["output"]),
                candidate_snapshot=snapshot,
                subtitle_variants=variants,
                draft=draft,
            )
        except (TypeError, ValueError):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE) from None
        if (
            raw["schema_version"] != CURRENT_FORWARD_PLAN_SCHEMA_VERSION
            or raw["policy_version"] != CURRENT_FORWARD_PLAN_POLICY_VERSION
            or raw["draft_schema_version"] != draft.schema_version
            or raw["draft_policy_version"] != draft.policy_version
            or raw["moves"] != [_move_payload(move) for move in draft.moves]
            or raw["unmapped_candidate_ids"]
            != [str(item) for item in draft.unmapped_candidate_ids]
            or restored.plan_hash != plan_hash
            or restored.canonical_bytes() != canonical
        ):
            raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)
        return restored

    def canonical_bytes(self) -> bytes:
        payload = {
            "candidate_snapshot_id": self.candidate_snapshot.snapshot_id,
            "config_revision": self.config_revision,
            "created_at": self.created_at,
            "draft_policy_version": self.draft.policy_version,
            "draft_schema_version": self.draft.schema_version,
            "mapping": _mapping_payload(self.draft.mapping),
            "moves": [_move_payload(move) for move in self.draft.moves],
            "policy_version": self.policy_version,
            "roots": {
                "output": self.output_root.payload(),
                "source": self.source_root.payload(),
            },
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "series": _series_payload(self.draft.series),
            "sources": self.candidate_snapshot.payload(),
            "subtitle_variants": [
                {
                    "subtitle_id": str(candidate_id),
                    "variant": variant.value,
                }
                for candidate_id, variant in self.subtitle_variants
            ],
            "unmapped_candidate_ids": [
                str(item) for item in self.draft.unmapped_candidate_ids
            ],
            "watch_id": self.watch_id,
            "work_type": self.work_type.value,
        }
        return json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def verify_hash(self) -> bool:
        return hmac.compare_digest(
            self.plan_hash, _plan_hash(self.canonical_bytes())
        )
