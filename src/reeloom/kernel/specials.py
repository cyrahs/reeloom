from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from reeloom.kernel.candidates import (
    CandidateId,
    CandidateKind,
    CandidateSnapshot,
)
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.mapping import EpisodeCatalog, EpisodeSpan, VideoMapping
from reeloom.kernel.schema import check_fields

_LOCAL_SPECIAL_FIELDS = frozenset({"video_id", "kind"})
_TMDB_EPISODE_HINT_FIELDS = frozenset(
    {"season_number", "episode_number", "kind"}
)


class SpecialKind(StrEnum):
    OVA = "ova"
    OAD = "oad"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: object) -> SpecialKind:
        if not isinstance(value, str):
            raise DomainError(ErrorCode.INVALID_SPECIAL_KIND)
        try:
            return cls(value)
        except ValueError as error:
            raise DomainError(ErrorCode.INVALID_SPECIAL_KIND) from error


@dataclass(frozen=True, slots=True)
class LocalSpecial:
    """A local S00 candidate ordered by its scanner-issued opaque ID."""

    video_id: CandidateId
    kind: SpecialKind

    def __post_init__(self) -> None:
        if not isinstance(self.video_id, CandidateId):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "video_id", "expected": "CandidateId"},
            )
        if self.video_id.kind is not CandidateKind.VIDEO:
            raise DomainError(
                ErrorCode.CANDIDATE_KIND_MISMATCH,
                context={
                    "candidate_id": str(self.video_id),
                    "expected_kind": CandidateKind.VIDEO.value,
                },
            )
        if not isinstance(self.kind, SpecialKind):
            raise DomainError(ErrorCode.INVALID_SPECIAL_KIND)

    @property
    def order(self) -> int:
        return self.video_id.ordinal

    @classmethod
    def from_dict(cls, payload: object) -> LocalSpecial:
        payload = check_fields(payload, _LOCAL_SPECIAL_FIELDS, field="local_special")
        return cls(
            video_id=CandidateId.parse(payload["video_id"]),
            kind=SpecialKind.parse(payload["kind"]),
        )


@dataclass(frozen=True, slots=True, order=True)
class EpisodeRef:
    """A canonical TMDB episode coordinate."""

    season_number: int
    episode_number: int

    def __post_init__(self) -> None:
        if (
            type(self.season_number) is not int
            or self.season_number < 0
            or type(self.episode_number) is not int
            or self.episode_number < 1
        ):
            raise DomainError(ErrorCode.INVALID_SPECIAL_EPISODE)

    @property
    def span(self) -> EpisodeSpan:
        return EpisodeSpan(
            season=self.season_number,
            episode_start=self.episode_number,
            episode_end=self.episode_number,
        )


@dataclass(frozen=True, slots=True)
class TmdbEpisodeHint:
    """A minimal TMDB episode hint that may point outside Specials season."""

    target: EpisodeRef
    kind: SpecialKind

    def __post_init__(self) -> None:
        if not isinstance(self.target, EpisodeRef):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "target", "expected": "EpisodeRef"},
            )
        if not isinstance(self.kind, SpecialKind):
            raise DomainError(ErrorCode.INVALID_SPECIAL_KIND)

    @classmethod
    def from_dict(cls, payload: object) -> TmdbEpisodeHint:
        payload = check_fields(
            payload,
            _TMDB_EPISODE_HINT_FIELDS,
            field="tmdb_episode_hint",
        )
        return cls(
            target=EpisodeRef(
                season_number=payload["season_number"],  # type: ignore[arg-type]
                episode_number=payload["episode_number"],  # type: ignore[arg-type]
            ),
            kind=SpecialKind.parse(payload["kind"]),
        )

    @property
    def season_number(self) -> int:
        return self.target.season_number

    @property
    def episode_number(self) -> int:
        return self.target.episode_number


@dataclass(frozen=True, slots=True)
class SpecialResolution:
    assignments: tuple[VideoMapping, ...]
    unmapped_video_ids: tuple[CandidateId, ...]
    unused_targets: tuple[EpisodeRef, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.assignments, tuple)
            or not isinstance(self.unmapped_video_ids, tuple)
            or not isinstance(self.unused_targets, tuple)
        ):
            raise DomainError(ErrorCode.INVALID_FIELD_TYPE)


