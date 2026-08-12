from __future__ import annotations

import pytest

from reeloom.models import (
    EpisodeSpan,
    MediaIdentity,
    MediaType,
    PlanError,
    SubtitleVariant,
)
from reeloom.naming import (
    ExistingFolder,
    episode_path,
    folder_name,
    movie_path,
    parse_tmdb_id,
    resolve_library_folder,
    sanitize_title,
)

SERIES = MediaIdentity(MediaType.ANIME, 123, "赛马娘", 2024)
MOVIE = MediaIdentity(MediaType.MOVIE, 456, "你的名字", 2016)


def test_series_layout() -> None:
    path = episode_path(SERIES, EpisodeSpan(1, 1, 1), ".mkv")
    assert path.as_posix() == "赛马娘 (2024) {tmdb-123}/S01/赛马娘 S01E01.mkv"


def test_multi_episode_file_keeps_a_span_in_the_name() -> None:
    path = episode_path(SERIES, EpisodeSpan(2, 5, 6), ".mkv")
    assert path.name == "赛马娘 S02E05-E06.mkv"


def test_subtitle_shares_the_video_base_name_with_a_variant_tag() -> None:
    path = episode_path(
        SERIES, EpisodeSpan(1, 1, 1), ".srt", variant=SubtitleVariant.CHS
    )
    assert path.name == "赛马娘 S01E01.chs.srt"
    assert path.parent.name == "S01"


def test_movie_layout_has_no_season_level() -> None:
    assert movie_path(MOVIE, ".mkv").as_posix() == (
        "你的名字 (2016) {tmdb-456}/你的名字 (2016).mkv"
    )
    assert movie_path(MOVIE, ".ass", variant=SubtitleVariant.CHT).name == (
        "你的名字 (2016).cht.ass"
    )


def test_specials_use_season_zero() -> None:
    path = episode_path(SERIES, EpisodeSpan(0, 1, 1), ".mkv")
    assert path.parent.name == "S00"
    assert path.name == "赛马娘 S00E01.mkv"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("A/B", "A B"),
        ("Show: The Movie", "Show The Movie"),
        ("  padded  ", "padded"),
        ("tab\tin\nname", "tab in name"),
        ("dots...", "dots"),
        ("Ｆｕｌｌｗｉｄｔｈ", "Fullwidth"),
        ("CON", "CON_"),
        ("con.mkv", "con_.mkv"),
    ],
)
def test_sanitize_title_neutralizes_untrusted_tmdb_text(
    raw: str, expected: str
) -> None:
    assert sanitize_title(raw) == expected


def test_sanitize_title_rejects_titles_with_nothing_left() -> None:
    with pytest.raises(PlanError):
        sanitize_title("///")


def test_long_title_is_truncated_without_splitting_a_character() -> None:
    result = sanitize_title("字" * 200)
    assert len(result.encode("utf-8")) <= 160
    assert "�" not in result


def test_unsupported_extension_is_refused() -> None:
    with pytest.raises(PlanError):
        episode_path(SERIES, EpisodeSpan(1, 1, 1), ".exe")
    with pytest.raises(PlanError):
        episode_path(SERIES, EpisodeSpan(1, 1, 1), ".srt")


def test_invalid_spans_are_refused() -> None:
    with pytest.raises(PlanError):
        EpisodeSpan(1, 5, 2)
    with pytest.raises(PlanError):
        EpisodeSpan(-1, 1, 1)


def test_parse_tmdb_id() -> None:
    assert parse_tmdb_id("Show (2024) {tmdb-123}") == 123
    assert parse_tmdb_id("Show (2024)") is None


def test_missing_folder_uses_the_canonical_name() -> None:
    assert resolve_library_folder(SERIES, None) == (folder_name(SERIES), None)


def test_existing_tagged_folder_is_never_renamed() -> None:
    existing = ExistingFolder("Uma Musume (2024) {tmdb-123}")
    assert resolve_library_folder(SERIES, existing) == (existing.name, None)


def test_untagged_folder_is_renamed_once_to_pick_up_the_id() -> None:
    target, rename_from = resolve_library_folder(SERIES, ExistingFolder("赛马娘"))
    assert target == "赛马娘 (2024) {tmdb-123}"
    assert rename_from == "赛马娘"


def test_span_from_name() -> None:
    from reeloom.naming import span_from_name

    assert span_from_name("Show S01E05.mkv") == EpisodeSpan(1, 5, 5)
    assert span_from_name("Show S01E01-E12.mkv") == EpisodeSpan(1, 1, 12)
    assert span_from_name("Show 第5集.mkv") is None
    # An inverted span in an arbitrary filename is unparseable, not an error.
    assert span_from_name("Weird S01E05-E03.mkv") is None
