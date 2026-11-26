"""Release titles: what a subscription may grab.

Pure functions. A release title is untrusted text from an indexer; it is only
ever matched against patterns here and, sanitized, used as the name of the
folder the download lands in. Nothing here decides where a file ends up in
the library — that stays with the Agent's episode mapping.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from reeloom.models import ReeloomError

MAX_FOLDER_BYTES = 180
_MAX_EPISODE_RANGE = 200
RESERVED_NAMES = frozenset({"archive", "fail", "in_progress"})

# "S01E04", "S01 E01-E02", "S01E11-E12", "S01E01-02", "S01EP05"
_EPISODES = re.compile(
    r"(?<![A-Za-z0-9])S(\d{1,2})\s?EP?(\d{1,4})"
    r"(?:\s?-\s?(?:S\d{1,2})?EP?(\d{1,4})|\s?-\s?(\d{1,4})(?![0-9pPiI]))?"
    r"(?![0-9])",
    re.IGNORECASE,
)
# A bare season: "S01", "Season 1" — a pack unless an episode follows.
_SEASON = re.compile(
    r"(?<![A-Za-z0-9])(?:S(\d{1,2})|Season\s?(\d{1,2}))(?![0-9])(?!\s?EP?\d)",
    re.IGNORECASE,
)
_UNSAFE = re.compile(r"[\x00-\x1f/\\]")


class ReleaseError(ReeloomError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseEpisodes:
    """Which part of a season a release title claims to hold.

    ``episodes`` is None for a season pack (or a title that names a season
    but no episode): the whole season, as far as dedupe is concerned.
    ``season`` is None when the title names no season at all.
    """

    season: int | None
    episodes: tuple[int, ...] | None

    def to_json(self) -> dict[str, object]:
        return {
            "season": self.season,
            "episodes": list(self.episodes) if self.episodes is not None else None,
        }


def parse_episodes(title: str) -> ReleaseEpisodes:
    match = _EPISODES.search(title)
    if match:
        season = int(match.group(1))
        start = int(match.group(2))
        end_text = match.group(3) or match.group(4)
        end = int(end_text) if end_text else start
        if end < start or end - start > _MAX_EPISODE_RANGE:
            end = start
        return ReleaseEpisodes(season, tuple(range(start, end + 1)))
    season_match = _SEASON.search(title)
    if season_match:
        return ReleaseEpisodes(
            int(season_match.group(1) or season_match.group(2)), None
        )
    return ReleaseEpisodes(None, None)


def matches_group(title: str, groups: Sequence[str]) -> bool:
    """True when the release is by one of ``groups`` (any when empty).

    Scene-style titles end in ``-GROUP`` (sometimes ``@GROUP``); anime
    releases lead with ``[GROUP]``. Matching is case-insensitive and on the
    whole token, so ``WEB`` does not match ``HHWEB``.
    """

    if not groups:
        return True
    stripped = title.strip()
    for group in groups:
        token = re.escape(group.strip())
        if not token:
            continue
        if re.search(rf"[-@]{token}$", stripped, re.IGNORECASE):
            return True
        if re.match(rf"^\[{token}\]", stripped, re.IGNORECASE):
            return True
    return False


def compile_pattern(pattern: str) -> re.Pattern[str] | None:
    """Compile a user filter, case-insensitive; empty means no filter."""

    if not pattern.strip():
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as error:
        raise ReleaseError("invalid_pattern", pattern=pattern, detail=str(error))


@dataclass(frozen=True, slots=True)
class Filter:
    groups: tuple[str, ...] = ()
    include: str = ""
    exclude: str = ""
    season: int | None = None

    def rejects(self, title: str, parsed: ReleaseEpisodes) -> str | None:
        """Why the release is not wanted, or None if it passes."""

        if not matches_group(title, self.groups):
            return "group"
        include = compile_pattern(self.include)
        if include is not None and not include.search(title):
            return "include"
        exclude = compile_pattern(self.exclude)
        if exclude is not None and exclude.search(title):
            return "exclude"
        if self.season is not None and parsed.season != self.season:
            return "season"
        return None


def is_covered(
    parsed: ReleaseEpisodes, covered: Iterable[tuple[int, int | None]]
) -> bool:
    """Whether everything the release holds was already grabbed.

    ``covered`` holds ``(season, episode)`` pairs, with ``episode`` None for
    a grabbed season pack. A pack is only wanted while nothing of its season
    is covered; a title with no recognizable season is never deduped (the
    guid still keeps it from being grabbed twice).
    """

    pairs = set(covered)
    if parsed.season is None:
        return False
    if (parsed.season, None) in pairs:
        return True
    if parsed.episodes is None:
        return any(season == parsed.season for season, _ in pairs)
    return all((parsed.season, episode) in pairs for episode in parsed.episodes)


def coverage(parsed: ReleaseEpisodes) -> list[tuple[int, int | None]]:
    if parsed.season is None:
        return []
    if parsed.episodes is None:
        return [(parsed.season, None)]
    return [(parsed.season, episode) for episode in parsed.episodes]


def folder_for(title: str, taken: set[str]) -> str:
    """A safe, unused folder name for a release, derived from its title."""

    base = _UNSAFE.sub(" ", title).strip().strip(".").strip()
    base = re.sub(r"\s+", " ", base)
    encoded = base.encode("utf-8")[:MAX_FOLDER_BYTES]
    base = encoded.decode("utf-8", "ignore").strip() or "release"
    if base.startswith(".") or base.lower() in RESERVED_NAMES:
        base = f"release {base.lstrip('.')}".strip()
    name = base
    number = 2
    while name in taken:
        name = f"{base} #{number}"
        number += 1
    return name