def _validate_inputs(
    local_specials: tuple[LocalSpecial, ...],
    tmdb_episode_hints: tuple[TmdbEpisodeHint, ...],
    *,
    candidates: CandidateSnapshot,
    catalog: EpisodeCatalog,
) -> None:
    if not isinstance(candidates, CandidateSnapshot):
        raise DomainError(
            ErrorCode.INVALID_FIELD_TYPE,
            context={"field": "candidates", "expected": "CandidateSnapshot"},
        )
    if not isinstance(catalog, EpisodeCatalog):
        raise DomainError(
            ErrorCode.INVALID_FIELD_TYPE,
            context={"field": "catalog", "expected": "EpisodeCatalog"},
        )

    candidate_ids = {candidate.id for candidate in candidates.candidates}
    seen_video_ids: set[CandidateId] = set()
    for special in local_specials:
        if not isinstance(special, LocalSpecial):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "local_specials", "expected": "LocalSpecial"},
            )
        if special.video_id in seen_video_ids:
            raise DomainError(
                ErrorCode.DUPLICATE_SPECIAL_VIDEO,
                context={"video_id": str(special.video_id)},
            )
        seen_video_ids.add(special.video_id)
        if special.video_id not in candidate_ids:
            raise DomainError(
                ErrorCode.UNKNOWN_CANDIDATE_ID,
                context={"candidate_id": str(special.video_id)},
            )

    seen_targets: set[EpisodeRef] = set()
    for hint in tmdb_episode_hints:
        if not isinstance(hint, TmdbEpisodeHint):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={
                    "field": "tmdb_episode_hints",
                    "expected": "TmdbEpisodeHint",
                },
            )
        if hint.target in seen_targets:
            raise DomainError(
                ErrorCode.DUPLICATE_SPECIAL_EPISODE,
                context={
                    "season_number": hint.season_number,
                    "episode_number": hint.episode_number,
                },
            )
        seen_targets.add(hint.target)
        catalog.validate(hint.target.span)


def resolve_specials(
    local_specials: Iterable[LocalSpecial],
    tmdb_episode_hints: Iterable[TmdbEpisodeHint],
    *,
    candidates: CandidateSnapshot,
    catalog: EpisodeCatalog,
) -> SpecialResolution:
    """Resolve typed hints across seasons, then fallback only within S00."""

    local_tuple = tuple(local_specials)
    tmdb_tuple = tuple(tmdb_episode_hints)
    _validate_inputs(
        local_tuple,
        tmdb_tuple,
        candidates=candidates,
        catalog=catalog,
    )

    ordered_locals = tuple(sorted(local_tuple, key=lambda item: item.order))
    ordered_tmdb = tuple(sorted(tmdb_tuple, key=lambda item: item.target))
    assignments: dict[CandidateId, EpisodeRef] = {}
    used_targets: set[EpisodeRef] = set()

    for kind in (SpecialKind.OVA, SpecialKind.OAD):
        hinted_locals = tuple(item for item in ordered_locals if item.kind is kind)
        hinted_tmdb = tuple(item for item in ordered_tmdb if item.kind is kind)
        if len(hinted_locals) > len(hinted_tmdb):
            raise DomainError(
                ErrorCode.SPECIAL_EVIDENCE_CONFLICT,
                context={
                    "kind": kind.value,
                    "local_count": len(hinted_locals),
                    "tmdb_count": len(hinted_tmdb),
                },
            )
        for local_item, tmdb_item in zip(
            hinted_locals,
            hinted_tmdb,
            strict=False,
        ):
            assignments[local_item.video_id] = tmdb_item.target
            used_targets.add(tmdb_item.target)

    remaining_locals = tuple(
        item for item in ordered_locals if item.video_id not in assignments
    )
    remaining_tmdb = tuple(
        item
        for item in ordered_tmdb
        if item.season_number == 0 and item.target not in used_targets
    )
    for local_item, tmdb_item in zip(
        remaining_locals,
        remaining_tmdb,
        strict=False,
    ):
        assignments[local_item.video_id] = tmdb_item.target
        used_targets.add(tmdb_item.target)

    assignment_items = tuple(
        VideoMapping(
            video_id=local_item.video_id,
            span=assignments[local_item.video_id].span,
        )
        for local_item in ordered_locals
        if local_item.video_id in assignments
    )
    unmapped_video_ids = tuple(
        local_item.video_id
        for local_item in ordered_locals
        if local_item.video_id not in assignments
    )
    unused_targets = tuple(
        tmdb_item.target
        for tmdb_item in ordered_tmdb
        if tmdb_item.target not in used_targets
    )
    return SpecialResolution(
        assignments=assignment_items,
        unmapped_video_ids=unmapped_video_ids,
        unused_targets=unused_targets,
    )
