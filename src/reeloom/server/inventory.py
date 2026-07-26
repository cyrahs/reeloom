from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from reeloom.adapters.filesystem import FilesystemScanner, ScanLimits
from reeloom.kernel.inventory import ExistingInventory
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import AuthorizedRoot

_SERIES_SUFFIX = re.compile(r"\{tmdb-(?P<tmdb_id>[1-9][0-9]{0,9})\}$")
_EPISODE = re.compile(
    r"(?:^|[ ._-])S(?P<season>[0-9]{2,3})"
    r"E(?P<start>[0-9]{2,5})(?:-E(?P<end>[0-9]{2,5}))?",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ArchiveInventoryProvider:
    """Return a bounded, path-free inventory from one fixed archive root."""

    root: AuthorizedRoot
    limits: ScanLimits = ScanLimits(
        max_candidates=10_000,
        max_entries=50_000,
        max_depth=8,
    )
    exclude_paths: frozenset[PurePosixPath] = frozenset()

    async def get_inventory(
        self,
        *,
        work_type: TmdbWorkType,
        tmdb_id: int,
    ) -> ExistingInventory:
        snapshot = FilesystemScanner(limits=self.limits).scan(self.root)
        occupied: set[tuple[int, int]] = set()
        for record in snapshot.snapshot.records:
            if record.relative_path in self.exclude_paths:
                continue
            parts = record.relative_path.parts
            if len(parts) < 2:
                continue
            series_match = _SERIES_SUFFIX.search(parts[0])
            if (
                series_match is None
                or int(series_match.group("tmdb_id")) != tmdb_id
            ):
                continue
            episode_match = _EPISODE.search(parts[-1])
            if episode_match is None:
                continue
            season = int(episode_match.group("season"))
            start = int(episode_match.group("start"))
            end = int(episode_match.group("end") or start)
            if end < start or end - start > 100:
                continue
            occupied.update((season, episode) for episode in range(start, end + 1))
        return ExistingInventory.from_episodes(
            work_type=work_type,
            tmdb_id=tmdb_id,
            occupied=tuple(occupied),
        )
