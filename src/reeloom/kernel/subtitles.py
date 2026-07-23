from __future__ import annotations

import re
import unicodedata

from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.naming import SubtitleVariant

MAX_SUBTITLE_SAMPLE_BYTES = 64 * 1024

_TOKEN_PATTERN = re.compile(r"[\W_]+", re.UNICODE)
_SIMPLIFIED_TOKENS = frozenset({"chs", "sc", "gb", "gbk", "简体", "简中"})
_TRADITIONAL_TOKENS = frozenset(
    {"cht", "tc", "big5", "繁体", "繁中"}
)
_GENERIC_TOKENS = frozenset({"chi", "zh", "中文"})
_SIMPLIFIED_MARKERS = frozenset("后发里国台万与云为这")
_TRADITIONAL_MARKERS = frozenset("後發裡國臺萬與雲為這")


def _filename_variant(display_name: str) -> SubtitleVariant | None:
    normalized = unicodedata.normalize("NFKC", display_name).casefold()
    tokens = frozenset(
        token for token in _TOKEN_PATTERN.split(normalized) if token
    )
    simplified = bool(tokens & _SIMPLIFIED_TOKENS)
    traditional = bool(tokens & _TRADITIONAL_TOKENS)
    if simplified and not traditional:
        return SubtitleVariant.CHS
    if traditional and not simplified:
        return SubtitleVariant.CHT
    if simplified or traditional or tokens & _GENERIC_TOKENS:
        return SubtitleVariant.CHI
    return None


def _decode_samples(sample: bytes) -> tuple[str, ...]:
    bounded = sample[:MAX_SUBTITLE_SAMPLE_BYTES]
    if bounded.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return (bounded.decode("utf-16"),)
        except UnicodeError:
            return ()

    decoded: list[str] = []
    for encoding in ("utf-8-sig", "gb18030", "big5"):
        try:
            value = bounded.decode(encoding)
        except UnicodeError:
            continue
        if value not in decoded:
            decoded.append(value)
    return tuple(decoded)


def _marker_counts(value: str) -> tuple[int, int]:
    normalized = unicodedata.normalize("NFKC", value)
    return (
        sum(normalized.count(marker) for marker in _SIMPLIFIED_MARKERS),
        sum(normalized.count(marker) for marker in _TRADITIONAL_MARKERS),
    )


def detect_subtitle_variant(
    display_name: object,
    sample: object,
) -> SubtitleVariant:
    """Classify a bounded sample without exposing subtitle text to the Agent."""

    if not isinstance(display_name, str) or not isinstance(
        sample,
        bytes,
    ):
        raise DomainError(ErrorCode.INVALID_SUBTITLE_VARIANT)

    filename_variant = _filename_variant(display_name)
    if filename_variant is not None:
        return filename_variant

    simplified_count, traditional_count = max(
        (
            _marker_counts(decoded)
            for decoded in _decode_samples(sample)
        ),
        key=lambda counts: max(counts),
        default=(0, 0),
    )
    if simplified_count and not traditional_count:
        return SubtitleVariant.CHS
    if traditional_count and not simplified_count:
        return SubtitleVariant.CHT
    return SubtitleVariant.CHI
