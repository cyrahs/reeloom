from __future__ import annotations

from dataclasses import dataclass

from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.subtitle_acquisition import EmbeddedChineseStatus
from reeloom.runtime.state import RunState


@dataclass(frozen=True, slots=True)
class SubtitleWorkflowProjection:
    """Deterministic M13 progress derived only from replayable run state."""

    has_external_subtitles: bool
    catalog_seasons: frozenset[int]
    inspected_seasons: frozenset[int]
    uninspected_seasons: frozenset[int]
    present_seasons: frozenset[int]
    absent_seasons: frozenset[int]
    ambiguous_seasons: frozenset[int]
    completed_search_seasons: frozenset[int]
    candidate_seasons: frozenset[int]
    failed_search_seasons: frozenset[int]

    @property
    def all_catalog_seasons_inspected(self) -> bool:
        return bool(self.catalog_seasons) and not self.uninspected_seasons

    @property
    def searches_complete(self) -> bool:
        return bool(self.absent_seasons) and self.absent_seasons <= (
            self.completed_search_seasons | self.failed_search_seasons
        )

    @property
    def mapping_is_ready(self) -> bool:
        return (
            self.all_catalog_seasons_inspected
            and not self.absent_seasons
            and not self.ambiguous_seasons
        )

    @property
    def selection_is_ready(self) -> bool:
        return (
            self.all_catalog_seasons_inspected
            and not self.ambiguous_seasons
            and bool(self.absent_seasons)
            and self.absent_seasons <= self.completed_search_seasons
        )

    @property
    def attention_is_ready(self) -> bool:
        return self.all_catalog_seasons_inspected and (
            bool(self.ambiguous_seasons)
            or (
                self.searches_complete
                and bool(self.failed_search_seasons & self.absent_seasons)
            )
            or self.selection_is_ready
        )


def project_subtitle_workflow(state: RunState) -> SubtitleWorkflowProjection:
    candidate_ids = state.candidate_ids or ()
    catalog_seasons = frozenset(
        season_number for season_number, _episode_count in state.episode_catalog_counts
    )
    inspections = {
        item.season_number: item
        for item in state.embedded_subtitle_inspections
        if item.season_number in catalog_seasons
    }
    present = frozenset(
        season_number
        for season_number, item in inspections.items()
        if item.chinese_status is EmbeddedChineseStatus.PRESENT
    )
    absent = frozenset(
        season_number
        for season_number, item in inspections.items()
        if item.chinese_status is EmbeddedChineseStatus.ABSENT
    )
    ambiguous = frozenset(inspections) - present - absent
    completed: set[int] = set()
    for season_number in catalog_seasons:
        records = tuple(
            item
            for item in state.subtitle_search_records
            if item.season_number == season_number
        )
        if records and records[-1].page.complete and records[-1].page.next_cursor is None:
            completed.add(season_number)
    candidate_seasons = frozenset(
        season_number
        for season_number, _archive_set_id in state.subtitle_archive_search_bindings
        if season_number in catalog_seasons
    )
    failed_search_seasons = frozenset(
        season_number
        for season_number, _reason_code in state.subtitle_search_failures
        if season_number in catalog_seasons
    )
    return SubtitleWorkflowProjection(
        has_external_subtitles=any(
            item.kind is CandidateKind.SUBTITLE for item in candidate_ids
        ),
        catalog_seasons=catalog_seasons,
        inspected_seasons=frozenset(inspections),
        uninspected_seasons=catalog_seasons - frozenset(inspections),
        present_seasons=present,
        absent_seasons=absent,
        ambiguous_seasons=ambiguous,
        completed_search_seasons=frozenset(completed),
        candidate_seasons=candidate_seasons,
        failed_search_seasons=failed_search_seasons,
    )
