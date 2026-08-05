from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.errors import DomainError, ErrorCode

_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
_PLAN_HASH = "sha256:" + "a" * 64
_NONCE = "n" * 32


def _approval() -> ApprovalRecord:
    return ApprovalRecord.create(
        run_id="run-m6",
        plan_hash=_PLAN_HASH,
        scope=ApprovalScope.APPLY,
        expires_at=_NOW + timedelta(minutes=5),
        nonce=_NONCE,
    )


def test_approval_record_is_canonical_and_self_authenticating() -> None:
    approval = _approval()

    restored = ApprovalRecord.from_canonical_bytes(
        approval.canonical_bytes()
    )

    assert restored == approval
    assert restored.verify_id()
    assert restored.approval_id.startswith("approval-v1-")
    assert restored.expires_at == "2026-07-23T12:05:00.000000Z"
    assert restored.canonical_bytes() == approval.canonical_bytes()


def test_approval_record_rejects_tampered_canonical_bytes() -> None:
    approval = _approval()
    tampered = approval.canonical_bytes().replace(
        b'"run-m6"',
        b'"run-x6"',
    )

    with pytest.raises(DomainError) as raised:
        ApprovalRecord.from_canonical_bytes(tampered)

    assert raised.value.code is ErrorCode.INVALID_APPROVAL


def test_subtitle_acquisition_approval_has_distinct_exact_scope() -> None:
    approval = ApprovalRecord.create(
        run_id="run-m13",
        plan_hash=_PLAN_HASH,
        scope=ApprovalScope.SUBTITLE_ACQUIRE,
        expires_at=_NOW + timedelta(minutes=5),
        nonce=_NONCE,
    )

    restored = ApprovalRecord.from_canonical_bytes(
        approval.canonical_bytes()
    )
    assert restored.scope is ApprovalScope.SUBTITLE_ACQUIRE
    assert restored.approval_id != _approval().approval_id


@pytest.mark.parametrize(
    ("expires_at", "nonce"),
    (
        (datetime(2026, 7, 23, 12, 5), _NONCE),
        (_NOW + timedelta(minutes=5), "short"),
    ),
)
def test_approval_record_rejects_invalid_security_fields(
    expires_at: datetime,
    nonce: str,
) -> None:
    with pytest.raises(DomainError) as raised:
        ApprovalRecord.create(
            run_id="run-m6",
            plan_hash=_PLAN_HASH,
            scope=ApprovalScope.APPLY,
            expires_at=expires_at,
            nonce=nonce,
        )

    assert raised.value.code is ErrorCode.INVALID_APPROVAL
