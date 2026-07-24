from __future__ import annotations

from pathlib import Path

import pytest

from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.policy.path_policy import AuthorizedRoot

_PLAN_HASH = "sha256:" + "a" * 64


def _store(tmp_path: Path) -> FilesystemPlanStore:
    root = tmp_path / "plans"
    root.mkdir()
    return FilesystemPlanStore(AuthorizedRoot.create(root))


def test_plan_store_rejects_unknown_hash_without_path_escape(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    with pytest.raises(ExecutorError) as raised:
        store.load("../../plan")

    assert raised.value.code is ExecutorErrorCode.INVALID_PLAN


def test_plan_store_does_not_follow_plan_symlink(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outside = tmp_path / "outside-plan.json"
    outside.write_bytes(b"untrusted")
    (
        store.root.path / f"plan-v1-{'a' * 64}.json"
    ).symlink_to(outside)

    with pytest.raises(ExecutorError) as raised:
        store.load(_PLAN_HASH)

    assert raised.value.code is ExecutorErrorCode.PLAN_STORE_FAILURE
    assert outside.read_bytes() == b"untrusted"
