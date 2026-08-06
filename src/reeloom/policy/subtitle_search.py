from __future__ import annotations

import unicodedata

from reeloom.kernel.errors import DomainError, ErrorCode
from reeloom.kernel.subtitle_acquisition import (
    MAX_SUBTITLE_SEARCH_ALIASES,
    MAX_SUBTITLE_SEARCH_ALIAS_BYTES,
)

MAX_SUBTITLE_SEARCH_TERMS = 12
MIN_RELAXED_SEARCH_CHARACTERS = 2

_DISCUZ_QUERY_METACHARACTERS = frozenset("*+&|")


def _alias_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _is_safe_literal(value: str) -> bool:
    """Return whether Discuz will treat the value as one literal term."""

    return (
        not any(character.isspace() for character in value)
        and not any(
            character in _DISCUZ_QUERY_METACHARACTERS
            for character in value
        )
        and "OR" not in value
    )


def _and_term_query(value: str) -> str | None:
    """Compile a bounded query whose ASCII spaces mean AND to Discuz."""

    terms: list[str] = []
    current: list[str] = []
    information_characters = 0
    for character in value.casefold():
        category = unicodedata.category(character)
        if category[:1] in {"L", "M", "N"}:
            current.append(character)
            if category[:1] in {"L", "N"}:
                information_characters += 1
            continue
        if current:
            terms.append("".join(current))
            current = []
    if current:
        terms.append("".join(current))
    if (
        information_characters < MIN_RELAXED_SEARCH_CHARACTERS
        or not terms
        or len(terms) > MAX_SUBTITLE_SEARCH_TERMS
    ):
        return None
    query = " ".join(terms)
    if len(query.encode("utf-8")) > MAX_SUBTITLE_SEARCH_ALIAS_BYTES:
        return None
    return query


def compile_subtitle_search_aliases(title: str) -> tuple[str, ...]:
    """Compile safe, deterministic Discuz queries from one trusted title.

    Punctuation is kept in a literal query when Discuz has no special meaning
    for it. A second query turns every non-letter/mark/number run into one
    ASCII space, which Discuz compiles as an AND between title terms.
    """

    if (
        not isinstance(title, str)
        or not title.strip()
        or any(
            unicodedata.category(character).startswith("C")
            for character in title
        )
    ):
        raise DomainError(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)
    normalized = unicodedata.normalize("NFKC", title).strip()
    if (
        not normalized
        or len(normalized.encode("utf-8"))
        > MAX_SUBTITLE_SEARCH_ALIAS_BYTES
    ):
        raise DomainError(ErrorCode.INVALID_SUBTITLE_SEARCH_DATA)

    candidates: list[str] = []
    if _is_safe_literal(normalized):
        candidates.append(normalized)
    relaxed = _and_term_query(normalized)
    if relaxed is not None:
        candidates.append(relaxed)

    aliases: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = _alias_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        aliases.append(candidate)
        if len(aliases) == MAX_SUBTITLE_SEARCH_ALIASES:
            break
    return tuple(aliases)
