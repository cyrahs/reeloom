from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from reeloom.adapters.approval import FilesystemApprovalStore
from reeloom.executor.errors import ApprovalError, ApprovalErrorCode
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.policy.path_policy import AuthorizedRoot

_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
_PLAN_HASH = "sha256:" + "a" * 64


def _approval(*, expires_at: datetime | None = None) -> ApprovalRecord:
    return ApprovalRecord.create(
        run_id="run-m6",
        plan_hash=_PLAN_HASH,
        scope=ApprovalScope.APPLY,
        expires_at=expires_at or _NOW + timedelta(minutes=5),
        nonce="n" * 32,
    )


def _root(tmp_path: Path) -> AuthorizedRoot:
    path = tmp_path / "approval-store"
    path.mkdir()
    return AuthorizedRoot.create(path)


def test_approval_claim_is_persistent_and_one_time(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    store = FilesystemApprovalStore(root=root, clock=lambda: _NOW)
    approval = _approval()
    store.issue(approval)

    claimed = store.claim(
        approval_id=approval.approval_id,
        run_id="run-m6",
        plan_hash=_PLAN_HASH,
        scope=ApprovalScope.APPLY,
    )

    assert claimed == approval
    assert len(tuple(root.path.iterdir())) == 2
    restarted = FilesystemApprovalStore(root=root, clock=lambda: _NOW)
    with pytest.raises(ApprovalError) as raised:
        restarted.claim(
            approval_id=approval.approval_id,
            run_id="run-m6",
            plan_hash=_PLAN_HASH,
            scope=ApprovalScope.APPLY,
        )
    assert raised.value.code is ApprovalErrorCode.ALREADY_CLAIMED


def test_issuing_the_same_approval_is_idempotent(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    store = FilesystemApprovalStore(root=root, clock=lambda: _NOW)
    approval = _approval()

    store.issue(approval)
    store.issue(approval)

    assert tuple(path.name for path in root.path.iterdir()) == (
        f"{approval.approval_id}.approval.json",
    )


def test_wrong_binding_does_not_consume_approval(tmp_path: Path) -> None:
    store = FilesystemApprovalStore(
        root=_root(tmp_path),
        clock=lambda: _NOW,
    )
    approval = _approval()
    store.issue(approval)

    with pytest.raises(ApprovalError) as raised:
        store.claim(
            approval_id=approval.approval_id,
            run_id="run-m6",
            plan_hash="sha256:" + "b" * 64,
            scope=ApprovalScope.APPLY,
        )
    assert raised.value.code is ApprovalErrorCode.BINDING_MISMATCH

    assert store.claim(
        approval_id=approval.approval_id,
        run_id="run-m6",
        plan_hash=_PLAN_HASH,
        scope=ApprovalScope.APPLY,
    ) == approval


def test_expired_approval_cannot_be_claimed(tmp_path: Path) -> None:
    now = [_NOW]
    store = FilesystemApprovalStore(
        root=_root(tmp_path),
        clock=lambda: now[0],
    )
    approval = _approval(expires_at=_NOW + timedelta(seconds=1))
    store.issue(approval)
    now[0] = _NOW + timedelta(seconds=2)

    with pytest.raises(ApprovalError) as raised:
        store.claim(
            approval_id=approval.approval_id,
            run_id="run-m6",
            plan_hash=_PLAN_HASH,
            scope=ApprovalScope.APPLY,
        )

    assert raised.value.code is ApprovalErrorCode.EXPIRED


def test_concurrent_claim_has_exactly_one_winner(tmp_path: Path) -> None:
    root = _root(tmp_path)
    approval = _approval()
    FilesystemApprovalStore(root=root, clock=lambda: _NOW).issue(
        approval
    )
    barrier = Barrier(2)

    def claim() -> ApprovalErrorCode | None:
        store = FilesystemApprovalStore(root=root, clock=lambda: _NOW)
        barrier.wait()
        try:
            store.claim(
                approval_id=approval.approval_id,
                run_id="run-m6",
                plan_hash=_PLAN_HASH,
                scope=ApprovalScope.APPLY,
            )
        except ApprovalError as error:
            return error.code
        return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: claim(), range(2)))

    assert sorted(
        result.value if result is not None else "claimed"
        for result in results
    ) == ["already_claimed", "claimed"]


def test_tampered_persisted_approval_fails_closed(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    approval = _approval()
    tampered = approval.canonical_bytes().replace(
        b'"run-m6"',
        b'"run-x6"',
    )
    (
        root.path / f"{approval.approval_id}.approval.json"
    ).write_bytes(tampered)
    store = FilesystemApprovalStore(root=root, clock=lambda: _NOW)

    with pytest.raises(ApprovalError) as raised:
        store.claim(
            approval_id=approval.approval_id,
            run_id="run-m6",
            plan_hash=_PLAN_HASH,
            scope=ApprovalScope.APPLY,
        )

    assert raised.value.code is ApprovalErrorCode.INVALID_RECORD
    assert len(tuple(root.path.iterdir())) == 1


def test_approval_store_does_not_follow_record_symlink(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    approval = _approval()
    outside = tmp_path / "outside-approval.json"
    outside.write_bytes(approval.canonical_bytes())
    (
        root.path / f"{approval.approval_id}.approval.json"
    ).symlink_to(outside)
    store = FilesystemApprovalStore(root=root, clock=lambda: _NOW)

    with pytest.raises(ApprovalError) as raised:
        store.claim(
            approval_id=approval.approval_id,
            run_id="run-m6",
            plan_hash=_PLAN_HASH,
            scope=ApprovalScope.APPLY,
        )

    assert raised.value.code is ApprovalErrorCode.STORE_FAILURE
    assert outside.read_bytes() == approval.canonical_bytes()
    assert len(tuple(root.path.iterdir())) == 1
