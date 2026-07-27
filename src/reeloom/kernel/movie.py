from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from reeloom.kernel.candidates import (
    CandidateId,
    CandidateKind,
    CandidateSnapshot,
)
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.naming import (
    MovieIdentity,
    SubtitleVariant,
    movie_subtitle_relative_path,
    movie_video_relative_path,
)
from reeloom.kernel.schema import check_fields
from reeloom.kernel.scanner import ScannedCandidateSnapshot

CURRENT_MOVIE_DRAFT_SCHEMA_VERSION = "1"
CURRENT_MOVIE_DRAFT_POLICY_VERSION = "m10-v1"
_MAPPING_FIELDS = frozenset({"subtitle_ids", "video_id"})


def _sort_key(candidate_id: CandidateId) -> tuple[int, int]:
    return (
        0 if candidate_id.kind is CandidateKind.VIDEO else 1,
        candidate_id.ordinal,
    )


@dataclass(frozen=True, slots=True, init=False)
class MovieMappingDraft:
    video_id: CandidateId
    subtitle_ids: tuple[CandidateId, ...]

    @classmethod
    def from_dict(
        cls,
        payload: object,
        *,
        candidates: CandidateSnapshot,
    ) -> MovieMappingDraft:
        payload = check_fields(payload, _MAPPING_FIELDS, field="movie_mapping")
        raw_subtitles = payload["subtitle_ids"]
        if not isinstance(raw_subtitles, list):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
        return cls.create(
            video_id=CandidateId.parse(payload["video_id"]),
            subtitle_ids=tuple(
                CandidateId.parse(value) for value in raw_subtitles
            ),
            candidates=candidates,
        )

    @classmethod
    def create(
        cls,
        *,
        video_id: CandidateId,
        subtitle_ids: tuple[CandidateId, ...],
        candidates: CandidateSnapshot,
    ) -> MovieMappingDraft:
        if (
            not isinstance(video_id, CandidateId)
            or video_id.kind is not CandidateKind.VIDEO
            or not isinstance(subtitle_ids, tuple)
            or not isinstance(candidates, CandidateSnapshot)
        ):
            raise DomainError(ErrorCode.CANDIDATE_KIND_MISMATCH)
        if any(
            not isinstance(item, CandidateId)
            or item.kind is not CandidateKind.SUBTITLE
            for item in subtitle_ids
        ):
            raise DomainError(ErrorCode.CANDIDATE_KIND_MISMATCH)
        if len(set(subtitle_ids)) != len(subtitle_ids):
            raise DomainError(ErrorCode.DUPLICATE_SUBTITLE_MAPPING)
        candidate_ids = {item.id for item in candidates.candidates}
        unknown = {video_id, *subtitle_ids} - candidate_ids
        if unknown:
            raise DomainError(
                ErrorCode.UNKNOWN_CANDIDATE_ID,
                context={
                    "candidate_id": str(min(unknown, key=_sort_key))
                },
            )
        draft = object.__new__(cls)
        object.__setattr__(draft, "video_id", video_id)
        object.__setattr__(
            draft,
            "subtitle_ids",
            tuple(sorted(subtitle_ids, key=_sort_key)),
        )
        return draft


@dataclass(frozen=True, slots=True)
class MoviePlannedMove:
    source_id: CandidateId
    video_id: CandidateId
    destination: PurePosixPath


@dataclass(frozen=True, slots=True)
class MoviePlanDraft:
    schema_version: str
    policy_version: str
    movie: MovieIdentity
    mapping: MovieMappingDraft
    moves: tuple[MoviePlannedMove, ...]
    unmapped_candidate_ids: tuple[CandidateId, ...]


def _collision_key(path: PurePosixPath) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFKC", part).casefold()
        for part in path.parts
    )


def _validate_destination(path: PurePosixPath) -> None:
    if (
        not isinstance(path, PurePosixPath)
        or path.is_absolute()
        or len(path.parts) != 2
        or ".." in path.parts
        or any(
            part in {"", ".", ".."}
            or part.casefold().startswith(".env")
            or len(part.encode("utf-8")) > 255
            for part in path.parts
        )
    ):
        raise DomainError(ErrorCode.INVALID_DESTINATION)


def compile_movie_plan_draft(
    *,
    movie: MovieIdentity,
    mapping: MovieMappingDraft,
    candidates: ScannedCandidateSnapshot,
    subtitle_variants: tuple[
        tuple[CandidateId, SubtitleVariant], ...
    ],
) -> MoviePlanDraft:
    if (
        not isinstance(movie, MovieIdentity)
        or not isinstance(mapping, MovieMappingDraft)
        or not isinstance(candidates, ScannedCandidateSnapshot)
    ):
        raise DomainError(ErrorCode.INVALID_FIELD_TYPE)
    variants = dict(subtitle_variants)
    if (
        len(variants) != len(subtitle_variants)
        or set(variants) != set(mapping.subtitle_ids)
        or any(
            candidate_id.kind is not CandidateKind.SUBTITLE
            or not isinstance(variant, SubtitleVariant)
            for candidate_id, variant in subtitle_variants
        )
    ):
        raise DomainError(ErrorCode.SUBTITLE_VARIANT_REQUIRED)

    moves = [
        MoviePlannedMove(
            source_id=mapping.video_id,
            video_id=mapping.video_id,
            destination=movie_video_relative_path(
                movie,
                candidates.record_for(mapping.video_id).relative_path.suffix,
            ),
        )
    ]
    subtitle_moves = [
        MoviePlannedMove(
            source_id=subtitle_id,
            video_id=mapping.video_id,
            destination=movie_subtitle_relative_path(
                movie,
                variants[subtitle_id],
                candidates.record_for(subtitle_id).relative_path.suffix,
            ),
        )
        for subtitle_id in mapping.subtitle_ids
    ]
    grouped: dict[PurePosixPath, list[MoviePlannedMove]] = {}
    for move in subtitle_moves:
        grouped.setdefault(move.destination, []).append(move)
    for destination, group in grouped.items():
        ordered = sorted(group, key=lambda item: _sort_key(item.source_id))
        if len(ordered) == 1:
            moves.extend(ordered)
            continue
        suffix = destination.suffix
        for index, move in enumerate(ordered, start=1):
            moves.append(
                MoviePlannedMove(
                    source_id=move.source_id,
                    video_id=move.video_id,
                    destination=destination.with_name(
                        f"{destination.stem}.{index}{suffix}"
                    ),
                )
            )

    moves = sorted(moves, key=lambda item: _sort_key(item.source_id))
    for move in moves:
        _validate_destination(move.destination)
    collision_keys = {_collision_key(item.destination) for item in moves}
    if len(collision_keys) != len(moves):
        raise DomainError(ErrorCode.DESTINATION_COLLISION)
    mapped = {mapping.video_id, *mapping.subtitle_ids}
    all_ids = {item.candidate.id for item in candidates.records}
    return MoviePlanDraft(
        schema_version=CURRENT_MOVIE_DRAFT_SCHEMA_VERSION,
        policy_version=CURRENT_MOVIE_DRAFT_POLICY_VERSION,
        movie=movie,
        mapping=mapping,
        moves=tuple(moves),
        unmapped_candidate_ids=tuple(
            sorted(all_ids - mapped, key=_sort_key)
        ),
    )
