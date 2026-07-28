from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from reeloom.runtime.state import Phase


def _default_rules() -> Mapping[str, frozenset[Phase]]:
    return MappingProxyType(
        {
            "list_candidates": frozenset(
                {
                    Phase.IDENTIFY_SERIES,
                    Phase.MAP_EPISODES,
                    Phase.IDENTIFY_MOVIE,
                    Phase.MAP_MOVIE,
                }
            ),
            "search_tmdb": frozenset(
                {Phase.IDENTIFY_SERIES, Phase.IDENTIFY_MOVIE}
            ),
            "get_tmdb_series": frozenset(
                {Phase.IDENTIFY_SERIES, Phase.MAP_EPISODES}
            ),
            "get_tmdb_season": frozenset({Phase.MAP_EPISODES}),
            "select_series": frozenset({Phase.IDENTIFY_SERIES}),
            "get_tmdb_movie": frozenset(
                {Phase.IDENTIFY_MOVIE, Phase.MAP_MOVIE}
            ),
            "select_movie": frozenset({Phase.IDENTIFY_MOVIE}),
            "search_dir": frozenset(
                {Phase.MAP_EPISODES, Phase.MAP_MOVIE}
            ),
            "list_dir": frozenset(
                {Phase.MAP_EPISODES, Phase.MAP_MOVIE}
            ),
            "detect_subtitle_variant": frozenset(
                {Phase.MAP_EPISODES, Phase.MAP_MOVIE}
            ),
            "submit_mapping": frozenset(
                {Phase.MAP_EPISODES, Phase.MAP_MOVIE}
            ),
        }
    )


@dataclass(frozen=True, slots=True)
class PhaseToolPolicy:
    """A deny-by-default mapping from tools to valid domain phases."""

    rules: Mapping[str, frozenset[Phase]] = field(default_factory=_default_rules)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rules",
            MappingProxyType(
                {
                    tool_name: frozenset(phases)
                    for tool_name, phases in self.rules.items()
                }
            ),
        )

    def is_allowed(self, tool_name: str, phase: Phase) -> bool:
        return phase in self.rules.get(tool_name, frozenset())
