from __future__ import annotations

import re
from dataclasses import dataclass

from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.mapping import MappingDraft
from reeloom.kernel.tmdb import TmdbWorkType

MAX_INVENTORY_EPISODES = 2_000
MAX_INVENTORY_TMDB_ID = (1 << 31) - 1
_MAX_SEASON_NUMBER = 999
_MAX_EPISODE_NUMBER = 100_000
_EPISODE = re.compile(
    r"(?:^|[ ._/-])S(?P<season>[0-9]{2,3})"
    r"E(?P<start>[0-9]{2,5})(?:-E(?P<end>[0-9]{2,5}))?",
    re.IGNORECASE,
)


def parse_episode_filename(value: str) -> tuple[tuple[int, int], ...]:
    match = _EPISODE.search(value)
    if match is None:
        return ()
    season = int(match.group("season"))
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if (
        start < 1
        or end < start
        or end > _MAX_EPISODE_NUMBER
        or end - start > 100
    ):
        return ()
    return tuple((season, episode) for episode in range(start, end + 1))


@dataclass(frozen=True, slots=True, order=True)
class EpisodeLocation:
    season: int
    episode: int

    def __post_init__(self) -> None:
        if (
            type(self.season) is not int
            or self.season < 0
            or self.season > _MAX_SEASON_NUMBER
            or type(self.episode) is not int
            or self.episode < 1
            or self.episode > _MAX_EPISODE_NUMBER
        ):
            raise DomainError(ErrorCode.INVALID_EPISODE_RANGE)


@dataclass(frozen=True, slots=True)
class ExistingInventory:
    """A bounded, path-free snapshot of episodes already in an archive."""

    work_type: TmdbWorkType
    tmdb_id: int
    occupied: tuple[EpisodeLocation, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.work_type, TmdbWorkType)
            or type(self.tmdb_id) is not int
            or self.tmdb_id < 1
            or self.tmdb_id > MAX_INVENTORY_TMDB_ID
            or not isinstance(self.occupied, tuple)
            or len(self.occupied) > MAX_INVENTORY_EPISODES
            or any(
                not isinstance(location, EpisodeLocation)
                for location in self.occupied
            )
            or tuple(sorted(self.occupied)) != self.occupied
            or len(set(self.occupied)) != len(self.occupied)
        ):
            raise DomainError(ErrorCode.INVALID_EPISODE_CATALOG)

    @classmethod
    def from_episodes(
        cls,
        *,
        work_type: TmdbWorkType,
        tmdb_id: int,
        occupied: tuple[tuple[int, int], ...],
    ) -> ExistingInventory:
        locations = tuple(
            sorted(
                EpisodeLocation(season=season, episode=episode)
                for season, episode in occupied
            )
        )
        return cls(
            work_type=work_type,
            tmdb_id=tmdb_id,
            occupied=locations,
        )

    def validate(self, mapping: MappingDraft) -> None:
        if not isinstance(mapping, MappingDraft):
            raise DomainError(
                ErrorCode.INVALID_FIELD_TYPE,
                context={"field": "mapping", "expected": "MappingDraft"},
            )
        occupied = set(self.occupied)
        for video in mapping.videos:
            for episode in video.span.episodes:
                location = EpisodeLocation(
                    season=video.span.season,
                    episode=episode,
                )
                if location in occupied:
                    raise DomainError(
                        ErrorCode.INVENTORY_CONFLICT,
                        context={
                            "season": location.season,
                            "episode": location.episode,
                            "video_id": str(video.video_id),
                        },
                    )
