"""Bounded domain tools exposed to the organizer agent."""

from reeloom.tools.candidates import (
    CandidatePage,
    CandidateSource,
    MAX_CURSOR,
    MAX_PAGE_SIZE,
    SnapshotCandidateSource,
    ToolExecutionError,
    ToolFailureCode,
    list_candidates,
)
from reeloom.tools.tmdb import (
    get_tmdb_season,
    get_tmdb_series,
    search_tmdb,
    select_series,
)
from reeloom.tools.mapping import (
    detect_subtitle_variant,
    get_existing_inventory,
    submit_mapping,
)

__all__ = [
    "CandidateSource",
    "CandidatePage",
    "MAX_CURSOR",
    "MAX_PAGE_SIZE",
    "SnapshotCandidateSource",
    "ToolExecutionError",
    "ToolFailureCode",
    "list_candidates",
    "get_tmdb_season",
    "get_tmdb_series",
    "search_tmdb",
    "select_series",
    "detect_subtitle_variant",
    "get_existing_inventory",
    "submit_mapping",
]
