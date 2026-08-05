from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

import pytest

from reeloom.adapters.subtitle_journal import (
    FilesystemSubtitleAcquisitionJournalStore,
)
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.executor.subtitle_transaction import (
    SubtitleAcquisitionTransactionRecord,
)
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.rename_plan import RootBinding
from reeloom.kernel.subtitle_acquisition import (
    InspectedSubtitleMember,
    SubtitleAcquisitionPlan,
    SubtitleArchiveFormat,
    SubtitleArchiveSetId,
    SubtitleArchiveSource,
    SubtitleArchiveVolume,
    SubtitleReleaseId,
)
from reeloom.policy.path_policy import AuthorizedRoot


def _plan() -> SubtitleAcquisitionPlan:
    archive_content = b"PK\x03\x04archive"
    subtitle_content = b"subtitle"
    archive_id = SubtitleArchiveSetId(1)
    return SubtitleAcquisitionPlan.create(
        run_id="run-m13-transaction",
        config_revision_id="config-1",
        created_at=datetime(2026, 8, 4, tzinfo=UTC),
        source_root=RootBinding(PurePosixPath("/media"), 1, 2),
        source_folder="release",
        source_folder_device=1,
        source_folder_inode=3,
        folder_generation_id="generation-1",
        candidate_snapshot_id="candidate-snapshot-v1:" + "a" * 64,
        tmdb_id=123,
        archives=(
            SubtitleArchiveSource(
                SubtitleReleaseId(1),
                archive_id,
                SubtitleArchiveFormat.ZIP,
                (1,),
                10081,
                95257,
                "b" * 64,
                (
                    SubtitleArchiveVolume(
                        1,
                        34768,
                        len(archive_content),
                        hashlib.sha256(archive_content).hexdigest(),
                    ),
                ),
            ),
        ),
        inspected_members=(
            InspectedSubtitleMember(
                archive_id,
                PurePosixPath("Subs/E01.ass"),
                len(subtitle_content),
                hashlib.sha256(subtitle_content).hexdigest(),
            ),
        ),
    )


def _approval(plan: SubtitleAcquisitionPlan) -> ApprovalRecord:
    return ApprovalRecord.create(
        run_id=plan.run_id,
        plan_hash=plan.plan_hash,
        scope=ApprovalScope.SUBTITLE_ACQUIRE,
        expires_at=datetime(2026, 8, 4, tzinfo=UTC) + timedelta(minutes=15),
        nonce="n" * 32,
    )


def _transaction() -> SubtitleAcquisitionTransactionRecord:
    plan = _plan()
    return SubtitleAcquisitionTransactionRecord.create(
        plan,
        approval_id=_approval(plan).approval_id,
    )


def test_subtitle_transaction_names_are_deterministic_and_plan_bound() -> None:
    plan = _plan()
    approval = _approval(plan)

    first = SubtitleAcquisitionTransactionRecord.create(
        plan,
        approval_id=approval.approval_id,
    )
    second = SubtitleAcquisitionTransactionRecord.create(
        plan,
        approval_id=approval.approval_id,
    )

    assert first == second
    assert first.verify(plan)
    assert first.staging_name.startswith(".reeloom-acquiring-")
    assert first.destination_name == plan.destination_directory.as_posix()
    assert b"E01.ass" not in first.canonical_bytes()


def test_subtitle_transaction_rejects_tampered_derived_names() -> None:
    transaction = _transaction()

    with pytest.raises(ExecutorError) as raised:
        replace(transaction, destination_name="chosen-by-agent").canonical_bytes()

    assert raised.value.code is ExecutorErrorCode.INVALID_JOURNAL


@pytest.mark.parametrize(
    ("event_type", "member_index", "failure_code"),
    (
        ("member_written", None, None),
        ("completed", 0, None),
        ("failed", None, None),
        ("published", None, "unexpected"),
    ),
)
def test_subtitle_transaction_event_schema_is_strict(
    event_type: str,
    member_index: int | None,
    failure_code: str | None,
) -> None:
    with pytest.raises(ExecutorError) as raised:
        _transaction().event_bytes(
            event_type,
            member_index=member_index,
            failure_code=failure_code,
        )
    assert raised.value.code is ExecutorErrorCode.INVALID_JOURNAL


def test_subtitle_journal_is_write_once_idempotent_and_member_granular(
    tmp_path,
) -> None:
    root = tmp_path / "journals"
    root.mkdir()
    store = FilesystemSubtitleAcquisitionJournalStore(
        AuthorizedRoot.create(root)
    )
    transaction = _transaction()

    with store.transaction_lock(transaction):
        store.begin(transaction)
        store.begin(transaction)
        store.record(transaction, "approval_claimed")
        store.record(transaction, "staging_create_started")
        store.record_staging(transaction, device=10, inode=20)
        store.record_member(transaction, 0)

    assert store.has(transaction, "approval_claimed")
    assert store.has(transaction, "staging_create_started")
    assert store.staging_identity(transaction) == (10, 20)
    assert store.has_member(transaction, 0)
    assert not store.has(transaction, "completed")


def test_subtitle_journal_detects_tampered_event(tmp_path) -> None:
    root = tmp_path / "journals"
    root.mkdir()
    store = FilesystemSubtitleAcquisitionJournalStore(
        AuthorizedRoot.create(root)
    )
    transaction = _transaction()
    store.record(transaction, "published")
    event_path = root / f"{transaction.transaction_id}.published.json"
    event_path.write_bytes(b"tampered")

    with pytest.raises(ExecutorError) as raised:
        store.has(transaction, "published")
    assert raised.value.code is ExecutorErrorCode.INVALID_JOURNAL


def test_subtitle_journal_never_follows_precreated_symlink(tmp_path) -> None:
    root = tmp_path / "journals"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    store = FilesystemSubtitleAcquisitionJournalStore(
        AuthorizedRoot.create(root)
    )
    transaction = _transaction()
    (root / f"{transaction.transaction_id}.journal.json").symlink_to(outside)

    with pytest.raises(ExecutorError) as raised:
        store.begin(transaction)
    assert raised.value.code in {
        ExecutorErrorCode.INVALID_JOURNAL,
        ExecutorErrorCode.JOURNAL_FAILURE,
    }
    assert outside.read_bytes() == b"outside"
