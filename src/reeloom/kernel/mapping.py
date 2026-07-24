from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from reeloom.kernel.candidates import (
    CandidateId,
    CandidateKind,
    CandidateSnapshot,
)
from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.schema import check_fields, require_object

_MAPPING_FIELDS = frozenset({"videos", "subtitles"})
_VIDEO_MAPPING_FIELDS = frozenset(
    {"video_id", "season", "episode_start", "episode_end"}
)
_SUBTITLE_MAPPING_FIELDS = frozenset({"subtitle_id", "video_id"})
MAX_SEASON_NUMBER = 999
MAX_EPISODE_NUMBER = 100_000
MAX_CATALOG_SEASONS = 100
MAX_MAPPED_EPISODES = 100_000


def _require_list(value: object, *, field: str) -> list[object]:
    if not isinstance(value, list):
        raise DomainError(
            ErrorCode.INVALID_FIELD_TYPE,
            context={"field": field, "expected": "list"},
        )
    return value


def _require_int(value: object, *, field: str) -> int:
    if type(value) is not int:
        raise DomainError(
            ErrorCode.INVALID_FIELD_TYPE,
            context={"field": field, "expected": "int"},
        )
    return value


@dataclass(frozen=True, slots=True)
class EpisodeSpan:
    """One video's inclusive destination episode range."""

    season: int
    episode_start: int
    episode_end: int

    def __post_init__(self) -> None:
        values = (self.season, self.episode_start, self.episode_end)
        if any(type(value) is not int for value in values):
            raise DomainError(ErrorCode.INVALID_EPISODE_RANGE)
        if (
            self.season < 0
            or self.season > MAX_SEASON_NUMBER
            or self.episode_start < 1
            or self.episode_start > MAX_EPISODE_NUMBER
            or self.episode_end < self.episode_start
            or self.episode_end > MAX_EPISODE_NUMBER
        ):
            raise DomainError(
                ErrorCode.INVALID_EPISODE_RANGE,
                context={
                    "season": self.season,
                    "episode_start": self.episode_start,
                    "episode_end": self.episode_end,
                },
            )

    @property
    def episodes(self) -> tuple[int, ...]:
        return tuple(range(self.episode_start, self.episode_end + 1))


@dataclass(frozen=True, slots=True)
class EpisodeCatalog:
    """Provider-neutral episode bounds used to validate an Agent mapping."""

    season_episode_counts: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.season_episode_counts, tuple):
            raise DomainError(ErrorCode.INVALID_EPISODE_CATALOG)
        if len(self.season_episode_counts) > MAX_CATALOG_SEASONS:
            raise DomainError(ErrorCode.INVALID_EPISODE_CATALOG)

        previous_season = -1
        for entry in self.season_episode_counts:
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or type(entry[0]) is not int
                or type(entry[1]) is not int
            ):
                raise DomainError(ErrorCode.INVALID_EPISODE_CATALOG)
            season, episode_count = entry
            if (
                season < 0
                or season > MAX_SEASON_NUMBER
                or episode_count < 1
                or episode_count > MAX_EPISODE_NUMBER
                or season <= previous_season
            ):
                raise DomainError(ErrorCode.INVALID_EPISODE_CATALOG)
            previous_season = season

    @classmethod
    def from_counts(cls, counts: Mapping[int, int]) -> EpisodeCatalog:
        entries = tuple(sorted(counts.items()))
        return cls(season_episode_counts=entries)

    def validate(self, span: EpisodeSpan) -> None:
        counts = dict(self.season_episode_counts)
        episode_count = counts.get(span.season)
        if episode_count is None:
            raise DomainError(
                ErrorCode.SEASON_OUT_OF_BOUNDS,
                context={"season": span.season},
            )
        if span.episode_start > episode_count or span.episode_end > episode_count:
            raise DomainError(
                ErrorCode.EPISODE_OUT_OF_BOUNDS,
                context={
                    "season": span.season,
                    "episode_start": span.episode_start,
                    "episode_end": span.episode_end,
                    "episode_count": episode_count,
                },
            )


