from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from reeloom.adapters.folder_journal import FilesystemFolderJournalStore
from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.executor.errors import (
    ExecutorError,
    ExecutorErrorCode,
    atomic_move_error_code,
)
from reeloom.executor.atomic_rename import rename_noreplace
from reeloom.executor.folder_transaction import FolderTransactionRecord
from reeloom.executor.preflight import FilesystemPreflightExecutor
from reeloom.kernel.folder_disposition import (
    FolderDispositionAction,
    FolderDispositionPlan,
)
from reeloom.kernel.naming import filesystem_name_key
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.watcher import FolderSnapshot, NoFollowWatcher


class FolderDispositionStore(Protocol):
    def claim(
        self,
        *,
        approval_id: str,
        run_id: str,
        plan_hash: str,
    ) -> object: ...

    def require_claim(
        self,
        *,
        approval_id: str,
        run_id: str,
        plan_hash: str,
    ) -> object: ...

    def begin_transaction(
        self, transaction: FolderTransactionRecord
    ) -> None: ...

    def mark_transaction(
        self,
        transaction: FolderTransactionRecord,
        *,
        status: str,
        failure_code: ExecutorErrorCode | None = None,
    ) -> None: ...

    def settle(
        self,
        transaction: FolderTransactionRecord,
        *,
        run_id: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class FolderDispositionResult:
    run_id: str
    plan_hash: str
    approval_id: str
    transaction_id: str
    action: FolderDispositionAction
    target_relative: str | None
    status: str = "completed"


@dataclass(frozen=True, slots=True)
class FolderDispositionExecutor:
    plans: FilesystemPlanStore
    approvals: FolderDispositionStore
    journals: FilesystemFolderJournalStore
    watcher: NoFollowWatcher = NoFollowWatcher()

    def apply(
        self, *, plan_hash: str, approval_id: str
    ) -> FolderDispositionResult:
        plan = self._load(plan_hash)
        self.approvals.claim(
            approval_id=approval_id,
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
        )
        transaction = FolderTransactionRecord.create(
            plan, approval_id=approval_id
        )
        self.approvals.begin_transaction(transaction)
        return self._resume(plan, transaction)

    def recover(
        self, *, plan_hash: str, approval_id: str
    ) -> FolderDispositionResult:
        plan = self._load(plan_hash)
        self.approvals.require_claim(
            approval_id=approval_id,
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
        )
        transaction = FolderTransactionRecord.create(
            plan, approval_id=approval_id
        )
        self.approvals.begin_transaction(transaction)
        return self._resume(plan, transaction)

    def _resume(
        self,
        plan: FolderDispositionPlan,
        transaction: FolderTransactionRecord,
    ) -> FolderDispositionResult:
        with self.journals.transaction_lock(transaction):
            self.journals.begin(transaction)
            completed = self.journals.is_completed(transaction)
            rolled_back = self.journals.is_rolled_back(transaction)
            if completed and rolled_back:
                raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
            if completed:
                self.approvals.settle(
                    transaction, run_id=plan.run_id
                )
                return self._result(plan, transaction)
            if rolled_back:
                self.approvals.mark_transaction(
                    transaction,
                    status="blocked",
                )
                raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
            try:
                self._execute(plan, transaction)
            except ExecutorError as error:
                self.approvals.mark_transaction(
                    transaction,
                    status=(
                        "blocked"
                        if error.code
                        in {
                            ExecutorErrorCode.SOURCE_DRIFT,
                            ExecutorErrorCode.DESTINATION_COLLISION,
                        }
                        else "recovery_required"
                    ),
                    failure_code=error.code,
                )
                raise
            self.journals.record_completed(transaction)
            self.approvals.settle(transaction, run_id=plan.run_id)
            return self._result(plan, transaction)

    def _execute(
        self,
        plan: FolderDispositionPlan,
        transaction: FolderTransactionRecord,
    ) -> None:
        root = AuthorizedRoot.create(Path(plan.source_root.path.as_posix()))
        if (
            root.device != plan.source_root.device
            or root.inode != plan.source_root.inode
        ):
            raise ExecutorError(ExecutorErrorCode.ROOT_DRIFT)
        target = self._effective_target(plan, transaction)
        source_state = self._entry_state(
            root,
            PurePosixPath(plan.source_folder),
            plan,
        )
        target_state = self._entry_state(
            root,
            target,
            plan,
        )
        source_exists = source_state == "expected"
        target_exists = target_state == "expected"
        if source_state == "other":
            raise ExecutorError(ExecutorErrorCode.STATE_AMBIGUOUS)
        if target_state == "other":
            raise ExecutorError(
                ExecutorErrorCode.DESTINATION_COLLISION
            )
        if not source_exists:
            if not target_exists:
                if (
                    plan.action is FolderDispositionAction.REMOVE_EMPTY
                    and self.journals.is_renamed(transaction)
                ):
                    return
                raise ExecutorError(ExecutorErrorCode.RECOVERY_REQUIRED)
            try:
                moved = self.watcher.scan_folder(
                    root,
                    target,
                    logical_name=plan.source_folder,
                )
            except Exception:
                self._rollback(root, target, plan, transaction)
                raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT) from None
            if self._inventory_id(moved, plan) != plan.inventory_id:
                self._rollback(root, target, plan, transaction)
                raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
            if not self.journals.is_renamed(transaction):
                self.journals.record_renamed(transaction)
                self.approvals.mark_transaction(
                    transaction, status="renamed"
                )
            self._finish_target(root, target, plan)
            return
        if target_exists:
            raise ExecutorError(ExecutorErrorCode.RECOVERY_REQUIRED)
        try:
            current = self.watcher.scan_folder(
                root,
                PurePosixPath(plan.source_folder),
                logical_name=plan.source_folder,
            )
        except Exception:
            raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT) from None
        if (
            current.device != plan.folder_device
            or current.inode != plan.folder_inode
            or self._inventory_id(current, plan) != plan.inventory_id
        ):
            raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)

        root_fd = FilesystemPreflightExecutor._open_bound_root(
            plan.source_root
        )
        bucket_fd: int | None = None
        try:
            bucket_fd = self._open_bucket(
                root_fd, target.parts[0]
            )
            target_key = filesystem_name_key(target.name)
            if any(
                filesystem_name_key(name) == target_key
                for name in os.listdir(bucket_fd)
            ):
                raise ExecutorError(
                    ExecutorErrorCode.DESTINATION_COLLISION
                )
            self.journals.record_started(transaction)
            failure_code: ExecutorErrorCode | None = None
            try:
                rename_noreplace(
                    root_fd,
                    plan.source_folder,
                    bucket_fd,
                    target.name,
                )
            except OSError as error:
                failure_code = atomic_move_error_code(error)
            moved = self._reconcile_forward_move(
                root,
                target,
                plan,
                failure_code=failure_code,
            )
            if not moved:
                if failure_code is None:
                    raise ExecutorError(
                        ExecutorErrorCode.STATE_AMBIGUOUS
                    )
                raise ExecutorError(failure_code)
            try:
                os.fsync(root_fd)
                os.fsync(bucket_fd)
            except OSError:
                raise ExecutorError(
                    ExecutorErrorCode.STATE_AMBIGUOUS
                ) from None
        finally:
            if bucket_fd is not None:
                os.close(bucket_fd)
            os.close(root_fd)
        if not self._matches(root, target, plan):
            self._rollback(root, target, plan, transaction)
            raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
        self.journals.record_renamed(transaction)
        self.approvals.mark_transaction(transaction, status="renamed")
        try:
            moved = self.watcher.scan_folder(
                root,
                target,
                logical_name=plan.source_folder,
            )
        except Exception:
            self._rollback(root, target, plan, transaction)
            raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT) from None
        if self._inventory_id(moved, plan) != plan.inventory_id:
            self._rollback(root, target, plan, transaction)
            raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
        self._finish_target(root, target, plan)

    @staticmethod
    def _inventory_id(
        snapshot: FolderSnapshot,
        plan: FolderDispositionPlan,
    ) -> str:
        if plan.media_plan_hash is None:
            return snapshot.inventory_id
        return snapshot.disposition_inventory_id

    def _finish_target(
        self,
        root: AuthorizedRoot,
        target: PurePosixPath,
        plan: FolderDispositionPlan,
    ) -> None:
        if plan.action is not FolderDispositionAction.REMOVE_EMPTY:
            return
        root_fd = FilesystemPreflightExecutor._open_bound_root(
            plan.source_root
        )
        bucket_fd: int | None = None
        try:
            bucket_fd = self._open_bucket(root_fd, target.parts[0])
            try:
                os.rmdir(target.name, dir_fd=bucket_fd)
                os.fsync(bucket_fd)
            except FileNotFoundError:
                return
            except OSError:
                raise ExecutorError(
                    ExecutorErrorCode.RECOVERY_REQUIRED
                ) from None
        finally:
            if bucket_fd is not None:
                os.close(bucket_fd)
            os.close(root_fd)

    def _rollback(
        self,
        root: AuthorizedRoot,
        target: PurePosixPath,
        plan: FolderDispositionPlan,
        transaction: FolderTransactionRecord,
    ) -> None:
        root_fd = FilesystemPreflightExecutor._open_bound_root(
            plan.source_root
        )
        bucket_fd: int | None = None
        try:
            bucket_fd = self._open_bucket(root_fd, target.parts[0])
            failure_code: ExecutorErrorCode | None = None
            try:
                rename_noreplace(
                    bucket_fd,
                    target.name,
                    root_fd,
                    plan.source_folder,
                )
            except OSError as error:
                failure_code = atomic_move_error_code(error)
            still_forward = self._reconcile_forward_move(
                root,
                target,
                plan,
                failure_code=failure_code,
            )
            if still_forward:
                if failure_code is None:
                    raise ExecutorError(
                        ExecutorErrorCode.STATE_AMBIGUOUS
                    )
                raise ExecutorError(failure_code)
            os.fsync(bucket_fd)
            os.fsync(root_fd)
            self.journals.record_rolled_back(transaction)
        except Exception:
            raise ExecutorError(ExecutorErrorCode.RECOVERY_REQUIRED) from None
        finally:
            if bucket_fd is not None:
                os.close(bucket_fd)
            os.close(root_fd)

    @staticmethod
    def _effective_target(
        plan: FolderDispositionPlan,
        transaction: FolderTransactionRecord,
    ) -> PurePosixPath:
        if plan.target_relative is not None:
            return plan.target_relative
        return PurePosixPath("archive") / (
            ".reeloom-empty-"
            + transaction.transaction_id.removeprefix("folder-txn-v1-")
        )

    @staticmethod
    def _matches(
        root: AuthorizedRoot,
        relative: PurePosixPath,
        plan: FolderDispositionPlan,
        *,
        missing_ok: bool = False,
    ) -> bool:
        state = FolderDispositionExecutor._entry_state(
            root,
            relative,
            plan,
        )
        if state == "absent" and not missing_ok:
            raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)
        return state == "expected"

    @staticmethod
    def _entry_state(
        root: AuthorizedRoot,
        relative: PurePosixPath,
        plan: FolderDispositionPlan,
    ) -> str:
        root_fd = FilesystemPreflightExecutor._open_bound_root(
            plan.source_root
        )
        current_fd = root_fd
        try:
            for part in relative.parts[:-1]:
                try:
                    next_fd = FilesystemPreflightExecutor._open_existing_directory(
                        current_fd,
                        part,
                        missing_code=ExecutorErrorCode.SOURCE_DRIFT,
                        nondirectory_code=ExecutorErrorCode.SOURCE_DRIFT,
                    )
                except ExecutorError:
                    return "absent"
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = next_fd
            try:
                metadata = os.stat(
                    relative.name,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return "absent"
            except OSError:
                return "other"
            return "expected" if (
                stat.S_ISDIR(metadata.st_mode)
                and metadata.st_dev == plan.folder_device
                and metadata.st_ino == plan.folder_inode
            ) else "other"
        finally:
            if current_fd != root_fd:
                os.close(current_fd)
            os.close(root_fd)

    @classmethod
    def _reconcile_forward_move(
        cls,
        root: AuthorizedRoot,
        target: PurePosixPath,
        plan: FolderDispositionPlan,
        *,
        failure_code: ExecutorErrorCode | None,
    ) -> bool:
        source_state = cls._entry_state(
            root,
            PurePosixPath(plan.source_folder),
            plan,
        )
        target_state = cls._entry_state(root, target, plan)
        if source_state == "absent" and target_state == "expected":
            return True
        if source_state == "expected" and target_state == "absent":
            return False
        if (
            failure_code is ExecutorErrorCode.DESTINATION_COLLISION
            and source_state == "expected"
            and target_state == "other"
        ):
            raise ExecutorError(
                ExecutorErrorCode.DESTINATION_COLLISION
            )
        raise ExecutorError(ExecutorErrorCode.STATE_AMBIGUOUS)

    @staticmethod
    def _open_bucket(root_fd: int, name: str) -> int:
        try:
            os.mkdir(name, mode=0o700, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileExistsError:
            pass
        try:
            metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ExecutorError(
                    ExecutorErrorCode.DESTINATION_COLLISION
                )
            opened = FilesystemPreflightExecutor._open_existing_directory(
                root_fd,
                name,
                missing_code=ExecutorErrorCode.DESTINATION_COLLISION,
                nondirectory_code=ExecutorErrorCode.DESTINATION_COLLISION,
            )
            after = os.fstat(opened)
            if (
                after.st_dev != metadata.st_dev
                or after.st_ino != metadata.st_ino
            ):
                os.close(opened)
                raise ExecutorError(ExecutorErrorCode.ROOT_DRIFT)
            return opened
        except ExecutorError:
            raise
        except OSError:
            raise ExecutorError(
                ExecutorErrorCode.DESTINATION_COLLISION
            ) from None

    @staticmethod
    def _load_result_target(
        plan: FolderDispositionPlan,
    ) -> str | None:
        return (
            None
            if plan.target_relative is None
            else plan.target_relative.as_posix()
        )

    def _load(self, plan_hash: str) -> FolderDispositionPlan:
        try:
            plan = FolderDispositionPlan.from_canonical_bytes(
                self.plans.load_folder_disposition(plan_hash)
            )
        except Exception:
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN) from None
        if plan.plan_hash != plan_hash:
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
        return plan

    def _result(
        self,
        plan: FolderDispositionPlan,
        transaction: FolderTransactionRecord,
    ) -> FolderDispositionResult:
        return FolderDispositionResult(
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
            approval_id=transaction.approval_id,
            transaction_id=transaction.transaction_id,
            action=plan.action,
            target_relative=self._load_result_target(plan),
        )
