from pathlib import PurePosixPath

import pytest

from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.mapping import EpisodeSpan
from reeloom.kernel.naming import (
    SeriesIdentity,
    SubtitleVariant,
    series_root_name,
    subtitle_relative_path,
    video_relative_path,
)


def frieren() -> SeriesIdentity:
    return SeriesIdentity.from_dict(
        {
            "title_zh_cn": "葬送的芙莉莲",
            "year": 2023,
            "tmdb_id": 209867,
        }
    )


def test_series_root_uses_canonical_identity_contract() -> None:
    assert series_root_name(frieren()) == "葬送的芙莉莲 (2023) {tmdb-209867}"


def test_series_schema_rejects_non_object_input() -> None:
    with pytest.raises(DomainError) as raised:
        SeriesIdentity.from_dict(["title_zh_cn", "year", "tmdb_id"])

    assert raised.value.code is ErrorCode.INVALID_FIELD_TYPE
    assert raised.value.context == {
        "field": "series",
        "expected": "object",
    }


def test_single_episode_video_path_is_deterministic() -> None:
    relative_path = video_relative_path(
        frieren(),
        EpisodeSpan(season=1, episode_start=1, episode_end=1),
        ".MKV",
    )

    assert relative_path == PurePosixPath(
        "葬送的芙莉莲 (2023) {tmdb-209867}",
        "S01",
        "葬送的芙莉莲 S01E01.mkv",
    )


def test_multi_episode_video_path_uses_inclusive_range() -> None:
    relative_path = video_relative_path(
        frieren(),
        EpisodeSpan(season=1, episode_start=2, episode_end=3),
        ".mp4",
    )

    assert relative_path.name == "葬送的芙莉莲 S01E02-E03.mp4"


def test_specials_use_s00_without_episode_title() -> None:
    relative_path = video_relative_path(
        frieren(),
        EpisodeSpan(season=0, episode_start=1, episode_end=1),
        ".mkv",
    )

    assert relative_path.parts[-2:] == (
        "S00",
        "葬送的芙莉莲 S00E01.mkv",
    )


@pytest.mark.parametrize(
    ("variant", "expected_name"),
    [
        (SubtitleVariant.CHS, "葬送的芙莉莲 S01E02-E03.chs.ass"),
        (SubtitleVariant.CHT, "葬送的芙莉莲 S01E02-E03.cht.ass"),
        (SubtitleVariant.CHI, "葬送的芙莉莲 S01E02-E03.chi.ass"),
    ],
)
def test_subtitle_path_reuses_video_base_and_adds_variant(
    variant: SubtitleVariant,
    expected_name: str,
) -> None:
    relative_path = subtitle_relative_path(
        frieren(),
        EpisodeSpan(season=1, episode_start=2, episode_end=3),
        variant,
        ".ASS",
    )

    assert relative_path.name == expected_name


def test_untrusted_title_is_normalized_to_one_safe_component() -> None:
    identity = SeriesIdentity.from_dict(
        {
            "title_zh_cn": "  ../../CON:\u202e Anime／Test  ",
            "year": 2024,
            "tmdb_id": 42,
        }
    )

    relative_path = video_relative_path(
        identity,
        EpisodeSpan(season=1, episode_start=1, episode_end=1),
        ".mkv",
    )

    assert identity.title_zh_cn == "CON Anime Test"
    assert not relative_path.is_absolute()
    assert ".." not in relative_path.parts
    assert relative_path.parts == (
        "CON Anime Test (2024) {tmdb-42}",
        "S01",
        "CON Anime Test S01E01.mkv",
    )


@pytest.mark.parametrize(
    ("raw_title", "expected_title"),
    [
        ("CON", "CON_"),
        ("COM1.txt", "COM1_.txt"),
    ],
)
def test_reserved_device_title_is_made_portable(
    raw_title: str,
    expected_title: str,
) -> None:
    identity = SeriesIdentity.from_dict(
        {
            "title_zh_cn": raw_title,
            "year": 2024,
            "tmdb_id": 42,
        }
    )

    assert identity.title_zh_cn == expected_title


def test_title_that_sanitizes_to_empty_is_rejected() -> None:
    with pytest.raises(DomainError) as raised:
        SeriesIdentity.from_dict(
            {
                "title_zh_cn": "../..",
                "year": 2024,
                "tmdb_id": 42,
            }
        )

    assert raised.value.code is ErrorCode.INVALID_SERIES_TITLE


def test_series_schema_rejects_agent_supplied_destination() -> None:
    with pytest.raises(DomainError) as raised:
        SeriesIdentity.from_dict(
            {
                "title_zh_cn": "安全剧名",
                "year": 2024,
                "tmdb_id": 42,
                "destination": "../../chosen-by-agent",
            }
        )

    assert raised.value.code is ErrorCode.EXTRA_KEYS
    assert raised.value.context == {"keys": ("destination",)}


def test_series_schema_rejects_episode_title() -> None:
    with pytest.raises(DomainError) as raised:
        SeriesIdentity.from_dict(
            {
                "title_zh_cn": "安全剧名",
                "year": 2024,
                "tmdb_id": 42,
                "episode_title": "模型建议的单集标题",
            }
        )

    assert raised.value.code is ErrorCode.EXTRA_KEYS
    assert raised.value.context == {"keys": ("episode_title",)}


@pytest.mark.parametrize(
    "extension",
    ["mkv", ".mkv.exe", "../mkv", ".ass"],
)
def test_video_extension_must_be_a_supported_single_suffix(
    extension: str,
) -> None:
    with pytest.raises(DomainError) as raised:
        video_relative_path(
            frieren(),
            EpisodeSpan(season=1, episode_start=1, episode_end=1),
            extension,
        )

    assert raised.value.code is ErrorCode.INVALID_FILE_EXTENSION


def test_subtitle_extension_cannot_be_a_video_extension() -> None:
    with pytest.raises(DomainError) as raised:
        subtitle_relative_path(
            frieren(),
            EpisodeSpan(season=1, episode_start=1, episode_end=1),
            SubtitleVariant.CHS,
            ".mkv",
        )

    assert raised.value.code is ErrorCode.INVALID_FILE_EXTENSION


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("year", True, ErrorCode.INVALID_YEAR),
        ("year", 0, ErrorCode.INVALID_YEAR),
        ("tmdb_id", True, ErrorCode.INVALID_TMDB_ID),
        ("tmdb_id", 0, ErrorCode.INVALID_TMDB_ID),
    ],
)
def test_series_identity_rejects_invalid_numeric_fields(
    field: str,
    value: object,
    expected_code: ErrorCode,
) -> None:
    payload: dict[str, object] = {
        "title_zh_cn": "安全剧名",
        "year": 2024,
        "tmdb_id": 42,
    }
    payload[field] = value

    with pytest.raises(DomainError) as raised:
        SeriesIdentity.from_dict(payload)

    assert raised.value.code is expected_code


def test_long_unicode_title_keeps_every_path_component_within_255_bytes() -> None:
    identity = SeriesIdentity.from_dict(
        {
            "title_zh_cn": "剧" * 200,
            "year": 2024,
            "tmdb_id": 42,
        }
    )

    relative_path = video_relative_path(
        identity,
        EpisodeSpan(season=1, episode_start=1, episode_end=1),
        ".mkv",
    )

    assert all(len(part.encode("utf-8")) <= 255 for part in relative_path.parts)