@dataclass(frozen=True, slots=True)
class VideoMapping:
    video_id: CandidateId
    span: EpisodeSpan

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
        if not isinstance(self.span, EpisodeSpan):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "span", "expected": "EpisodeSpan"},
            )

    @classmethod
    def from_dict(cls, payload: object) -> VideoMapping:
        payload = check_fields(
            payload,
            _VIDEO_MAPPING_FIELDS,
            field="video_mapping",
        )
        return cls(
            video_id=CandidateId.parse(payload["video_id"]),
            span=EpisodeSpan(
                season=_require_int(payload["season"], field="season"),
                episode_start=_require_int(
                    payload["episode_start"],
                    field="episode_start",
                ),
                episode_end=_require_int(
                    payload["episode_end"],
                    field="episode_end",
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class SubtitleMapping:
    subtitle_id: CandidateId
    video_id: CandidateId

    def __post_init__(self) -> None:
        if not isinstance(self.subtitle_id, CandidateId):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "subtitle_id", "expected": "CandidateId"},
            )
        if not isinstance(self.video_id, CandidateId):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "video_id", "expected": "CandidateId"},
            )
        if self.subtitle_id.kind is not CandidateKind.SUBTITLE:
            raise DomainError(
                ErrorCode.CANDIDATE_KIND_MISMATCH,
                context={
                    "candidate_id": str(self.subtitle_id),
                    "expected_kind": CandidateKind.SUBTITLE.value,
                },
            )
        if self.video_id.kind is not CandidateKind.VIDEO:
            raise DomainError(
                ErrorCode.CANDIDATE_KIND_MISMATCH,
                context={
                    "candidate_id": str(self.video_id),
                    "expected_kind": CandidateKind.VIDEO.value,
                },
            )

    @classmethod
    def from_dict(cls, payload: object) -> SubtitleMapping:
        payload = check_fields(
            payload,
            _SUBTITLE_MAPPING_FIELDS,
            field="subtitle_mapping",
        )
        return cls(
            subtitle_id=CandidateId.parse(payload["subtitle_id"]),
            video_id=CandidateId.parse(payload["video_id"]),
        )


