from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from pathlib import PurePosixPath

import pytest

from reeloom.adapters.journal import FilesystemJournalStore
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.executor.manifest import ExecutionManifest
from reeloom.executor.transaction import TransactionRecord
from reeloom.kernel.rename_plan import RootBinding
from reeloom.policy.path_policy import AuthorizedRoot


def _transaction() -> TransactionRecord:
    manifest = ExecutionManifest(
        plan_hash="sha256:" + "b" * 64,
        run_id="run-m6",
        source_root=RootBinding(
            path=PurePosixPath("/incoming"),
            device=1,
            inode=2,
        ),
        output_root=RootBinding(
            path=PurePosixPath("/anime"),
            device=1,
            inode=3,
        ),
        sources=(),
        moves=(),
    )
    return TransactionRecord.create(
        manifest,
        approval_id="approval-v1-" + "c" * 64,
    )


def _store(tmp_path: Path) -> FilesystemJournalStore:
    root = tmp_path / "journals"
    root.mkdir()
    return FilesystemJournalStore(AuthorizedRoot.create(root))


def test_journal_events_are_immutable_and_idempotent(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    transaction = _transaction()
    store.begin(transaction)

    store.record_completed(transaction)
    store.record_completed(transaction)

    assert store.is_completed(transaction)
    assert len(tuple(store.root.path.iterdir())) == 2
    store.begin(transaction)


def test_journal_rejects_tampered_header(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transaction = _transaction()
    (
        store.root.path
        / f"{transaction.transaction_id}.journal.json"
    ).write_bytes(b"tampered")

    with pytest.raises(ExecutorError) as raised:
        store.require(transaction)

    assert raised.value.code is ExecutorErrorCode.INVALID_JOURNAL


def test_journal_begin_does_not_follow_symlink(tmp_path: Path) -> None:
    store = _store(tmp_path)
    transaction = _transaction()
    outside = tmp_path / "outside-journal.json"
    outside.write_bytes(b"outside")
    (
        store.root.path
        / f"{transaction.transaction_id}.journal.json"
    ).symlink_to(outside)

    with pytest.raises(ExecutorError) as raised:
        store.begin(transaction)

    assert (
        raised.value.code
        is ExecutorErrorCode.JOURNAL_FAILURE
    )
    assert outside.read_bytes() == b"outside"


def test_transaction_lock_is_exclusive_and_released(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    transaction = _transaction()

    with store.transaction_lock(transaction):
        with pytest.raises(ExecutorError) as raised:
            with store.transaction_lock(transaction):
                pass

        assert (
            raised.value.code
            is ExecutorErrorCode.TRANSACTION_BUSY
        )

    with store.transaction_lock(transaction):
        pass


def test_transaction_lock_does_not_follow_symlink(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    transaction = _transaction()
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"outside")
    (
        store.root.path / f"{transaction.transaction_id}.lock"
    ).symlink_to(outside)

    with pytest.raises(ExecutorError) as raised:
        with store.transaction_lock(transaction):
            pass

    assert raised.value.code is ExecutorErrorCode.JOURNAL_FAILURE
    assert outside.read_bytes() == b"outside"


def test_transaction_id_is_bound_to_record_content() -> None:
    transaction = _transaction()
    mismatched = replace(
        transaction,
        transaction_id="txn-v1-" + "a" * 64,
    )

    with pytest.raises(ExecutorError) as raised:
        mismatched.canonical_bytes()

    assert raised.value.code is ExecutorErrorCode.INVALID_JOURNAL
