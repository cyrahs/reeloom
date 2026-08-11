from __future__ import annotations

import pytest

from reeloom.models import (
    EpisodeSpan,
    FileKind,
    MediaIdentity,
    MediaType,
    MoveKind,
    PlanError,
    Root,
    SnapshotFile,
    SubtitleVariant,
)
from reeloom.naming import ExistingFolder
from reeloom.planner import MappingEntry, compile_plan

SERIES = MediaIdentity(MediaType.ANIME, 123, "Show", 2024)
MOVIE = MediaIdentity(MediaType.MOVIE, 456, "Feature", 2016)

SNAPSHOT = (
    SnapshotFile("V1", "ep01.mkv", FileKind.VIDEO, 100),
    SnapshotFile("V2", "ep02.mkv", FileKind.VIDEO, 100),
    SnapshotFile("S1", "ep01.srt", FileKind.SUBTITLE, 10, SubtitleVariant.CHS),
    SnapshotFile("O1", "readme.txt", FileKind.OTHER, 1),
)


def plan(entries, *, snapshot=SNAPSHOT, identity=SERIES, **kwargs):
    return compile_plan(
        identity, snapshot, tuple(entries), folder_name="Drop", **kwargs
    )


def test_mapping_compiles_to_library_destinations() -> None:
    result = plan(
        [
            MappingEntry("V1", EpisodeSpan(1, 1, 1)),
            MappingEntry("S1", EpisodeSpan(1, 1, 1)),
        ]
    )

    assert [(move.source_path, move.dest_path) for move in result.moves] == [
        ("Drop/ep01.mkv", "Show (2024) {tmdb-123}/S01/Show S01E01.mkv"),
        ("Drop/ep01.srt", "Show (2024) {tmdb-123}/S01/Show S01E01.chs.srt"),
    ]
    assert all(move.source_root is Root.INBOUND for move in result.moves)
    assert all(move.dest_root is Root.LIBRARY for move in result.moves)


def test_everything_not_mapped_is_reported_as_unmapped() -> None:
    result = plan([MappingEntry("V1", EpisodeSpan(1, 1, 1))])
    assert result.unmapped == ("V2", "S1", "O1")


def test_subtitle_variant_comes_from_the_snapshot_not_the_agent() -> None:
    snapshot = (
        SnapshotFile("V1", "ep01.mkv", FileKind.VIDEO, 100),
        SnapshotFile(
            "S1", "ep01.srt", FileKind.SUBTITLE, 10, SubtitleVariant.CHT
        ),
    )
    result = plan(
        [MappingEntry("V1", EpisodeSpan(1, 1, 1)), MappingEntry("S1", EpisodeSpan(1, 1, 1))],
        snapshot=snapshot,
    )
    assert result.moves[1].dest_path.endswith(".cht.srt")


def test_two_files_landing_on_one_destination_is_rejected() -> None:
    with pytest.raises(PlanError) as error:
        plan(
            [
                MappingEntry("V1", EpisodeSpan(1, 1, 1)),
                MappingEntry("V2", EpisodeSpan(1, 1, 1)),
            ]
        )
    assert error.value.code == "destination_collision"


def test_two_subtitles_of_different_variants_can_share_an_episode() -> None:
    snapshot = (
        SnapshotFile("V1", "ep01.mkv", FileKind.VIDEO, 100),
        SnapshotFile("S1", "chs.srt", FileKind.SUBTITLE, 10, SubtitleVariant.CHS),
        SnapshotFile("S2", "cht.srt", FileKind.SUBTITLE, 10, SubtitleVariant.CHT),
    )
    result = plan(
        [
            MappingEntry("V1", EpisodeSpan(1, 1, 1)),
            MappingEntry("S1", EpisodeSpan(1, 1, 1)),
            MappingEntry("S2", EpisodeSpan(1, 1, 1)),
        ],
        snapshot=snapshot,
    )
    assert len(result.moves) == 3


@pytest.mark.parametrize(
    ("entries", "code"),
    [
        ([], "empty_mapping"),
        ([MappingEntry("V9", EpisodeSpan(1, 1, 1))], "unknown_candidate"),
        (
            [
                MappingEntry("V1", EpisodeSpan(1, 1, 1)),
                MappingEntry("V1", EpisodeSpan(1, 2, 2)),
            ],
            "duplicate_candidate",
        ),
        ([MappingEntry("O1", EpisodeSpan(1, 1, 1))], "unmappable_kind"),
        ([MappingEntry("V1")], "missing_episode"),
    ],
)
def test_invalid_mappings_are_rejected_with_a_code(entries, code) -> None:
    with pytest.raises(PlanError) as error:
        plan(entries)
    assert error.value.code == code


def test_movie_maps_one_feature_and_its_subtitles() -> None:
    result = plan(
        [MappingEntry("V1"), MappingEntry("S1")], identity=MOVIE
    )
    assert [move.dest_path for move in result.moves] == [
        "Feature (2016) {tmdb-456}/Feature (2016).mkv",
        "Feature (2016) {tmdb-456}/Feature (2016).chs.srt",
    ]


def test_movie_refuses_a_second_feature() -> None:
    with pytest.raises(PlanError) as error:
        plan([MappingEntry("V1"), MappingEntry("V2")], identity=MOVIE)
    assert error.value.code == "movie_needs_exactly_one_video"


def test_movie_refuses_episode_numbers() -> None:
    with pytest.raises(PlanError) as error:
        plan([MappingEntry("V1", EpisodeSpan(1, 1, 1))], identity=MOVIE)
    assert error.value.code == "movie_has_no_episodes"


def test_new_season_joins_an_existing_tagged_folder_untouched() -> None:
    result = plan(
        [MappingEntry("V1", EpisodeSpan(2, 1, 1))],
        existing_folder=ExistingFolder("Show (2024) {tmdb-123}"),
    )
    assert [move.kind for move in result.moves] == [MoveKind.MEDIA]
    assert result.moves[0].dest_path.startswith("Show (2024) {tmdb-123}/S02/")


def test_untagged_folder_is_renamed_before_the_new_season_lands() -> None:
    result = plan(
        [MappingEntry("V1", EpisodeSpan(2, 1, 1))],
        existing_folder=ExistingFolder("Show"),
    )
    rename, media = result.moves
    assert rename.kind is MoveKind.FOLDER_RENAME
    assert (rename.source_path, rename.dest_path) == (
        "Show",
        "Show (2024) {tmdb-123}",
    )
    assert rename.source_root is Root.LIBRARY
    assert media.dest_path.startswith("Show (2024) {tmdb-123}/S02/")
