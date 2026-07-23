from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from reeloom.kernel.candidates import (
    CandidateId,
    CandidateKind,
    CandidateSnapshot,
)
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.mapping import EpisodeSpan, MappingDraft
from reeloom.kernel.naming import (
    SeriesIdentity,
    SubtitleVariant,
    subtitle_relative_path,
    video_relative_path,
)

CURRENT_PLAN_SCHEMA_VERSION = "1"
CURRENT_PLAN_POLICY_VERSION = "m0.5-v1"

_SEASON_COMPONENT_PATTERN = re.compile(r"^S[0-9]{2,}$")
_KIND_ORDER = {
    CandidateKind.VIDEO: 0,
    CandidateKind.SUBTITLE: 1,
}


def _candidate_sort_key(candidate_id: CandidateId) -> tuple[int, int]:
    return (_KIND_ORDER[candidate_id.kind], candidate_id.ordinal)


def _validate_destination(destination: PurePosixPath) -> None:
    if (
        not isinstance(destination, PurePosixPath)
        or destination.is_absolute()
        or len(destination.parts) != 3
        or destination.parts[0] in {"", ".", ".."}
        or _SEASON_COMPONENT_PATTERN.fullmatch(destination.parts[1]) is None
        or destination.parts[2] in {"", ".", ".."}
        or ".." in destination.parts
        or any(len(part.encode("utf-8")) > 255 for part in destination.parts)
    ):
        raise DomainError(ErrorCode.INVALID_DESTINATION)


def _destination_collision_key(
    destination: PurePosixPath,
) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFKC", part).casefold()
        for part in destination.parts
    )


@dataclass(frozen=True, slots=True, init=False)
class PlannedMove:
    """A source capability paired with a code-compiled relative destination."""

    source_id: CandidateId
    video_id: CandidateId
    series: SeriesIdentity
    span: EpisodeSpan
    destination: PurePosixPath

    @classmethod
    def for_video(
        cls,
        *,
        source_id: CandidateId,
        series: SeriesIdentity,
        span: EpisodeSpan,
        extension: object,
    ) -> PlannedMove:
        if (
            not isinstance(source_id, CandidateId)
            or source_id.kind is not CandidateKind.VIDEO
        ):
            raise DomainError(
                ErrorCode.CANDIDATE_KIND_MISMATCH,
                context={"expected_kind": CandidateKind.VIDEO.value},
            )
        return cls._from_compiled_destination(
            source_id=source_id,
            video_id=source_id,
            series=series,
            span=span,
            destination=video_relative_path(series, span, extension),
        )

    @classmethod
    def for_subtitle(
        cls,
        *,
        source_id: CandidateId,
        video_id: CandidateId,
        series: SeriesIdentity,
        span: EpisodeSpan,
        variant: SubtitleVariant,
        extension: object,
    ) -> PlannedMove:
        if (
            not isinstance(source_id, CandidateId)
            or source_id.kind is not CandidateKind.SUBTITLE
        ):
            raise DomainError(
                ErrorCode.CANDIDATE_KIND_MISMATCH,
                context={"expected_kind": CandidateKind.SUBTITLE.value},
            )
        if (
            not isinstance(video_id, CandidateId)
            or video_id.kind is not CandidateKind.VIDEO
        ):
            raise DomainError(
                ErrorCode.CANDIDATE_KIND_MISMATCH,
                context={"expected_kind": CandidateKind.VIDEO.value},
            )
        return cls._from_compiled_destination(
            source_id=source_id,
            video_id=video_id,
            series=series,
            span=span,
            destination=subtitle_relative_path(
                series,
                span,
                variant,
                extension,
            ),
        )

    @classmethod
    def _from_compiled_destination(
        cls,
        *,
        source_id: CandidateId,
        video_id: CandidateId,
        series: SeriesIdentity,
        span: EpisodeSpan,
        destination: PurePosixPath,
    ) -> PlannedMove:
        _validate_destination(destination)
        move = object.__new__(cls)
        object.__setattr__(move, "source_id", source_id)
        object.__setattr__(move, "video_id", video_id)
        object.__setattr__(move, "series", series)
        object.__setattr__(move, "span", span)
        object.__setattr__(move, "destination", destination)
        return move


