"""Scrub credentials out of text bound for logs or error messages.

Two secrets travel inside request URLs: the TMDB key as an ``api_key`` query
value and the Telegram bot token as a ``/bot<token>`` path segment. httpx
prints the full URL in its INFO request line and can quote it in exception
messages, so anything that renders one of those passes through here first.
"""

from __future__ import annotations

import logging
import re

REDACTED = "<redacted>"

_PATTERNS = (
    re.compile(r"(?i)(api_key=)[^&\s'\"]*"),
    re.compile(r"(/bot)[0-9]{1,20}:[A-Za-z0-9_-]+"),
)


def redact(text: str, *secrets: str) -> str:
    """Replace known secret values, then the URL shapes that carry one."""

    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(lambda match: match.group(1) + REDACTED, text)
    return text


def describe(error: BaseException, *secrets: str) -> str:
    """``TypeName: message`` with credentials scrubbed, bounded for a log line."""

    message = redact(str(error), *secrets)[:200]
    name = type(error).__name__
    return f"{name}: {message}" if message else name


class RedactingFormatter(logging.Formatter):
    """Scrubs the whole rendered record, chained-cause tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
