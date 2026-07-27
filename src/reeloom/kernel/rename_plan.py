from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import cast

from reeloom.kernel.candidates import Candidate, CandidateId, CandidateKind
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.mapping import (
    EpisodeCatalog,
    MappingDraft,
    VideoMapping,
)
from reeloom.kernel.naming import SeriesIdentity, SubtitleVariant
from reeloom.kernel.plan import PlanDraft, PlannedMove
from reeloom.kernel.schema import check_fields
from reeloom.kernel.scanner import (
    CandidateRecord,
    ScannedFile,
    ScannedCandidateSnapshot,
    build_candidate_snapshot,
    rebuild_candidate_snapshot,
)
from reeloom.kernel.tmdb import TmdbWorkType

CURRENT_RENAME_PLAN_SCHEMA_VERSION = "1"
CURRENT_RENAME_PLAN_POLICY_VERSION = "m5-v1"

_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_RUN_ID_BYTES = 128
_MAX_CANONICAL_PLAN_BYTES = 8 * 1024 * 1024
_PLAN_FIELDS = frozenset(
    {
        "candidate_snapshot_id",
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
        "work_type",
    }
)
_ROOTS_FIELDS = frozenset({"output", "source"})
_ROOT_FIELDS = frozenset({"device", "inode", "path"})
_SOURCE_FIELDS = frozenset(
    {
        "candidate_id",
        "ctime_ns",
        "device",
        "inode",
        "kind",
        "mtime_ns",
        "relative_path",
        "sample_digest",
        "size_bytes",
    }
)
_MOVE_FIELDS = frozenset(
    {
        "destination",
        "destination_preflight",
        "episode_end",
        "episode_start",
        "season",
        "source_id",
        "video_id",
    }
)
_VARIANT_FIELDS = frozenset({"subtitle_id", "variant"})


def _candidate_sort_key(candidate_id: CandidateId) -> tuple[int, int]:
    return (
        0 if candidate_id.kind is CandidateKind.VIDEO else 1,
        candidate_id.ordinal,
    )


@dataclass(frozen=True, slots=True)
class RootBinding:
    """An authorized absolute root and the directory identity seen at build time."""

    path: PurePosixPath
    device: int
    inode: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, PurePosixPath)
            or not self.path.is_absolute()
            or ".." in self.path.parts
            or any(
                part.casefold().startswith(".env")
                for part in self.path.parts
            )
            or type(self.device) is not int
            or self.device < 0
            or type(self.inode) is not int
            or self.inode < 0
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)


@dataclass(frozen=True, slots=True)
class PlanSource:
    candidate_id: CandidateId
    kind: CandidateKind
    relative_path: PurePosixPath
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int
    sample_digest: str | None

    @classmethod
    def from_record(cls, record: CandidateRecord) -> PlanSource:
        identities = (
            record.device,
            record.inode,
            record.mtime_ns,
            record.ctime_ns,
        )
        if any(type(value) is not int for value in identities):
            raise DomainError(
                ErrorCode.INCOMPLETE_SOURCE_IDENTITY,
                context={"candidate_id": str(record.candidate.id)},
            )
        if (
            record.candidate.kind is CandidateKind.SUBTITLE
            and record.sample_digest is None
        ):
            raise DomainError(
                ErrorCode.INCOMPLETE_SOURCE_IDENTITY,
                context={"candidate_id": str(record.candidate.id)},
            )
        return cls(
            candidate_id=record.candidate.id,
            kind=record.candidate.kind,
            relative_path=record.relative_path,
            size_bytes=record.size_bytes,
            device=cast(int, record.device),
            inode=cast(int, record.inode),
            mtime_ns=cast(int, record.mtime_ns),
            ctime_ns=cast(int, record.ctime_ns),
            sample_digest=record.sample_digest,
        )


@dataclass(frozen=True, slots=True)
class PlanPreviewMove:
    candidate_id: CandidateId
    source: PurePosixPath
    destination: PurePosixPath


