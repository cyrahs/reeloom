from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.movie import (
    MovieMappingDraft,
    MoviePlanDraft,
    compile_movie_plan_draft,
)
from reeloom.kernel.naming import MovieIdentity, SubtitleVariant
from reeloom.kernel.rename_plan import (
    PlanPreview,
    PlanPreviewMove,
    PlanPreviewUnmapped,
    PlanSource,
    RootBinding,
    _canonical_timestamp,
    _parse_root,
    _parse_sources,
    _plan_hash,
    _reject_duplicate_keys,
    _require_list,
    _require_str,
    _root_payload,
    _source_payload,
    verify_plan_bytes,
)
from reeloom.kernel.schema import check_fields
from reeloom.kernel.scanner import ScannedCandidateSnapshot
from reeloom.kernel.tmdb import TmdbWorkType

CURRENT_MOVIE_PLAN_SCHEMA_VERSION = "movie-rename-plan-v1"
CURRENT_MOVIE_PLAN_POLICY_VERSION = "m10-v1"
_MAX_CANONICAL_BYTES = 8 * 1024 * 1024
_FIELDS = frozenset(
    {
        "candidate_snapshot_id",
        "created_at",
        "mapping",
        "moves",
        "movie",
        "policy_version",
        "roots",
        "run_id",
        "schema_version",
        "sources",
        "subtitle_variants",
        "unmapped_candidate_ids",
        "work_type",
    }
)
_ROOTS_FIELDS = frozenset({"output", "source"})
_MOVE_FIELDS = frozenset(
    {"destination", "destination_preflight", "source_id", "video_id"}
)
_VARIANT_FIELDS = frozenset({"subtitle_id", "variant"})


def _mapping_payload(mapping: MovieMappingDraft) -> dict[str, object]:
    return {
        "subtitle_ids": [
            str(candidate_id) for candidate_id in mapping.subtitle_ids
        ],
        "video_id": str(mapping.video_id),
    }


def _movie_payload(movie: MovieIdentity) -> dict[str, object]:
    return {
        "release_year": movie.release_year,
        "title_zh_cn": movie.title_zh_cn,
        "tmdb_id": movie.tmdb_id,
    }


