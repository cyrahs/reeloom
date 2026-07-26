from __future__ import annotations

import ctypes
import errno
import os
import stat
from dataclasses import dataclass
from enum import StrEnum

from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.executor.manifest import (
    ExecutionManifest,
    ExecutionMove,
    ExecutionSource,
)
from reeloom.executor.preflight import FilesystemPreflightExecutor
from reeloom.executor.transaction import TransactionRecord
from reeloom.kernel.approval import ApprovalScope
from reeloom.ports.approvals import ApprovalStore
from reeloom.ports.journals import (
    JournalStore,
    JournalTerminalSummary,
)
from reeloom.ports.plans import PlanStore

_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
_RENAMEATX_NP = getattr(_LIBC, "renameatx_np", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEAT2.restype = ctypes.c_int
if _RENAMEATX_NP is not None:
    _RENAMEATX_NP.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEATX_NP.restype = ctypes.c_int


def _rename_noreplace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
) -> None:
    """Atomically rename without replacing any existing destination."""

    if _RENAMEAT2 is not None:
        result = _RENAMEAT2(
            source_parent_fd,
            os.fsencode(source_name),
            destination_parent_fd,
            os.fsencode(destination_name),
            _RENAME_NOREPLACE,
        )
    elif _RENAMEATX_NP is not None:
        result = _RENAMEATX_NP(
            source_parent_fd,
            os.fsencode(source_name),
            destination_parent_fd,
            os.fsencode(destination_name),
            _RENAME_EXCL,
        )
    else:
        raise OSError(errno.ENOSYS, "exclusive rename unavailable")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


class ApplyStatus(StrEnum):
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, slots=True)
class ApplyResult:
    transaction_id: str
    plan_hash: str
    approval_id: str
    status: ApplyStatus
    applied_count: int
    rolled_back_count: int
    failure_code: ExecutorErrorCode | None