@dataclass(frozen=True, slots=True, init=False)
class MappingDraft:
    """A validated Agent proposal; construction always crosses domain policy."""

    videos: tuple[VideoMapping, ...]
    subtitles: tuple[SubtitleMapping, ...]

    @classmethod
    def from_dict(
        cls,
        payload: object,
        *,
        candidates: CandidateSnapshot,
        catalog: EpisodeCatalog,
    ) -> MappingDraft:
        payload = check_fields(payload, _MAPPING_FIELDS, field="mapping")
        raw_videos = _require_list(payload["videos"], field="videos")
        raw_subtitles = _require_list(payload["subtitles"], field="subtitles")

        videos = tuple(
            VideoMapping.from_dict(
                require_object(item, field=f"videos[{index}]")
            )
            for index, item in enumerate(raw_videos)
        )
        subtitles = tuple(
            SubtitleMapping.from_dict(
                require_object(item, field=f"subtitles[{index}]")
            )
            for index, item in enumerate(raw_subtitles)
        )
        return cls.create(
            videos,
            subtitles,
            candidates=candidates,
            catalog=catalog,
        )

    @classmethod
    def create(
        cls,
        videos: Iterable[VideoMapping],
        subtitles: Iterable[SubtitleMapping],
        *,
        candidates: CandidateSnapshot,
        catalog: EpisodeCatalog,
    ) -> MappingDraft:
        video_tuple = tuple(videos)
        subtitle_tuple = tuple(subtitles)
        cls._validate_videos(video_tuple, candidates=candidates, catalog=catalog)
        cls._validate_subtitles(
            subtitle_tuple,
            videos=video_tuple,
            candidates=candidates,
        )

        draft = object.__new__(cls)
        object.__setattr__(draft, "videos", video_tuple)
        object.__setattr__(draft, "subtitles", subtitle_tuple)
        return draft

    @staticmethod
    def _validate_videos(
        videos: tuple[VideoMapping, ...],
        *,
        candidates: CandidateSnapshot,
        catalog: EpisodeCatalog,
    ) -> None:
        candidate_ids = {candidate.id for candidate in candidates.candidates}
        seen_video_ids: set[CandidateId] = set()
        occupied_episodes: dict[tuple[int, int], CandidateId] = {}
        mapped_episode_count = sum(
            mapping.span.episode_end
            - mapping.span.episode_start
            + 1
            for mapping in videos
            if isinstance(mapping, VideoMapping)
        )
        if mapped_episode_count > MAX_MAPPED_EPISODES:
            raise DomainError(ErrorCode.INVALID_EPISODE_RANGE)

        for mapping in videos:
            if not isinstance(mapping, VideoMapping):
                raise DomainError(
                    ErrorCode.INVALID_FIELD_TYPE,
                    context={"field": "videos", "expected": "VideoMapping"},
                )
            if mapping.video_id in seen_video_ids:
                raise DomainError(
                    ErrorCode.DUPLICATE_VIDEO_MAPPING,
                    context={"video_id": str(mapping.video_id)},
                )
            seen_video_ids.add(mapping.video_id)

            if mapping.video_id not in candidate_ids:
                raise DomainError(
                    ErrorCode.UNKNOWN_CANDIDATE_ID,
                    context={"candidate_id": str(mapping.video_id)},
                )

            catalog.validate(mapping.span)
            for episode in mapping.span.episodes:
                location = (mapping.span.season, episode)
                previous_video_id = occupied_episodes.get(location)
                if previous_video_id is not None:
                    raise DomainError(
                        ErrorCode.EPISODE_RANGE_OVERLAP,
                        context={
                            "season": mapping.span.season,
                            "episode": episode,
                            "video_ids": (
                                str(previous_video_id),
                                str(mapping.video_id),
                            ),
                        },
                    )
                occupied_episodes[location] = mapping.video_id

    @staticmethod
    def _validate_subtitles(
        subtitles: tuple[SubtitleMapping, ...],
        *,
        videos: tuple[VideoMapping, ...],
        candidates: CandidateSnapshot,
    ) -> None:
        candidate_ids = {candidate.id for candidate in candidates.candidates}
        mapped_video_ids = {mapping.video_id for mapping in videos}
        seen_subtitle_ids: set[CandidateId] = set()

        for mapping in subtitles:
            if not isinstance(mapping, SubtitleMapping):
                raise DomainError(
                    ErrorCode.INVALID_FIELD_TYPE,
                    context={"field": "subtitles", "expected": "SubtitleMapping"},
                )
            if mapping.subtitle_id in seen_subtitle_ids:
                raise DomainError(
                    ErrorCode.DUPLICATE_SUBTITLE_MAPPING,
                    context={"subtitle_id": str(mapping.subtitle_id)},
                )
            seen_subtitle_ids.add(mapping.subtitle_id)

            if mapping.subtitle_id not in candidate_ids:
                raise DomainError(
                    ErrorCode.UNKNOWN_CANDIDATE_ID,
                    context={"candidate_id": str(mapping.subtitle_id)},
                )
            if mapping.video_id not in candidate_ids:
                raise DomainError(
                    ErrorCode.UNKNOWN_CANDIDATE_ID,
                    context={"candidate_id": str(mapping.video_id)},
                )
            if mapping.video_id not in mapped_video_ids:
                raise DomainError(
                    ErrorCode.SUBTITLE_VIDEO_NOT_MAPPED,
                    context={
                        "subtitle_id": str(mapping.subtitle_id),
                        "video_id": str(mapping.video_id),
                    },
                )
