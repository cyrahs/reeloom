from __future__ import annotations

from collections.abc import Mapping

from reeloom.kernel.errors import DomainError, ErrorCode


def require_object(
    value: object,
    *,
    field: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise DomainError(
            ErrorCode.INVALID_FIELD_TYPE,
            context={"field": field, "expected": "object"},
        )
    return value


def check_fields(
    value: object,
    expected: frozenset[str],
    *,
    field: str,
) -> Mapping[str, object]:
    payload = require_object(value, field=field)
    keys = frozenset(payload)
    extra_keys = keys - expected
    if extra_keys:
        raise DomainError(
            ErrorCode.EXTRA_KEYS,
            context={"keys": tuple(sorted(extra_keys))},
        )

    missing_keys = expected - keys
    if missing_keys:
        raise DomainError(
            ErrorCode.MISSING_KEYS,
            context={"keys": tuple(sorted(missing_keys))},
        )
    return payload