@dataclass(frozen=True, slots=True)
class FilesystemExecutor:
    """Approval-gated apply and rollback with no model dependencies."""

    plans: PlanStore
    approvals: ApprovalStore
    journals: JournalStore

    def apply(
        self,
        *,
        plan_hash: str,
        approval_id: str,
    ) -> ApplyResult:
        preflight = FilesystemPreflightExecutor(plans=self.plans)
        manifest = preflight.load(plan_hash)
        transaction = TransactionRecord.create(
            manifest,
            approval_id=approval_id,
        )
        with self.journals.transaction_lock(transaction):
            self.journals.begin(transaction)
            self.approvals.claim(
                approval_id=approval_id,
                run_id=manifest.run_id,
                plan_hash=manifest.plan_hash,
                scope=ApprovalScope.APPLY,
            )
            preflight.validate(manifest)
            return self._apply_locked(
                manifest,
                transaction,
                approval_id=approval_id,
            )

    def _apply_locked(
        self,
        manifest: ExecutionManifest,
        transaction: TransactionRecord,
        *,
        approval_id: str,
    ) -> ApplyResult:
        applied: list[ExecutionMove] = []
        try:
            for move in manifest.moves:
                self._apply_move(manifest, move)
                applied.append(move)
                self.journals.record_move(
                    transaction,
                    move.source_id,
                )
        except ExecutorError as error:
            if error.code is ExecutorErrorCode.RECOVERY_REQUIRED:
                raise
            return self._rollback_after_failure(
                manifest,
                transaction,
                applied,
                error,
            )
        try:
            self.journals.record_completed(transaction)
        except ExecutorError:
            raise ExecutorError(
                ExecutorErrorCode.RECOVERY_REQUIRED
            ) from None

        return self._terminal_result(
            manifest,
            transaction,
            approval_id=approval_id,
        )

    def recover(
        self,
        *,
        plan_hash: str,
        approval_id: str,
    ) -> ApplyResult:
        loader = FilesystemPreflightExecutor(plans=self.plans)
        manifest = loader.load(plan_hash)
        self.approvals.require_claim(
            approval_id=approval_id,
            run_id=manifest.run_id,
            plan_hash=manifest.plan_hash,
            scope=ApprovalScope.APPLY,
        )
        transaction = TransactionRecord.create(
            manifest,
            approval_id=approval_id,
        )
        with self.journals.transaction_lock(transaction):
            return self._recover_locked(
                manifest,
                transaction,
                approval_id=approval_id,
            )

    def _recover_locked(
        self,
        manifest: ExecutionManifest,
        transaction: TransactionRecord,
        *,
        approval_id: str,
    ) -> ApplyResult:
        self.journals.require(transaction)
        if (
            self.journals.is_completed(transaction)
            and self.journals.is_rolled_back(transaction)
        ):
            raise ExecutorError(ExecutorErrorCode.RECOVERY_REQUIRED)
        summary = self.journals.terminal_summary(
            transaction,
            tuple(move.source_id for move in manifest.moves),
        )
        if summary.completed or summary.rolled_back:
            return self._result_from_summary(
                manifest,
                transaction,
                approval_id=approval_id,
                summary=summary,
            )

        try:
            for move in reversed(manifest.moves):
                allow_restored = (
                    self.journals.is_rollback_started(
                        transaction,
                        move.source_id,
                    )
                )
                self.journals.record_rollback_started(
                    transaction,
                    move.source_id,
                )
                if self._rollback_move(
                    manifest,
                    move,
                    allow_restored=allow_restored,
                ):
                    self.journals.record_rollback(
                        transaction,
                        move.source_id,
                    )
            self.journals.record_rolled_back(transaction)
        except (ExecutorError, OSError):
            raise ExecutorError(
                ExecutorErrorCode.RECOVERY_REQUIRED
            ) from None
        return self._terminal_result(
            manifest,
            transaction,
            approval_id=approval_id,
        )

    def _rollback_after_failure(
        self,
        manifest: ExecutionManifest,
        transaction: TransactionRecord,
        applied: list[ExecutionMove],
        failure: ExecutorError,
    ) -> ApplyResult:
        try:
            self.journals.record_failure(
                transaction,
                failure.code,
            )
        except ExecutorError:
            self._best_effort_rollback(
                manifest,
                transaction,
                applied,
            )
            raise ExecutorError(
                ExecutorErrorCode.RECOVERY_REQUIRED
            ) from None

        self._best_effort_rollback(
            manifest,
            transaction,
            applied,
        )
        try:
            self.journals.record_rolled_back(transaction)
        except ExecutorError:
            raise ExecutorError(
                ExecutorErrorCode.RECOVERY_REQUIRED
            ) from None
        return self._terminal_result(
            manifest,
            transaction,
            approval_id=transaction.approval_id,
        )

    def _terminal_result(
        self,
        manifest: ExecutionManifest,
        transaction: TransactionRecord,
        *,
        approval_id: str,
    ) -> ApplyResult:
        summary = self.journals.terminal_summary(
            transaction,
            tuple(move.source_id for move in manifest.moves),
        )
        if not (summary.completed or summary.rolled_back):
            raise ExecutorError(ExecutorErrorCode.RECOVERY_REQUIRED)
        return self._result_from_summary(
            manifest,
            transaction,
            approval_id=approval_id,
            summary=summary,
        )

    @staticmethod
    def _result_from_summary(
        manifest: ExecutionManifest,
        transaction: TransactionRecord,
        *,
        approval_id: str,
        summary: JournalTerminalSummary,
    ) -> ApplyResult:
        return ApplyResult(
            transaction_id=transaction.transaction_id,
            plan_hash=manifest.plan_hash,
            approval_id=approval_id,
            status=(
                ApplyStatus.COMPLETED
                if summary.completed
                else ApplyStatus.ROLLED_BACK
            ),
            applied_count=summary.applied_count,
            rolled_back_count=summary.rolled_back_count,
            failure_code=summary.failure_code,
        )

    def _best_effort_rollback(
        self,
        manifest: ExecutionManifest,
        transaction: TransactionRecord,
        applied: list[ExecutionMove],
    ) -> int:
        rolled_back = 0
        try:
            for move in reversed(applied):
                self.journals.record_rollback_started(
                    transaction,
                    move.source_id,
                )
                if self._rollback_move(
                    manifest,
                    move,
                    allow_restored=False,
                ):
                    rolled_back += 1
                    self.journals.record_rollback(
                        transaction,
                        move.source_id,
                    )
            return rolled_back
        except (ExecutorError, OSError):
            raise ExecutorError(
                ExecutorErrorCode.RECOVERY_REQUIRED
            ) from None

    @classmethod
    def _apply_move(
        cls,
        manifest: ExecutionManifest,
        move: ExecutionMove,
    ) -> None:
        source = cls._source_for(manifest, move)
        source_root_fd = (
            FilesystemPreflightExecutor._open_bound_root(
                manifest.source_root
            )
        )
        output_root_fd: int | None = None
        source_parent_fd: int | None = None
        destination_parent_fd: int | None = None
        try:
            output_root_fd = (
                FilesystemPreflightExecutor._open_bound_root(
                    manifest.output_root
                )
            )
            source_parent_fd = cls._open_source_parent(
                source_root_fd,
                source,
            )
            destination_parent_fd = cls._open_destination_parent(
                output_root_fd,
                move,
                source,
            )
            FilesystemPreflightExecutor._check_source_file(
                source_parent_fd,
                source.relative_path.name,
                source,
            )
            cls._require_absent(
                destination_parent_fd,
                move.destination.name,
                collision_code=(
                    ExecutorErrorCode.DESTINATION_COLLISION
                ),
            )
            try:
                _rename_noreplace(
                    source_parent_fd,
                    source.relative_path.name,
                    destination_parent_fd,
                    move.destination.name,
                )
            except FileExistsError:
                raise ExecutorError(
                    ExecutorErrorCode.DESTINATION_COLLISION
                ) from None
            except OSError as error:
                if error.errno == errno.EXDEV:
                    raise ExecutorError(
                        ExecutorErrorCode.CROSS_FILESYSTEM
                    ) from None
                raise ExecutorError(
                    ExecutorErrorCode.MOVE_FAILED
                ) from None
            try:
                cls._require_absent(
                    source_parent_fd,
                    source.relative_path.name,
                    collision_code=ExecutorErrorCode.SOURCE_DRIFT,
                )
                cls._require_source(
                    destination_parent_fd,
                    move.destination.name,
                    source,
                )
            except ExecutorError:
                raise ExecutorError(
                    ExecutorErrorCode.RECOVERY_REQUIRED
                ) from None
            try:
                os.fsync(source_parent_fd)
                os.fsync(destination_parent_fd)
            except OSError:
                cls._restore_unjournaled_move(
                    source_parent_fd,
                    source.relative_path.name,
                    destination_parent_fd,
                    move.destination.name,
                    source,
                )
                raise ExecutorError(
                    ExecutorErrorCode.MOVE_FAILED
                ) from None
        finally:
            for file_descriptor in (
                destination_parent_fd,
                source_parent_fd,
                output_root_fd,
                source_root_fd,
            ):
                if file_descriptor is not None:
                    os.close(file_descriptor)

    @classmethod
    def _rollback_move(
        cls,
        manifest: ExecutionManifest,
        move: ExecutionMove,
        *,
        allow_restored: bool,
    ) -> bool:
        source = cls._source_for(manifest, move)
        source_root_fd = (
            FilesystemPreflightExecutor._open_bound_root(
                manifest.source_root
            )
        )
        output_root_fd: int | None = None
        source_parent_fd: int | None = None
        destination_parent_fd: int | None = None
        try:
            output_root_fd = (
                FilesystemPreflightExecutor._open_bound_root(
                    manifest.output_root
                )
            )
            source_parent_fd = cls._open_source_parent(
                source_root_fd,
                source,
            )
            source_state = cls._source_state(
                source_parent_fd,
                source.relative_path.name,
                source,
                moved=False,
            )
            if source_state == "other" and allow_restored:
                source_state = cls._source_state(
                    source_parent_fd,
                    source.relative_path.name,
                    source,
                    moved=True,
                )
            try:
                destination_parent_fd = cls._open_existing_parent(
                    output_root_fd,
                    move.destination.parts[:-1],
                )
            except FileNotFoundError:
                if source_state == "expected":
                    return False
                raise ExecutorError(
                    ExecutorErrorCode.RECOVERY_REQUIRED
                ) from None
            destination_state = cls._source_state(
                destination_parent_fd,
                move.destination.name,
                source,
                moved=True,
            )
            if source_state == "expected" and destination_state == "absent":
                return False
            if source_state != "absent" or destination_state != "expected":
                raise ExecutorError(
                    ExecutorErrorCode.RECOVERY_REQUIRED
                )
            try:
                _rename_noreplace(
                    destination_parent_fd,
                    move.destination.name,
                    source_parent_fd,
                    source.relative_path.name,
                )
                os.fsync(source_parent_fd)
                os.fsync(destination_parent_fd)
            except OSError:
                raise ExecutorError(
                    ExecutorErrorCode.RECOVERY_REQUIRED
                ) from None
            cls._require_source(
                source_parent_fd,
                source.relative_path.name,
                source,
            )
            cls._require_absent(
                destination_parent_fd,
                move.destination.name,
                collision_code=ExecutorErrorCode.RECOVERY_REQUIRED,
            )
            return True
        finally:
            for file_descriptor in (
                destination_parent_fd,
                source_parent_fd,
                output_root_fd,
                source_root_fd,
            ):
                if file_descriptor is not None:
                    os.close(file_descriptor)

    @staticmethod
    def _source_for(
        manifest: ExecutionManifest,
        move: ExecutionMove,
    ) -> ExecutionSource:
        for source in manifest.sources:
            if source.candidate_id == move.source_id:
                return source
        raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)

    @classmethod
    def _open_source_parent(
        cls,
        root_fd: int,
        source: ExecutionSource,
    ) -> int:
        return cls._open_existing_parent(
            root_fd,
            source.relative_path.parts[:-1],
            missing_code=ExecutorErrorCode.SOURCE_DRIFT,
        )

    @classmethod
    def _open_existing_parent(
        cls,
        root_fd: int,
        parts: tuple[str, ...],
        *,
        missing_code: ExecutorErrorCode | None = None,
    ) -> int:
        try:
            current_fd = os.dup(root_fd)
        except OSError:
            raise ExecutorError(
                ExecutorErrorCode.PREFLIGHT_FAILED
            ) from None
        try:
            for part in parts:
                next_fd = (
                    FilesystemPreflightExecutor._open_existing_directory(
                        current_fd,
                        part,
                        missing_code=missing_code,
                        nondirectory_code=(
                            ExecutorErrorCode.PREFLIGHT_FAILED
                        ),
                    )
                )
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except Exception:
            os.close(current_fd)
            raise

    @classmethod
    def _open_destination_parent(
        cls,
        root_fd: int,
        move: ExecutionMove,
        source: ExecutionSource,
    ) -> int:
        try:
            current_fd = os.dup(root_fd)
        except OSError:
            raise ExecutorError(
                ExecutorErrorCode.MOVE_FAILED
            ) from None
        try:
            for part in move.destination.parts[:-1]:
                try:
                    next_fd = (
                        FilesystemPreflightExecutor
                        ._open_existing_directory(
                            current_fd,
                            part,
                            missing_code=None,
                            nondirectory_code=(
                                ExecutorErrorCode
                                .DESTINATION_COLLISION
                            ),
                        )
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(part, 0o755, dir_fd=current_fd)
                        os.fsync(current_fd)
                    except FileExistsError:
                        pass
                    except OSError:
                        raise ExecutorError(
                            ExecutorErrorCode.MOVE_FAILED
                        ) from None
                    next_fd = (
                        FilesystemPreflightExecutor
                        ._open_existing_directory(
                            current_fd,
                            part,
                            missing_code=(
                                ExecutorErrorCode.MOVE_FAILED
                            ),
                            nondirectory_code=(
                                ExecutorErrorCode
                                .DESTINATION_COLLISION
                            ),
                        )
                    )
                os.close(current_fd)
                current_fd = next_fd
            if os.fstat(current_fd).st_dev != source.device:
                raise ExecutorError(
                    ExecutorErrorCode.CROSS_FILESYSTEM
                )
            return current_fd
        except Exception:
            os.close(current_fd)
            raise

    @classmethod
    def _source_state(
        cls,
        parent_fd: int,
        name: str,
        source: ExecutionSource,
        *,
        moved: bool,
    ) -> str:
        try:
            metadata = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return "absent"
        except OSError:
            raise ExecutorError(
                ExecutorErrorCode.RECOVERY_REQUIRED
            ) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ExecutorError(
                ExecutorErrorCode.RECOVERY_REQUIRED
            )
        if (
            cls._matches_moved_source(metadata, source)
            if moved
            else FilesystemPreflightExecutor._matches_source(
                metadata,
                source,
            )
        ):
            return "expected"
        return "other"

    @classmethod
    def _require_source(
        cls,
        parent_fd: int,
        name: str,
        source: ExecutionSource,
    ) -> None:
        if (
            cls._source_state(
                parent_fd,
                name,
                source,
                moved=True,
            )
            != "expected"
        ):
            raise ExecutorError(ExecutorErrorCode.SOURCE_DRIFT)

    @staticmethod
    def _matches_moved_source(
        metadata: os.stat_result,
        source: ExecutionSource,
    ) -> bool:
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_size == source.size_bytes
            and metadata.st_dev == source.device
            and metadata.st_ino == source.inode
            and metadata.st_mtime_ns == source.mtime_ns
        )

    @staticmethod
    def _require_absent(
        parent_fd: int,
        name: str,
        *,
        collision_code: ExecutorErrorCode,
    ) -> None:
        try:
            metadata = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        except OSError:
            raise ExecutorError(
                ExecutorErrorCode.PREFLIGHT_FAILED
            ) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ExecutorError(
                ExecutorErrorCode.SYMLINK_NOT_ALLOWED
            )
        raise ExecutorError(collision_code)

    @classmethod
    def _restore_unjournaled_move(
        cls,
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
        source: ExecutionSource,
    ) -> None:
        try:
            if (
                cls._source_state(
                    destination_parent_fd,
                    destination_name,
                    source,
                    moved=True,
                )
                != "expected"
            ):
                raise ExecutorError(
                    ExecutorErrorCode.RECOVERY_REQUIRED
                )
            _rename_noreplace(
                destination_parent_fd,
                destination_name,
                source_parent_fd,
                source_name,
            )
            os.fsync(source_parent_fd)
            os.fsync(destination_parent_fd)
            cls._require_source(
                source_parent_fd,
                source_name,
                source,
            )
            cls._require_absent(
                destination_parent_fd,
                destination_name,
                collision_code=(
                    ExecutorErrorCode.RECOVERY_REQUIRED
                ),
            )
        except ExecutorError:
            raise
        except OSError:
            raise ExecutorError(
                ExecutorErrorCode.RECOVERY_REQUIRED
            ) from None