@dataclass(frozen=True, slots=True, init=False)
class PlanDraft:
    """A mapping-bound immutable partition with code-compiled destinations."""

    schema_version: str
    policy_version: str
    series: SeriesIdentity
    mapping: MappingDraft
    moves: tuple[PlannedMove, ...]
    unmapped_candidate_ids: tuple[CandidateId, ...]

    @classmethod
    def create(
        cls,
        moves: Iterable[PlannedMove],
        *,
        series: SeriesIdentity,
        mapping: MappingDraft,
        candidates: CandidateSnapshot,
    ) -> PlanDraft:
        if not isinstance(series, SeriesIdentity):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "series", "expected": "SeriesIdentity"},
            )
        if not isinstance(mapping, MappingDraft):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "mapping", "expected": "MappingDraft"},
            )
        if not isinstance(candidates, CandidateSnapshot):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "candidates", "expected": "CandidateSnapshot"},
            )

        move_tuple = cls._disambiguate_subtitles(tuple(moves))
        unmapped_tuple = cls._validate(
            move_tuple,
            series=series,
            mapping=mapping,
            candidates=candidates,
        )

        plan = object.__new__(cls)
        object.__setattr__(plan, "schema_version", CURRENT_PLAN_SCHEMA_VERSION)
        object.__setattr__(
            plan,
            "policy_version",
            CURRENT_PLAN_POLICY_VERSION,
        )
        object.__setattr__(plan, "series", series)
        object.__setattr__(plan, "mapping", mapping)
        object.__setattr__(
            plan,
            "moves",
            tuple(
                sorted(
                    move_tuple,
                    key=lambda move: _candidate_sort_key(move.source_id),
                )
            ),
        )
        object.__setattr__(
            plan,
            "unmapped_candidate_ids",
            tuple(sorted(unmapped_tuple, key=_candidate_sort_key)),
        )
        return plan

    @staticmethod
    def _disambiguate_subtitles(
        moves: tuple[PlannedMove, ...],
    ) -> tuple[PlannedMove, ...]:
        groups: dict[PurePosixPath, list[PlannedMove]] = {}
        passthrough: list[PlannedMove] = []
        for move in moves:
            if not isinstance(move, PlannedMove):
                passthrough.append(move)
                continue
            groups.setdefault(move.destination, []).append(move)

        result = list(passthrough)
        for destination, group in groups.items():
            if (
                len(group) < 2
                or any(
                    move.source_id.kind is not CandidateKind.SUBTITLE
                    for move in group
                )
            ):
                result.extend(group)
                continue

            ordered_group = sorted(
                group,
                key=lambda move: _candidate_sort_key(move.source_id),
            )
            result.append(ordered_group[0])
            for disambiguator, move in enumerate(
                ordered_group[1:],
                start=1,
            ):
                suffix = destination.suffix
                disambiguated = destination.with_name(
                    f"{destination.stem}.{disambiguator}{suffix}"
                )
                result.append(
                    PlannedMove._from_compiled_destination(
                        source_id=move.source_id,
                        video_id=move.video_id,
                        series=move.series,
                        span=move.span,
                        destination=disambiguated,
                    )
                )
        return tuple(result)

    @staticmethod
    def _validate(
        moves: tuple[PlannedMove, ...],
        *,
        series: SeriesIdentity,
        mapping: MappingDraft,
        candidates: CandidateSnapshot,
    ) -> tuple[CandidateId, ...]:
        snapshot_ids = {candidate.id for candidate in candidates.candidates}
        video_mappings = {
            video.video_id: video
            for video in mapping.videos
        }
        subtitle_mappings = {
            subtitle.subtitle_id: subtitle
            for subtitle in mapping.subtitles
        }
        expected: dict[CandidateId, tuple[CandidateId, EpisodeSpan]] = {
            video_id: (video_id, video.span)
            for video_id, video in video_mappings.items()
        }
        for subtitle_id, subtitle in subtitle_mappings.items():
            expected[subtitle_id] = (
                subtitle.video_id,
                video_mappings[subtitle.video_id].span,
            )
        expected_ids = set(expected)

        unknown_mapping_ids = expected_ids - snapshot_ids
        if unknown_mapping_ids:
            candidate_id = min(unknown_mapping_ids, key=_candidate_sort_key)
            raise DomainError(
                ErrorCode.UNKNOWN_CANDIDATE_ID,
                context={"candidate_id": str(candidate_id)},
            )

        mapped_ids: set[CandidateId] = set()
        destinations: dict[tuple[str, ...], PlannedMove] = {}

        for move in moves:
            if not isinstance(move, PlannedMove):
                raise DomainError(
                    ErrorCode.INVALID_FIELD_TYPE,
                    context={"field": "moves", "expected": "PlannedMove"},
                )
            if move.source_id in mapped_ids:
                raise DomainError(
                    ErrorCode.DUPLICATE_PLAN_SOURCE,
                    context={"candidate_id": str(move.source_id)},
                )
            if move.source_id not in snapshot_ids:
                raise DomainError(
                    ErrorCode.UNKNOWN_CANDIDATE_ID,
                    context={"candidate_id": str(move.source_id)},
                )
            expected_move = expected.get(move.source_id)
            if expected_move is None:
                raise DomainError(
                    ErrorCode.PLAN_MAPPING_MISMATCH,
                    context={
                        "candidate_id": str(move.source_id),
                        "reason": "not_mapped",
                    },
                )
            expected_video_id, expected_span = expected_move
            if (
                move.video_id != expected_video_id
                or move.span != expected_span
                or move.series != series
            ):
                raise DomainError(
                    ErrorCode.PLAN_MAPPING_MISMATCH,
                    context={
                        "candidate_id": str(move.source_id),
                        "reason": "metadata",
                    },
                )
            mapped_ids.add(move.source_id)

            collision_key = _destination_collision_key(move.destination)
            previous_move = destinations.get(collision_key)
            if previous_move is not None:
                raise DomainError(
                    ErrorCode.DESTINATION_COLLISION,
                    context={
                        "destination": str(move.destination),
                        "candidate_ids": (
                            str(previous_move.source_id),
                            str(move.source_id),
                        ),
                    },
                )
            destinations[collision_key] = move

        missing_ids = expected_ids - mapped_ids
        if missing_ids:
            raise DomainError(
                ErrorCode.MISSING_PLAN_CANDIDATES,
                context={
                    "candidate_ids": tuple(
                        str(candidate_id)
                        for candidate_id in sorted(
                            missing_ids,
                            key=_candidate_sort_key,
                        )
                    )
                },
            )
        return tuple(snapshot_ids - expected_ids)
