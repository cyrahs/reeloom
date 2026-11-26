from __future__ import annotations

import pytest

from reeloom.release import (
    Filter,
    ReleaseEpisodes,
    ReleaseError,
    compile_pattern,
    folder_for,
    is_covered,
    matches_group,
    parse_episodes,
)


@pytest.mark.parametrize(
    ("title", "season", "episodes"),
    [
        ("I Miss You S01E11 2026 2160p MGTV WEB-DL H.265 AAC 2.0-ADWeb", 1, (11,)),
        ("Miss You S01E11-E12 2026 1080p WEB-DL H.264 AAC2.0-HHWEB", 1, (11, 12)),
        ("Miss You S01E09-E10 E09-E10 2026 1080p WEB-DL-HHWEB", 1, (9, 10)),
        ("I Miss You S01 E01-E02 2026 1080p WEB-DL H.264 AAC2.0-HHWEB", 1, (1, 2)),
        ("Show S02E01-03 1080p", 2, (1, 2, 3)),
        ("Show S01E01-1080p WEB", 1, (1,)),
        ("Show S01EP05 2026", 1, (5,)),
        ("Show 2026 S01 2160p WEB-DL-GROUP", 1, None),
        ("Show Season 2 Complete 1080p", 2, None),
        ("Show 2026 1080p WEB-DL", None, None),
    ],
)
def test_parse_episodes(title, season, episodes) -> None:
    assert parse_episodes(title) == ReleaseEpisodes(season, episodes)


def test_group_matches_whole_trailing_token_case_insensitively() -> None:
    title = "I Miss You S01E05 2026 1080p WEB-DL H.264 AAC2.0-HHWEB"
    assert matches_group(title, ["hhweb"])
    assert not matches_group(title, ["WEB"])
    assert not matches_group(title, ["ADWeb"])
    assert matches_group(title, [])
    assert matches_group("Show S01E01 1080p@MTeam", ["MTeam"])
    assert matches_group("[Nekomoe] Show - 01 [1080p].mkv", ["Nekomoe"])


def test_filter_reasons() -> None:
    title = "I Miss You S01E05 2026 2160p WEB-DL-ADWeb"
    parsed = parse_episodes(title)
    assert Filter(groups=("HHWEB",)).rejects(title, parsed) == "group"
    assert Filter(include="1080p").rejects(title, parsed) == "include"
    assert Filter(exclude="2160p").rejects(title, parsed) == "exclude"
    assert Filter(season=2).rejects(title, parsed) == "season"
    assert Filter(groups=("ADWeb",), include="2160p", season=1).rejects(
        title, parsed
    ) is None


def test_invalid_pattern_is_a_stable_error() -> None:
    with pytest.raises(ReleaseError) as raised:
        compile_pattern("(")
    assert raised.value.code == "invalid_pattern"


def test_coverage() -> None:
    covered = [(1, 1), (1, 2)]
    assert is_covered(ReleaseEpisodes(1, (1, 2)), covered)
    assert not is_covered(ReleaseEpisodes(1, (2, 3)), covered)
    # A pack is unwanted once anything of its season was grabbed.
    assert is_covered(ReleaseEpisodes(1, None), covered)
    assert not is_covered(ReleaseEpisodes(2, None), covered)
    # A grabbed pack covers every episode of its season.
    assert is_covered(ReleaseEpisodes(1, (9,)), [(1, None)])
    # Without a season nothing can be deduped.
    assert not is_covered(ReleaseEpisodes(None, None), covered)


def test_folder_names_are_safe_and_unique() -> None:
    assert folder_for("A/B\\C  D.", set()) == "A B C D"
    assert folder_for("Show S01E01", {"Show S01E01"}) == "Show S01E01 #2"
    assert folder_for("archive", set()) == "release archive"
    assert folder_for("..hidden", set()) == "hidden"
    assert folder_for("", set()) == "release"
    long = folder_for("后" * 200, set())
    assert len(long.encode()) <= 180
