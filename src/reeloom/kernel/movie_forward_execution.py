from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from reeloom.kernel.candidates import CandidateId
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.movie import (
    MovieMappingDraft,
    MoviePlanDraft,
    compile_movie_plan_draft_v2,
)
from reeloom.kernel.naming import MovieIdentity, SubtitleVariant
from reeloom.kernel.rename_plan import (
    PlanPreview,
    PlanPreviewMove,
    PlanPreviewUnmapped,
    _canonical_timestamp,
    _plan_hash,
    _reject_duplicate_keys,
    verify_plan_bytes,
)
from reeloom.kernel.semantic_identity import (
    SemanticCandidateSnapshot,
    SemanticRootBinding,
    SemanticSourceIdentity,
)
from reeloom.kernel.tmdb import TmdbWorkType

CURRENT_MOVIE_FORWARD_PLAN_SCHEMA_VERSION = "2"
CURRENT_MOVIE_FORWARD_PLAN_POLICY_VERSION = "m14-v1"
_MAX_BYTES = 8 * 1024 * 1024
_FIELDS = frozenset(
    {
        "candidate_snapshot_id",
        "config_revision",
        "created_at",
        "draft_policy_version",
        "draft_schema_version",
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
        "watch_id",
        "work_type",
    }
)


def _mapping_payload(mapping: MovieMappingDraft) -> dict[str, object]:
    return {
        "subtitle_ids": [str(item) for item in mapping.subtitle_ids],
        "video_id": str(mapping.video_id),
    }


def _movie_payload(movie: MovieIdentity) -> dict[str, object]:
    return {
        "release_year": movie.release_year,
        "title_zh_cn": movie.title_zh_cn,
        "tmdb_id": movie.tmdb_id,
    }


@dataclass(frozen=True, slots=True, init=False)
class MovieRenamePlanV2:
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
    draft: MoviePlanDraft
    plan_hash: str

    @property
    def candidate_snapshot_id(self) -> str:
        return self.candidate_snapshot.snapshot_id

    @property
    def sources(self) -> tuple[SemanticSourceIdentity, ...]:
        return self.candidate_snapshot.sources

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        config_revision: int,
        watch_id: str,
        created_at: datetime,
        source_root: SemanticRootBinding,
        output_root: SemanticRootBinding,
        candidate_snapshot: SemanticCandidateSnapshot,
        subtitle_variants: tuple[tuple[CandidateId, SubtitleVariant], ...],
        draft: MoviePlanDraft,
    ) -> MovieRenamePlanV2:
        if (
            not run_id
            or type(config_revision) is not int
            or config_revision < 1
            or not watch_id
            or not isinstance(source_root, SemanticRootBinding)
            or not isinstance(output_root, SemanticRootBinding)
            or not isinstance(candidate_snapshot, SemanticCandidateSnapshot)
            or not isinstance(draft, MoviePlanDraft)
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        variants = tuple(
            sorted(subtitle_variants, key=lambda item: item[0].ordinal)
        )
        expected = compile_movie_plan_draft_v2(
            movie=draft.movie,
            mapping=draft.mapping,
            candidates=candidate_snapshot,
            subtitle_variants=variants,
        )
        if expected != draft:
            raise DomainError(ErrorCode.PLAN_MAPPING_MISMATCH)
        plan = object.__new__(cls)
        object.__setattr__(
            plan, "schema_version", CURRENT_MOVIE_FORWARD_PLAN_SCHEMA_VERSION
        )
        object.__setattr__(
            plan, "policy_version", CURRENT_MOVIE_FORWARD_PLAN_POLICY_VERSION
        )
        object.__setattr__(plan, "run_id", run_id)
        object.__setattr__(plan, "config_revision", config_revision)
        object.__setattr__(plan, "watch_id", watch_id)
        object.__setattr__(plan, "work_type", TmdbWorkType.MOVIE)
        object.__setattr__(plan, "created_at", _canonical_timestamp(created_at))
        object.__setattr__(plan, "source_root", source_root)
        object.__setattr__(plan, "output_root", output_root)
        object.__setattr__(plan, "candidate_snapshot", candidate_snapshot)
        object.__setattr__(plan, "subtitle_variants", variants)
        object.__setattr__(plan, "draft", draft)
        object.__setattr__(plan, "plan_hash", _plan_hash(plan.canonical_bytes()))
        return plan

    @classmethod
    def from_canonical_bytes(
        cls, content: bytes, *, plan_hash: str
    ) -> MovieRenamePlanV2:
        if (
            not isinstance(content, bytes)
            or not 0 < len(content) <= _MAX_BYTES
            or not verify_plan_bytes(content, plan_hash)
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        try:
            raw = json.loads(content, object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE) from None
        if not isinstance(raw, dict) or set(raw) != _FIELDS:
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        if (
            raw["schema_version"] != CURRENT_MOVIE_FORWARD_PLAN_SCHEMA_VERSION
            or raw["policy_version"]
            != CURRENT_MOVIE_FORWARD_PLAN_POLICY_VERSION
            or raw["work_type"] != TmdbWorkType.MOVIE.value
            or not isinstance(raw["roots"], dict)
            or set(raw["roots"]) != {"source", "output"}
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        snapshot = SemanticCandidateSnapshot.from_payload(
            raw["sources"], snapshot_id=raw["candidate_snapshot_id"]
        )
        try:
            mapping = MovieMappingDraft.from_dict(
                raw["mapping"], candidates=snapshot.candidates
            )
            movie = MovieIdentity.from_dict(raw["movie"])
            variants = tuple(
                (
                    CandidateId.parse(item["subtitle_id"]),
                    SubtitleVariant(item["variant"]),
                )
                for item in raw["subtitle_variants"]
            )
            created_at = datetime.fromisoformat(
                str(raw["created_at"]).replace("Z", "+00:00")
            )
            draft = compile_movie_plan_draft_v2(
                movie=movie,
                mapping=mapping,
                candidates=snapshot,
                subtitle_variants=variants,
            )
            restored = cls.create(
                run_id=raw["run_id"],
                config_revision=raw["config_revision"],
                watch_id=raw["watch_id"],
                created_at=created_at,
                source_root=SemanticRootBinding.from_payload(
                    raw["roots"]["source"]
                ),
                output_root=SemanticRootBinding.from_payload(
                    raw["roots"]["output"]
                ),
                candidate_snapshot=snapshot,
                subtitle_variants=variants,
                draft=draft,
            )
        except (KeyError, TypeError, ValueError):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE) from None
        if restored.canonical_bytes() != content or restored.plan_hash != plan_hash:
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
            "moves": [
                {
                    "destination": item.destination.as_posix(),
                    "source_id": str(item.source_id),
                    "video_id": str(item.video_id),
                }
                for item in self.draft.moves
            ],
            "movie": _movie_payload(self.draft.movie),
            "policy_version": self.policy_version,
            "roots": {
                "output": self.output_root.payload(),
                "source": self.source_root.payload(),
            },
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "sources": self.candidate_snapshot.payload(),
            "subtitle_variants": [
                {"subtitle_id": str(item), "variant": variant.value}
                for item, variant in self.subtitle_variants
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
        ).encode("ascii")

    @property
    def preview(self) -> PlanPreview:
        sources = {item.candidate_id: item for item in self.sources}
        return PlanPreview(
            plan_hash=self.plan_hash,
            moves=tuple(
                PlanPreviewMove(
                    candidate_id=item.source_id,
                    source=sources[item.source_id].relative_path,
                    destination=item.destination,
                )
                for item in self.draft.moves
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