@dataclass(frozen=True, slots=True)
class PlanPreviewUnmapped:
    candidate_id: CandidateId
    source: PurePosixPath


@dataclass(frozen=True, slots=True)
class PlanPreview:
    plan_hash: str
    moves: tuple[PlanPreviewMove, ...]
    unmapped: tuple[PlanPreviewUnmapped, ...]


def _validated_variants(
    variants: Iterable[tuple[CandidateId, SubtitleVariant]],
    *,
    mapping: MappingDraft,
    candidates: ScannedCandidateSnapshot,
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
        record.candidate.id for record in candidates.records
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


def compile_plan_draft(
    *,
    series: SeriesIdentity,
    mapping: MappingDraft,
    candidates: ScannedCandidateSnapshot,
    subtitle_variants: Iterable[
        tuple[CandidateId, SubtitleVariant]
    ],
) -> PlanDraft:
    """Compile destinations from trusted domain data, never caller paths."""

    if not isinstance(candidates, ScannedCandidateSnapshot):
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
        record = candidates.record_for(video.video_id)
        moves.append(
            PlannedMove.for_video(
                source_id=video.video_id,
                series=series,
                span=video.span,
                extension=record.relative_path.suffix,
            )
        )
    for subtitle in mapping.subtitles:
        record = candidates.record_for(subtitle.subtitle_id)
        moves.append(
            PlannedMove.for_subtitle(
                source_id=subtitle.subtitle_id,
                video_id=subtitle.video_id,
                series=series,
                span=video_mappings[subtitle.video_id].span,
                variant=variants[subtitle.subtitle_id],
                extension=record.relative_path.suffix,
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


def _root_payload(root: RootBinding) -> dict[str, object]:
    return {
        "device": root.device,
        "inode": root.inode,
        "path": root.path.as_posix(),
    }


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


def _source_payload(source: PlanSource) -> dict[str, object]:
    return {
        "candidate_id": str(source.candidate_id),
        "ctime_ns": source.ctime_ns,
        "device": source.device,
        "inode": source.inode,
        "kind": source.kind.value,
        "mtime_ns": source.mtime_ns,
        "relative_path": source.relative_path.as_posix(),
        "sample_digest": source.sample_digest,
        "size_bytes": source.size_bytes,
    }


def _move_payload(move: PlannedMove) -> dict[str, object]:
    return {
        "destination": move.destination.as_posix(),
        "destination_preflight": "absent",
        "episode_end": move.span.episode_end,
        "episode_start": move.span.episode_start,
        "season": move.span.season,
        "source_id": str(move.source_id),
        "video_id": str(move.video_id),
    }


def _plan_hash(canonical_bytes: bytes) -> str:
    return f"sha256:{hashlib.sha256(canonical_bytes).hexdigest()}"


def is_valid_plan_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and _HASH_PATTERN.fullmatch(value) is not None
    )


def verify_plan_bytes(canonical_bytes: bytes, plan_hash: str) -> bool:
    if (
        not isinstance(canonical_bytes, bytes)
        or not is_valid_plan_hash(plan_hash)
    ):
        return False
    return hmac.compare_digest(_plan_hash(canonical_bytes), plan_hash)


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate key")
        payload[key] = value
    return payload


def _decode_canonical_plan(canonical_bytes: bytes) -> dict[str, object]:
    if (
        not isinstance(canonical_bytes, bytes)
        or not 0 < len(canonical_bytes) <= _MAX_CANONICAL_PLAN_BYTES
    ):
        raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
    try:
        payload = json.loads(
            canonical_bytes,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise DomainError(ErrorCode.INVALID_FIELD_TYPE) from None
    return dict(check_fields(payload, _PLAN_FIELDS, field="rename_plan"))


def _require_list(value: object, *, field: str) -> list[object]:
    if not isinstance(value, list):
        raise DomainError(
            ErrorCode.INVALID_FIELD_TYPE,
            context={"field": field, "expected": "list"},
        )
    return value


def _require_str(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise DomainError(
            ErrorCode.INVALID_FIELD_TYPE,
            context={"field": field, "expected": "str"},
        )
    return value


def _parse_root(value: object, *, field: str) -> RootBinding:
    payload = check_fields(value, _ROOT_FIELDS, field=field)
    path = PurePosixPath(_require_str(payload["path"], field=f"{field}.path"))
    return RootBinding(
        path=path,
        device=payload["device"],  # type: ignore[arg-type]
        inode=payload["inode"],  # type: ignore[arg-type]
    )


def _parse_sources(
    value: object,
    *,
    preserve_candidate_ids: bool = False,
) -> ScannedCandidateSnapshot:
    raw_sources = _require_list(value, field="sources")
    parsed: list[tuple[CandidateId, ScannedFile]] = []
    for index, item in enumerate(raw_sources):
        field = f"sources[{index}]"
        payload = check_fields(item, _SOURCE_FIELDS, field=field)
        raw_kind = _require_str(payload["kind"], field=f"{field}.kind")
        try:
            kind = CandidateKind(raw_kind)
        except ValueError:
            raise DomainError(ErrorCode.INVALID_CANDIDATE_KIND) from None
        parsed.append(
            (
                CandidateId.parse(payload["candidate_id"]),
                ScannedFile(
                    relative_path=PurePosixPath(
                        _require_str(
                            payload["relative_path"],
                            field=f"{field}.relative_path",
                        )
                    ),
                    kind=kind,
                    size_bytes=payload["size_bytes"],  # type: ignore[arg-type]
                    device=payload["device"],  # type: ignore[arg-type]
                    inode=payload["inode"],  # type: ignore[arg-type]
                    mtime_ns=payload["mtime_ns"],  # type: ignore[arg-type]
                    ctime_ns=payload["ctime_ns"],  # type: ignore[arg-type]
                    sample_digest=payload["sample_digest"],  # type: ignore[arg-type]
                ),
            )
        )
    if preserve_candidate_ids:
        return rebuild_candidate_snapshot(
            CandidateRecord(
                candidate=Candidate(
                    id=candidate_id,
                    kind=scanned.kind,
                    display_name=scanned.relative_path.as_posix(),
                ),
                relative_path=scanned.relative_path,
                size_bytes=scanned.size_bytes,
                device=scanned.device,
                inode=scanned.inode,
                mtime_ns=scanned.mtime_ns,
                ctime_ns=scanned.ctime_ns,
                sample_digest=scanned.sample_digest,
            )
            for candidate_id, scanned in parsed
        )
    scanned = [item for _, item in parsed]
    snapshot = build_candidate_snapshot(scanned)
    if tuple(candidate_id for candidate_id, _ in parsed) != tuple(
        record.candidate.id for record in snapshot.records
    ):
        raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)
    return snapshot


def _parse_mapping(
    value: object,
    *,
    candidates: ScannedCandidateSnapshot,
) -> MappingDraft:
    payload = check_fields(
        value,
        frozenset({"videos", "subtitles"}),
        field="mapping",
    )
    raw_videos = _require_list(payload["videos"], field="mapping.videos")
    counts: dict[int, int] = {}
    for item in raw_videos:
        video = VideoMapping.from_dict(item)
        counts[video.span.season] = max(
            counts.get(video.span.season, 0),
            video.span.episode_end,
        )
    return MappingDraft.from_dict(
        payload,
        candidates=candidates.candidates,
        catalog=EpisodeCatalog.from_counts(counts),
    )


def _parse_variants(
    value: object,
) -> tuple[tuple[CandidateId, SubtitleVariant], ...]:
    variants: list[tuple[CandidateId, SubtitleVariant]] = []
    for index, item in enumerate(
        _require_list(value, field="subtitle_variants")
    ):
        field = f"subtitle_variants[{index}]"
        payload = check_fields(item, _VARIANT_FIELDS, field=field)
        raw_variant = _require_str(
            payload["variant"],
            field=f"{field}.variant",
        )
        try:
            variant = SubtitleVariant(raw_variant)
        except ValueError:
            raise DomainError(ErrorCode.INVALID_SUBTITLE_VARIANT) from None
        variants.append(
            (CandidateId.parse(payload["subtitle_id"]), variant)
        )
    return tuple(variants)


def _parse_checked_destinations(value: object) -> tuple[PurePosixPath, ...]:
    destinations: list[PurePosixPath] = []
    for index, item in enumerate(_require_list(value, field="moves")):
        field = f"moves[{index}]"
        payload = check_fields(item, _MOVE_FIELDS, field=field)
        if payload["destination_preflight"] != "absent":
            raise DomainError(ErrorCode.PLAN_PREFLIGHT_MISMATCH)
        destinations.append(
            PurePosixPath(
                _require_str(
                    payload["destination"],
                    field=f"{field}.destination",
                )
            )
        )
    return tuple(destinations)


@dataclass(frozen=True, slots=True, init=False)
class RenamePlan:
    """Canonical, immutable transaction input suitable for exact approval."""

    schema_version: str
    policy_version: str
    run_id: str
    work_type: TmdbWorkType
    created_at: str
    source_root: RootBinding
    output_root: RootBinding
    candidate_snapshot_id: str
    sources: tuple[PlanSource, ...]
    subtitle_variants: tuple[
        tuple[CandidateId, SubtitleVariant],
        ...,
    ]
    draft: PlanDraft
    plan_hash: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        work_type: TmdbWorkType,
        created_at: datetime,
        source_root: RootBinding,
        output_root: RootBinding,
        candidate_snapshot: ScannedCandidateSnapshot,
        subtitle_variants: Iterable[
            tuple[CandidateId, SubtitleVariant]
        ],
        draft: PlanDraft,
        checked_destinations: Iterable[PurePosixPath],
    ) -> RenamePlan:
        if (
            not isinstance(run_id, str)
            or not run_id
            or len(run_id.encode("utf-8")) > _MAX_RUN_ID_BYTES
            or any(unicodedata.category(char).startswith("C") for char in run_id)
            or not isinstance(work_type, TmdbWorkType)
            or not work_type.supports_episodes
            or not isinstance(source_root, RootBinding)
            or not isinstance(output_root, RootBinding)
            or not isinstance(candidate_snapshot, ScannedCandidateSnapshot)
            or not isinstance(draft, PlanDraft)
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)

        canonical_variants = _validated_variants(
            subtitle_variants,
            mapping=draft.mapping,
            candidates=candidate_snapshot,
        )
        expected_draft = compile_plan_draft(
            series=draft.series,
            mapping=draft.mapping,
            candidates=candidate_snapshot,
            subtitle_variants=canonical_variants,
        )
        if expected_draft != draft:
            raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)

        checked = tuple(checked_destinations)
        expected_destinations = tuple(
            move.destination for move in draft.moves
        )
        if (
            any(
                not isinstance(path, PurePosixPath)
                for path in checked
            )
            or len(checked) != len(expected_destinations)
            or len(set(checked)) != len(checked)
            or set(checked) != set(expected_destinations)
        ):
            raise DomainError(ErrorCode.PLAN_PREFLIGHT_MISMATCH)

        sources = tuple(
            PlanSource.from_record(record)
            for record in candidate_snapshot.records
        )
        plan = object.__new__(cls)
        object.__setattr__(
            plan,
            "schema_version",
            CURRENT_RENAME_PLAN_SCHEMA_VERSION,
        )
        object.__setattr__(
            plan,
            "policy_version",
            CURRENT_RENAME_PLAN_POLICY_VERSION,
        )
        object.__setattr__(plan, "run_id", run_id)
        object.__setattr__(plan, "work_type", work_type)
        object.__setattr__(
            plan,
            "created_at",
            _canonical_timestamp(created_at),
        )
        object.__setattr__(plan, "source_root", source_root)
        object.__setattr__(plan, "output_root", output_root)
        object.__setattr__(
            plan,
            "candidate_snapshot_id",
            candidate_snapshot.snapshot_id,
        )
        object.__setattr__(plan, "sources", sources)
        object.__setattr__(
            plan,
            "subtitle_variants",
            canonical_variants,
        )
        object.__setattr__(plan, "draft", draft)
        object.__setattr__(
            plan,
            "plan_hash",
            _plan_hash(plan.canonical_bytes()),
        )
        return plan

    @classmethod
    def from_canonical_bytes(
        cls,
        canonical_bytes: bytes,
        *,
        plan_hash: str,
    ) -> RenamePlan:
        """Rebuild a plan through the existing deterministic compiler."""

        if not verify_plan_bytes(canonical_bytes, plan_hash):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        payload = _decode_canonical_plan(canonical_bytes)
        roots = check_fields(
            payload["roots"],
            _ROOTS_FIELDS,
            field="roots",
        )
        candidates = _parse_sources(payload["sources"])
        mapping = _parse_mapping(
            payload["mapping"],
            candidates=candidates,
        )
        series = SeriesIdentity.from_dict(payload["series"])
        variants = _parse_variants(payload["subtitle_variants"])
        draft = compile_plan_draft(
            series=series,
            mapping=mapping,
            candidates=candidates,
            subtitle_variants=variants,
        )
        created_at_text = _require_str(
            payload["created_at"],
            field="created_at",
        )
        try:
            created_at = datetime.fromisoformat(
                created_at_text.replace("Z", "+00:00")
            )
            work_type = TmdbWorkType(
                _require_str(payload["work_type"], field="work_type")
            )
        except ValueError:
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE) from None

        plan = cls.create(
            run_id=_require_str(payload["run_id"], field="run_id"),
            work_type=work_type,
            created_at=created_at,
            source_root=_parse_root(roots["source"], field="roots.source"),
            output_root=_parse_root(roots["output"], field="roots.output"),
            candidate_snapshot=candidates,
            subtitle_variants=variants,
            draft=draft,
            checked_destinations=_parse_checked_destinations(
                payload["moves"]
            ),
        )
        if (
            plan.plan_hash != plan_hash
            or plan.canonical_bytes() != canonical_bytes
        ):
            raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)
        return plan

    def canonical_bytes(self) -> bytes:
        payload = {
            "candidate_snapshot_id": self.candidate_snapshot_id,
            "created_at": self.created_at,
            "draft_policy_version": self.draft.policy_version,
            "draft_schema_version": self.draft.schema_version,
            "mapping": _mapping_payload(self.draft.mapping),
            "moves": [_move_payload(move) for move in self.draft.moves],
            "policy_version": self.policy_version,
            "roots": {
                "output": _root_payload(self.output_root),
                "source": _root_payload(self.source_root),
            },
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "series": _series_payload(self.draft.series),
            "sources": [
                _source_payload(source) for source in self.sources
            ],
            "subtitle_variants": [
                {
                    "subtitle_id": str(candidate_id),
                    "variant": variant.value,
                }
                for candidate_id, variant in self.subtitle_variants
            ],
            "unmapped_candidate_ids": [
                str(candidate_id)
                for candidate_id in self.draft.unmapped_candidate_ids
            ],
            "work_type": self.work_type.value,
        }
        return json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @property
    def preview(self) -> PlanPreview:
        sources = {
            source.candidate_id: source for source in self.sources
        }
        return PlanPreview(
            plan_hash=self.plan_hash,
            moves=tuple(
                PlanPreviewMove(
                    candidate_id=move.source_id,
                    source=sources[move.source_id].relative_path,
                    destination=move.destination,
                )
                for move in self.draft.moves
            ),
            unmapped=tuple(
                PlanPreviewUnmapped(
                    candidate_id=candidate_id,
                    source=sources[candidate_id].relative_path,
                )
                for candidate_id in self.draft.unmapped_candidate_ids
            ),
        )

    def verify_hash(self) -> bool:
        return verify_plan_bytes(self.canonical_bytes(), self.plan_hash)
