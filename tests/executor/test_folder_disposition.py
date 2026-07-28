from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from reeloom.adapters.folder_journal import FilesystemFolderJournalStore
from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.executor.folder_disposition import FolderDispositionExecutor
from reeloom.executor.folder_transaction import FolderTransactionRecord
from reeloom.kernel.folder_disposition import (
    FolderDispositionAction,
    FolderDispositionPlan,
)
from reeloom.kernel.rename_plan import RootBinding
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.watcher import NoFollowWatcher


class _Approvals:
    def __init__(self) -> None:
        self.claimed = False
        self.settled = False
        self.begun = 0
        self.statuses: list[str] = []

    def claim(self, **kwargs: object) -> object:
        del kwargs
        self.claimed = True
        return object()

    def require_claim(self, **kwargs: object) -> object:
        del kwargs
        return object()

    def begin_transaction(self, transaction: object) -> None:
        del transaction
        self.begun += 1

    def mark_transaction(
        self, transaction: object, *, status: str
    ) -> None:
        del transaction
        self.statuses.append(status)

    def settle(self, transaction: object, *, run_id: str) -> None:
        del transaction, run_id
        self.settled = True


def _executor(tmp_path: Path) -> tuple[
    FolderDispositionExecutor, FilesystemPlanStore, _Approvals
]:
    plan_root = tmp_path / "plans"
    journal_root = tmp_path / "journals"
    plan_root.mkdir()
    journal_root.mkdir()
    plans = FilesystemPlanStore(AuthorizedRoot.create(plan_root))
    approvals = _Approvals()
    return (
        FolderDispositionExecutor(
            plans=plans,
            approvals=approvals,
            journals=FilesystemFolderJournalStore(
                AuthorizedRoot.create(journal_root)
            ),
        ),
        plans,
        approvals,
    )


def _plan(
    watch: Path,
    *,
    action: FolderDispositionAction,
    target: PurePosixPath | None,
) -> FolderDispositionPlan:
    root = AuthorizedRoot.create(watch)
    snapshot = NoFollowWatcher().scan_folders(root).folders[0]
    return FolderDispositionPlan.create(
        run_id="run-test",
        folder_generation_id="folder-test",
        created_at=datetime(2026, 7, 27, tzinfo=UTC),
        source_root=RootBinding(
            PurePosixPath(root.path.as_posix()), root.device, root.inode
        ),
        source_folder=snapshot.name,
        folder_device=snapshot.device,
        folder_inode=snapshot.inode,
        inventory_id=snapshot.disposition_inventory_id,
        action=action,
        target_relative=target,
        media_plan_hash="sha256:" + "a" * 64,
        file_count=sum(
            item.kind.value != "directory" for item in snapshot.entries
        ),
        reason_code="media_completed",
    )


