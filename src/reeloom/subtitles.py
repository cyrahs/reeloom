"""Chinese subtitle variant classification.

Deterministic, and deliberately not an Agent decision: the filename tokens
decide when they are unambiguous, otherwise a bounded sample of the text is
scored on characters that differ between the two scripts. Subtitle text never
reaches the model.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from reeloom.models import SubtitleVariant

MAX_SAMPLE_BYTES = 64 * 1024

_TOKEN = re.compile(r"[\W_]+", re.UNICODE)
# ASCII markers only count as whole tokens ("sc" inside "screen" says nothing);
# CJK markers are matched as substrings because they fuse into compound runs
# like 简体中文 or 简日双语 that token splitting keeps in one piece.
_SIMPLIFIED_TOKENS = frozenset({"chs", "sc", "gb", "gbk", "gb2312", "jpsc"})
_TRADITIONAL_TOKENS = frozenset({"cht", "tc", "big5", "jptc"})
_GENERIC_TOKENS = frozenset({"chi", "zh"})
_SIMPLIFIED_MARKS = ("简体", "簡體", "简中", "簡中", "简日", "簡日")
_TRADITIONAL_MARKS = ("繁體", "繁体", "繁中", "繁日", "正體", "正体")
_DUAL_MARKS = ("简繁", "簡繁", "繁简", "繁簡")
_GENERIC_MARKS = ("中文", "中字")
_SIMPLIFIED_MARKERS = frozenset("后发里国台万与云为这")
_TRADITIONAL_MARKERS = frozenset("後發裡國臺萬與雲為這")


def variant_from_name(name: str) -> SubtitleVariant | None:
    normalized = unicodedata.normalize("NFKC", name).casefold()
    tokens = {token for token in _TOKEN.split(normalized) if token}
    if any(mark in normalized for mark in _DUAL_MARKS):
        return SubtitleVariant.CHI
    simplified = bool(tokens & _SIMPLIFIED_TOKENS) or any(
        mark in normalized for mark in _SIMPLIFIED_MARKS
    )
    traditional = bool(tokens & _TRADITIONAL_TOKENS) or any(
        mark in normalized for mark in _TRADITIONAL_MARKS
    )
    if simplified and not traditional:
        return SubtitleVariant.CHS
    if traditional and not simplified:
        return SubtitleVariant.CHT
    if (
        simplified
        or traditional
        or tokens & _GENERIC_TOKENS
        or any(mark in normalized for mark in _GENERIC_MARKS)
    ):
        return SubtitleVariant.CHI
    return None


# Language tags release groups actually mux: ISO 639-2 chi/zho, bare zh and
# BCP-47 zh-*, cmn/yue, plus the non-standard but common chs/cht.
_CHINESE_LANGUAGE_TAGS = frozenset({"chi", "zho", "zh", "chs", "cht", "cmn", "yue"})


def variant_from_stream_tags(
    language: str | None, title: str | None
) -> SubtitleVariant | None:
    """Classify an embedded subtitle track by its container tags.

    ``None`` means the track is not identifiably Chinese; an untagged track
    stays unknown rather than guessed.
    """

    if title:
        from_title = variant_from_name(title)
        if from_title is not None:
            return from_title
    if not language:
        return None
    normalized = (
        unicodedata.normalize("NFKC", language).casefold().replace("_", "-")
    )
    base = normalized.split("-", 1)[0]
    if base not in _CHINESE_LANGUAGE_TAGS:
        return None
    if base == "chs" or "hans" in normalized:
        return SubtitleVariant.CHS
    if base == "cht" or "hant" in normalized:
        return SubtitleVariant.CHT
    return SubtitleVariant.CHI


def _decodings(sample: bytes) -> list[str]:
    bounded = sample[:MAX_SAMPLE_BYTES]
    if bounded.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return [bounded.decode("utf-16", errors="ignore")]
        except UnicodeError:
            return []
    decoded: list[str] = []
    for encoding in ("utf-8-sig", "gb18030", "big5"):
        try:
            value = bounded.decode(encoding)
        except UnicodeError:
            continue
        if value not in decoded:
            decoded.append(value)
    return decoded


def variant_from_sample(sample: bytes) -> SubtitleVariant:
    best = (0, 0)
    for decoded in _decodings(sample):
        normalized = unicodedata.normalize("NFKC", decoded)
        counts = (
            sum(normalized.count(marker) for marker in _SIMPLIFIED_MARKERS),
            sum(normalized.count(marker) for marker in _TRADITIONAL_MARKERS),
        )
        if max(counts) > max(best):
            best = counts
    simplified, traditional = best
    if simplified and not traditional:
        return SubtitleVariant.CHS
    if traditional and not simplified:
        return SubtitleVariant.CHT
    return SubtitleVariant.CHI


def detect_variant(name: str, sample: bytes) -> SubtitleVariant:
    """Name tokens win when they are unambiguous; otherwise read the text."""

    from_name = variant_from_name(name)
    return from_name if from_name is not None else variant_from_sample(sample)


def detect_variant_for_file(path: Path) -> SubtitleVariant:
    from_name = variant_from_name(path.name)
    if from_name is not None:
        return from_name
    try:
        with path.open("rb") as handle:
            sample = handle.read(MAX_SAMPLE_BYTES)
    except OSError:
        return SubtitleVariant.CHI
    return variant_from_sample(sample)
