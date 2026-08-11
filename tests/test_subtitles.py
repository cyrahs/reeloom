from __future__ import annotations

import pytest

from reeloom.models import SubtitleVariant
from reeloom.subtitles import detect_variant, variant_from_name


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ep01.chs.srt", SubtitleVariant.CHS),
        ("ep01.sc.ass", SubtitleVariant.CHS),
        ("[Group] Show - 01 [GB].ass", SubtitleVariant.CHS),
        ("ep01.简体.srt", SubtitleVariant.CHS),
        ("ep01.cht.srt", SubtitleVariant.CHT),
        ("ep01.BIG5.ass", SubtitleVariant.CHT),
        ("ep01.繁体.srt", SubtitleVariant.CHT),
        ("ep01.中文.srt", SubtitleVariant.CHI),
        ("ep01.chs_cht.srt", SubtitleVariant.CHI),
        ("ep01.srt", None),
    ],
)
def test_filename_tokens_classify_when_unambiguous(name, expected) -> None:
    assert variant_from_name(name) is expected


def test_text_decides_when_the_filename_says_nothing() -> None:
    simplified = "这个国家发展了".encode()
    traditional = "這個國家發展了".encode("big5")

    assert detect_variant("ep01.srt", simplified) is SubtitleVariant.CHS
    assert detect_variant("ep01.srt", traditional) is SubtitleVariant.CHT


def test_filename_wins_over_content() -> None:
    assert detect_variant("ep01.cht.srt", "国家".encode()) is SubtitleVariant.CHT


def test_unclassifiable_content_falls_back_to_generic_chinese() -> None:
    assert detect_variant("ep01.srt", b"plain ascii only") is SubtitleVariant.CHI
    assert detect_variant("ep01.srt", b"") is SubtitleVariant.CHI


def test_utf16_sample_is_decoded() -> None:
    sample = "﻿這個國家".encode("utf-16")
    assert detect_variant("ep01.srt", sample) is SubtitleVariant.CHT
