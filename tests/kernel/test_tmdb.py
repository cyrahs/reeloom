from __future__ import annotations

import pytest

from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.specials import SpecialKind
from reeloom.kernel.tmdb import (
    TmdbCandidateRef,
    TmdbEpisode,
    TmdbLanguage,
    TmdbMediaType,
    TmdbSearchCandidate,
    TmdbSeasonDetails,
    TmdbSeriesDetails,
    TmdbWorkType,
    classify_special_kind,
    preferred_series_identity,
)


@pytest.mark.parametrize(
    ("name", "overview", "expected"),
    (
        ("OVA 1", "", SpecialKind.OVA),
        ("Bonus", "Original Video Animation", SpecialKind.OVA),
        ("原创视频动画", "", SpecialKind.OVA),
        ("OAD 第1话", "", SpecialKind.OAD),
        ("Bonus", "Original Animation DVD", SpecialKind.OAD),
        ("随书附赠动画", "", SpecialKind.OAD),
        ("Special 1", "A normal bonus episode", SpecialKind.UNKNOWN),
    ),
)
def test_special_kind_uses_explicit_english_and_chinese_evidence(
    name: str,
    overview: str,
    expected: SpecialKind,
) -> None:
    assert classify_special_kind(name, overview) is expected


def test_special_hint_ignores_evidence_beyond_exposed_text_bounds() -> None:
    episode = TmdbEpisode(
        season_number=0,
        episode_number=1,
        name=("x" * 240) + " OVA",
        overview=("y" * 1_000) + " OAD",
        special_kind=classify_special_kind(
            ("x" * 240) + " OVA",
            ("y" * 1_000) + " OAD",
        ),
    )

    assert episode.name == "x" * 240
    assert episode.overview == "y" * 1_000
    assert episode.special_kind is SpecialKind.UNKNOWN


@pytest.mark.parametrize(
    ("work_type", "media_type"),
    (
        (TmdbWorkType.ANIME, TmdbMediaType.TV),
        (TmdbWorkType.TV_SERIES, TmdbMediaType.TV),
        (TmdbWorkType.MOVIE, TmdbMediaType.MOVIE),
    ),
)
def test_work_type_maps_to_tmdb_media_namespace(
    work_type: TmdbWorkType,
    media_type: TmdbMediaType,
) -> None:
    candidate = TmdbSearchCandidate(
        tmdb_id=100,
        localized_name="Title",
        original_name="Title",
        year=2020,
        original_language="ja",
        work_type=work_type,
    )

    assert candidate.media_type is media_type
    assert candidate.reference == TmdbCandidateRef(
        work_type=work_type,
        tmdb_id=100,
    )


def test_movie_cannot_be_represented_as_episode_series_details() -> None:
    with pytest.raises(DomainError) as error:
        TmdbSeriesDetails(
            tmdb_id=100,
            language=TmdbLanguage.ZH_CN,
            localized_name="Movie",
            original_name="Movie",
            first_air_year=2020,
            seasons=(),
            work_type=TmdbWorkType.MOVIE,
        )

    assert error.value.code is ErrorCode.INVALID_TMDB_DATA


def test_series_identity_prefers_zh_cn_localized_title() -> None:
    details = TmdbSeriesDetails(
        tmdb_id=100,
        language=TmdbLanguage.ZH_CN,
        localized_name="葬送的芙莉莲",
        original_name="葬送のフリーレン",
        first_air_year=2023,
        seasons=(),
        work_type=TmdbWorkType.ANIME,
    )

    result = preferred_series_identity(details)

    assert result.title_zh_cn == "葬送的芙莉莲"
    assert result.year == 2023


def test_series_identity_falls_back_to_original_title() -> None:
    details = TmdbSeriesDetails(
        tmdb_id=100,
        language=TmdbLanguage.ZH_CN,
        localized_name="",
        original_name="Sousou no Frieren",
        first_air_year=2023,
        seasons=(),
        work_type=TmdbWorkType.ANIME,
    )

    assert (
        preferred_series_identity(details).title_zh_cn
        == "Sousou no Frieren"
    )


def test_series_identity_requires_first_air_year() -> None:
    details = TmdbSeriesDetails(
        tmdb_id=100,
        language=TmdbLanguage.ZH_CN,
        localized_name="Title",
        original_name="Title",
        first_air_year=None,
        seasons=(),
        work_type=TmdbWorkType.ANIME,
    )

    with pytest.raises(DomainError) as error:
        preferred_series_identity(details)

    assert error.value.code is ErrorCode.INVALID_YEAR


def test_season_rejects_duplicate_episode_numbers() -> None:
    episode = TmdbEpisode(
        season_number=0,
        episode_number=1,
        name="OVA",
        overview="",
        special_kind=SpecialKind.OVA,
    )

    with pytest.raises(DomainError) as error:
        TmdbSeasonDetails(
            tmdb_id=100,
            language=TmdbLanguage.ZH_CN,
            season_number=0,
            episodes=(episode, episode),
            work_type=TmdbWorkType.ANIME,
        )

    assert error.value.code is ErrorCode.INVALID_TMDB_DATA