def test_archives_remaining_folder_atomically(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    source = watch / "Incoming"
    source.mkdir()
    (source / "extra.txt").write_text("leftover")
    executor, plans, approvals = _executor(tmp_path)
    plan = _plan(
        watch,
        action=FolderDispositionAction.ARCHIVE,
        target=PurePosixPath("archive/Incoming"),
    )
    plans.save_folder_disposition(plan)

    result = executor.apply(
        plan_hash=plan.plan_hash,
        approval_id="approval-test",
    )

    assert result.status == "completed"
    assert not source.exists()
    assert (watch / "archive" / "Incoming" / "extra.txt").is_file()
    assert approvals.claimed
    assert approvals.settled


def test_removes_only_verified_empty_folder(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    source = watch / "Incoming"
    source.mkdir()
    executor, plans, _ = _executor(tmp_path)
    plan = _plan(
        watch,
        action=FolderDispositionAction.REMOVE_EMPTY,
        target=None,
    )
    plans.save_folder_disposition(plan)

    executor.apply(
        plan_hash=plan.plan_hash,
        approval_id="approval-test",
    )

    assert not source.exists()
    assert (watch / "archive").is_dir()
    assert tuple((watch / "archive").iterdir()) == ()


def test_expected_directory_time_change_is_not_late_content(
    tmp_path: Path,
) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    source = watch / "Incoming"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (nested / "extra.txt").write_text("leftover")
    executor, plans, _ = _executor(tmp_path)
    plan = _plan(
        watch,
        action=FolderDispositionAction.ARCHIVE,
        target=PurePosixPath("archive/Incoming"),
    )
    (nested / "temporary").write_text("changes directory times")
    (nested / "temporary").unlink()
    plans.save_folder_disposition(plan)

    executor.apply(
        plan_hash=plan.plan_hash,
        approval_id="approval-test",
    )

    assert (watch / "archive" / "Incoming" / "nested" / "extra.txt").is_file()


def test_fixed_target_is_never_overwritten(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    source = watch / "Incoming"
    source.mkdir()
    (source / "extra.txt").write_text("new")
    occupied = watch / "archive" / "Incoming"
    occupied.mkdir(parents=True)
    (occupied / "extra.txt").write_text("old")
    executor, plans, _ = _executor(tmp_path)
    plan = _plan(
        watch,
        action=FolderDispositionAction.ARCHIVE,
        target=PurePosixPath("archive/Incoming"),
    )
    plans.save_folder_disposition(plan)

    with pytest.raises(ExecutorError) as raised:
        executor.apply(
            plan_hash=plan.plan_hash,
            approval_id="approval-test",
        )

    assert raised.value.code is ExecutorErrorCode.DESTINATION_COLLISION
    assert (source / "extra.txt").read_text() == "new"
    assert (occupied / "extra.txt").read_text() == "old"


def test_equivalent_target_name_is_a_collision(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    source = watch / "incoming"
    source.mkdir()
    (source / "extra.txt").write_text("new")
    (watch / "archive" / "Incoming").mkdir(parents=True)
    executor, plans, _ = _executor(tmp_path)
    plan = _plan(
        watch,
        action=FolderDispositionAction.ARCHIVE,
        target=PurePosixPath("archive/incoming"),
    )
    plans.save_folder_disposition(plan)

    with pytest.raises(ExecutorError) as raised:
        executor.apply(
            plan_hash=plan.plan_hash,
            approval_id="approval-test",
        )

    assert raised.value.code is ExecutorErrorCode.DESTINATION_COLLISION
    assert source.is_dir()


def test_recovery_recreates_missing_transaction_record(
    tmp_path: Path,
) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    source = watch / "Incoming"
    source.mkdir()
    (source / "extra.txt").write_text("leftover")
    executor, plans, approvals = _executor(tmp_path)
    plan = _plan(
        watch,
        action=FolderDispositionAction.ARCHIVE,
        target=PurePosixPath("archive/Incoming"),
    )
    plans.save_folder_disposition(plan)

    executor.recover(
        plan_hash=plan.plan_hash,
        approval_id="approval-test",
    )

    assert approvals.begun == 1
    assert approvals.settled
    assert (watch / "archive" / "Incoming" / "extra.txt").is_file()


def test_recovery_rolls_back_drifted_destination(
    tmp_path: Path,
) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    source = watch / "Incoming"
    source.mkdir()
    (source / "extra.txt").write_text("leftover")
    executor, plans, approvals = _executor(tmp_path)
    plan = _plan(
        watch,
        action=FolderDispositionAction.ARCHIVE,
        target=PurePosixPath("archive/Incoming"),
    )
    plans.save_folder_disposition(plan)
    target = watch / "archive" / "Incoming"
    target.parent.mkdir()
    source.rename(target)
    (target / "late.txt").write_text("late")

    with pytest.raises(ExecutorError) as raised:
        executor.recover(
            plan_hash=plan.plan_hash,
            approval_id="approval-test",
        )

    assert raised.value.code is ExecutorErrorCode.SOURCE_DRIFT
    assert (source / "late.txt").is_file()
    assert not target.exists()
    assert approvals.statuses[-1] == "blocked"


def test_rolled_back_journal_cannot_replay_approval(
    tmp_path: Path,
) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    source = watch / "Incoming"
    source.mkdir()
    (source / "extra.txt").write_text("leftover")
    executor, plans, approvals = _executor(tmp_path)
    plan = _plan(
        watch,
        action=FolderDispositionAction.ARCHIVE,
        target=PurePosixPath("archive/Incoming"),
    )
    plans.save_folder_disposition(plan)
    transaction = FolderTransactionRecord.create(
        plan,
        approval_id="approval-test",
    )
    executor.journals.begin(transaction)
    executor.journals.record_rolled_back(transaction)

    with pytest.raises(ExecutorError) as raised:
        executor.recover(
            plan_hash=plan.plan_hash,
            approval_id="approval-test",
        )

    assert raised.value.code is ExecutorErrorCode.SOURCE_DRIFT
    assert source.is_dir()
    assert approvals.statuses[-1] == "blocked"