def _decode(content: bytes) -> dict[str, object]:
    if not isinstance(content, bytes) or not 0 < len(content) <= _MAX_CANONICAL_BYTES:
        raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
    try:
        value = json.loads(
            content,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise DomainError(ErrorCode.INVALID_FIELD_TYPE) from None
    return dict(check_fields(value, _FIELDS, field="movie_rename_plan"))


def _parse_variants(
    value: object,
) -> tuple[tuple[CandidateId, SubtitleVariant], ...]:
    variants: list[tuple[CandidateId, SubtitleVariant]] = []
    for index, item in enumerate(_require_list(value, field="subtitle_variants")):
        payload = check_fields(
            item,
            _VARIANT_FIELDS,
            field=f"subtitle_variants[{index}]",
        )
        try:
            variant = SubtitleVariant(
                _require_str(payload["variant"], field="variant")
            )
        except ValueError:
            raise DomainError(ErrorCode.INVALID_SUBTITLE_VARIANT) from None
        variants.append(
            (CandidateId.parse(payload["subtitle_id"]), variant)
        )
    return tuple(variants)


def _parse_destinations(value: object) -> tuple[PurePosixPath, ...]:
    destinations: list[PurePosixPath] = []
    for index, item in enumerate(_require_list(value, field="moves")):
        payload = check_fields(
            item,
            _MOVE_FIELDS,
            field=f"moves[{index}]",
        )
        if payload["destination_preflight"] != "absent":
            raise DomainError(ErrorCode.PLAN_PREFLIGHT_MISMATCH)
        CandidateId.parse(payload["source_id"])
        CandidateId.parse(payload["video_id"])
        destinations.append(
            PurePosixPath(
                _require_str(payload["destination"], field="destination")
            )
        )
    return tuple(destinations)


@dataclass(frozen=True, slots=True, init=False)
class MovieRenamePlan:
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
        tuple[CandidateId, SubtitleVariant], ...
    ]
    draft: MoviePlanDraft
    plan_hash: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        created_at: datetime,
        source_root: RootBinding,
        output_root: RootBinding,
        candidate_snapshot: ScannedCandidateSnapshot,
        subtitle_variants: tuple[
            tuple[CandidateId, SubtitleVariant], ...
        ],
        draft: MoviePlanDraft,
        checked_destinations: tuple[PurePosixPath, ...],
    ) -> MovieRenamePlan:
        if (
            not isinstance(run_id, str)
            or not run_id
            or len(run_id.encode("utf-8")) > 128
            or any(unicodedata.category(char).startswith("C") for char in run_id)
            or not isinstance(source_root, RootBinding)
            or not isinstance(output_root, RootBinding)
            or not isinstance(candidate_snapshot, ScannedCandidateSnapshot)
            or not isinstance(draft, MoviePlanDraft)
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        expected = compile_movie_plan_draft(
            movie=draft.movie,
            mapping=draft.mapping,
            candidates=candidate_snapshot,
            subtitle_variants=subtitle_variants,
        )
        if expected != draft:
            raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)
        destinations = tuple(move.destination for move in draft.moves)
        if (
            len(set(checked_destinations)) != len(destinations)
            or set(checked_destinations) != set(destinations)
        ):
            raise DomainError(ErrorCode.PLAN_PREFLIGHT_MISMATCH)
        plan = object.__new__(cls)
        object.__setattr__(
            plan, "schema_version", CURRENT_MOVIE_PLAN_SCHEMA_VERSION
        )
        object.__setattr__(
            plan, "policy_version", CURRENT_MOVIE_PLAN_POLICY_VERSION
        )
        object.__setattr__(plan, "run_id", run_id)
        object.__setattr__(plan, "work_type", TmdbWorkType.MOVIE)
        object.__setattr__(plan, "created_at", _canonical_timestamp(created_at))
        object.__setattr__(plan, "source_root", source_root)
        object.__setattr__(plan, "output_root", output_root)
        object.__setattr__(
            plan, "candidate_snapshot_id", candidate_snapshot.snapshot_id
        )
        object.__setattr__(
            plan,
            "sources",
            tuple(PlanSource.from_record(item) for item in candidate_snapshot.records),
        )
        object.__setattr__(
            plan,
            "subtitle_variants",
            tuple(
                sorted(
                    subtitle_variants,
                    key=lambda item: item[0].ordinal,
                )
            ),
        )
        object.__setattr__(plan, "draft", draft)
        object.__setattr__(plan, "plan_hash", _plan_hash(plan.canonical_bytes()))
        return plan

    @classmethod
    def from_canonical_bytes(
        cls,
        content: bytes,
        *,
        plan_hash: str,
    ) -> MovieRenamePlan:
        if not verify_plan_bytes(content, plan_hash):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        payload = _decode(content)
        if (
            payload["schema_version"] != CURRENT_MOVIE_PLAN_SCHEMA_VERSION
            or payload["policy_version"] != CURRENT_MOVIE_PLAN_POLICY_VERSION
            or payload["work_type"] != TmdbWorkType.MOVIE.value
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        roots = check_fields(payload["roots"], _ROOTS_FIELDS, field="roots")
        candidates = _parse_sources(payload["sources"])
        mapping = MovieMappingDraft.from_dict(
            payload["mapping"],
            candidates=candidates.candidates,
        )
        movie = MovieIdentity.from_dict(payload["movie"])
        variants = _parse_variants(payload["subtitle_variants"])
        draft = compile_movie_plan_draft(
            movie=movie,
            mapping=mapping,
            candidates=candidates,
            subtitle_variants=variants,
        )
        created_at_text = _require_str(payload["created_at"], field="created_at")
        try:
            created_at = datetime.fromisoformat(
                created_at_text.replace("Z", "+00:00")
            )
        except ValueError:
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE) from None
        plan = cls.create(
            run_id=_require_str(payload["run_id"], field="run_id"),
            created_at=created_at,
            source_root=_parse_root(roots["source"], field="roots.source"),
            output_root=_parse_root(roots["output"], field="roots.output"),
            candidate_snapshot=candidates,
            subtitle_variants=variants,
            draft=draft,
            checked_destinations=_parse_destinations(payload["moves"]),
        )
        if plan.plan_hash != plan_hash or plan.canonical_bytes() != content:
            raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)
        return plan

    def canonical_bytes(self) -> bytes:
        payload = {
            "candidate_snapshot_id": self.candidate_snapshot_id,
            "created_at": self.created_at,
            "mapping": _mapping_payload(self.draft.mapping),
            "moves": [
                {
                    "destination": move.destination.as_posix(),
                    "destination_preflight": "absent",
                    "source_id": str(move.source_id),
                    "video_id": str(move.video_id),
                }
                for move in self.draft.moves
            ],
            "movie": _movie_payload(self.draft.movie),
            "policy_version": self.policy_version,
            "roots": {
                "output": _root_payload(self.output_root),
                "source": _root_payload(self.source_root),
            },
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "sources": [_source_payload(item) for item in self.sources],
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
            "work_type": self.work_type.value,
        }
        return json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

    @property
    def preview(self) -> PlanPreview:
        sources = {item.candidate_id: item for item in self.sources}
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
                    candidate_id=item,
                    source=sources[item].relative_path,
                )
                for item in self.draft.unmapped_candidate_ids
            ),
        )

    def verify_hash(self) -> bool:
        return verify_plan_bytes(self.canonical_bytes(), self.plan_hash)


def verify_movie_plan_bytes(content: bytes, plan_hash: str) -> bool:
    try:
        MovieRenamePlan.from_canonical_bytes(content, plan_hash=plan_hash)
    except (DomainError, ValueError):
        return False
    return True
