from __future__ import annotations

import pytest

from reeloom.kernel.naming import SubtitleVariant
from reeloom.kernel.subtitles import detect_subtitle_variant


@pytest.mark.parametrize(
    ("display_name", "sample", "expected"),
    (
        ("Episode.01.chs.ass", b"", SubtitleVariant.CHS),
        ("Episode.01.cht.ass", b"", SubtitleVariant.CHT),
        ("Episode.01.chi.ass", b"", SubtitleVariant.CHI),
        ("Episode_01_chs.ass", b"", SubtitleVariant.CHS),
        ("Episode.01.ass", "这是简体字幕，后台发布。".encode(), SubtitleVariant.CHS),
        ("Episode.01.ass", "這是繁體字幕，後臺發佈。".encode(), SubtitleVariant.CHT),
        (
            "Episode.01.ass",
            "这是简体字幕，后台发布。".encode("gb18030"),
            SubtitleVariant.CHS,
        ),
        (
            "Episode.01.ass",
            "這是繁體字幕，後臺發佈。".encode("big5"),
            SubtitleVariant.CHT,
        ),
        (
            "Episode.01.ass",
            "這是繁體字幕，後臺發佈。".encode("utf-16"),
            SubtitleVariant.CHT,
        ),
        ("Episode.01.ass", b"dialogue", SubtitleVariant.CHI),
        ("Episode.chs.cht.ass", b"", SubtitleVariant.CHI),
    ),
)
def test_subtitle_variant_uses_filename_then_bounded_content(
    display_name: str,
    sample: bytes,
    expected: SubtitleVariant,
) -> None:
    assert detect_subtitle_variant(display_name, sample) is expected
