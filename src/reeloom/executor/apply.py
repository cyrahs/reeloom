from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import StrEnum

from reeloom.executor.errors import (
    ApprovalError,
    ApprovalErrorCode,
    ExecutorError,
    ExecutorErrorCode,
    atomic_move_error_code,
    filesystem_error_code,
)
from reeloom.executor.atomic_rename import rename_noreplace_compatible
from reeloom.executor.manifest import (
    ExecutionManifest,
    ExecutionMove,
    ExecutionSource,
)
from reeloom.executor.preflight import FilesystemPreflightExecutor
from reeloom.executor.transaction import TransactionRecord
from reeloom.kernel.approval import ApprovalScope
from reeloom.kernel.naming import filesystem_name_key
from reeloom.ports.approvals import ApprovalStore
from reeloom.ports.journals import (
    JournalStore,
    JournalTerminalSummary,
)
from reeloom.ports.plans import PlanStore

_rename_noreplace = rename_noreplace_compatible


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
        try:
            self.approvals.require_claim(
                approval_id=approval_id,
                run_id=manifest.run_id,
                plan_hash=plan_hash,
                scope=ApprovalScope.APPLY,
            )
        except ApprovalError as error:
            if error.code is not ApprovalErrorCode.NOT_FOUND:
                raise
        else:
            return self.recover(
                plan_hash=plan_hash,
                approval_id=approval_id,
            )
        # A stale plan is safe to retire only while no approval has been
        # claimed. Validate once before creating durable transaction state,
        # then validate again after the claim to close the TOCTOU window.
        preflight.validate(manifest)
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
            try:
                preflight.validate(manifest)
            except ExecutorError as error:
                if error.code is not ExecutorErrorCode.SOURCE_DRIFT:
                    raise
                self.journals.record_failure(transaction, error.code)
                self.journals.record_rolled_back(transaction)
                return self._terminal_result(
                    manifest,
                    transaction,
                    approval_id=approval_id,
                )
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
        bound_destination_fd: int | None = None
        try:
            try:
                if manifest.required_absent_directory is not None:
                    bound_destination_fd = (
                        self._create_required_directory(manifest)
                    )
                for move in manifest.moves:
                    if bound_destination_fd is not None:
                        self._require_bound_directory(
                            manifest,
                            bound_destination_fd,
                        )
                    self._apply_move(
                        manifest,
                        move,
                        bound_destination_fd=bound_destination_fd,
                    )
                    applied.append(move)
                    self.journals.record_move(
                        transaction,
                        move.source_id,
                    )
                    if bound_destination_fd is not None:
                        self._require_bound_directory(
                            manifest,
                            bound_destination_fd,
                        )
                if bound_destination_fd is not None:
                    self._require_bound_directory(
                        manifest,
                        bound_destination_fd,
                    )
            except ExecutorError as error:
                if error.code is ExecutorErrorCode.STATE_AMBIGUOUS:
                    self._record_failure_once(
                        transaction,
                        manifest,
                        error.code,
                    )
                    raise ExecutorError(
                        ExecutorErrorCode.RECOVERY_REQUIRED
                    ) from None
                if error.code in {
                    ExecutorErrorCode.ATOMIC_MOVE_UNSUPPORTED,
                    ExecutorErrorCode.PERMISSION_DENIED,
                    ExecutorErrorCode.RECOVERY_REQUIRED,
                    ExecutorErrorCode.TRANSIENT_IO,
                } and (
                    not applied
                    or error.code is ExecutorErrorCode.RECOVERY_REQUIRED
                ):
                    if (
                        not applied
                        and bound_destination_fd is not None
                        and error.code
                        in {
                            ExecutorErrorCode.ATOMIC_MOVE_UNSUPPORTED,
                            ExecutorErrorCode.PERMISSION_DENIED,
                            ExecutorErrorCode.TRANSIENT_IO,
                        }
                    ):
                        try:
                            self._remove_created_directory(
                                manifest,
                                bound_destination_fd,
                            )
                        except ExecutorError:
                            self._record_failure_once(
                                transaction,
                                manifest,
                                ExecutorErrorCode.STATE_AMBIGUOUS,
                            )
                            raise ExecutorError(
                                ExecutorErrorCode.RECOVERY_REQUIRED
                            ) from None
                    self._record_failure_once(
                        transaction,
                        manifest,
                        error.code,
                    )
                    raise
                return self._rollback_after_failure(
                    manifest,
                    transaction,
                    applied,
                    error,
                    bound_destination_fd=bound_destination_fd,
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
        finally:
            if bound_destination_fd is not None:
                os.close(bound_destination_fd)

    @staticmethod
    def _create_required_directory(
        manifest: ExecutionManifest,
    ) -> int:
        directory = manifest.required_absent_directory
        if (
            directory is None
            or directory.is_absolute()
            or len(directory.parts) != 1
        ):
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
        root_fd = FilesystemPreflightExecutor._open_bound_root(
            manifest.output_root
        )
        try:
            FilesystemPreflightExecutor._check_required_absent_directory(
                root_fd,
                directory,
            )
            try:
                os.mkdir(directory.name, 0o755, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                try:
                    metadata = os.stat(
                        directory.name,
                        dir_fd=root_fd,
                        follow_symlinks=False,
                    )
                except OSError:
                    raise ExecutorError(
                        ExecutorErrorCode.MOVE_FAILED
                    ) from None
                if stat.S_ISLNK(metadata.st_mode):
                    raise ExecutorError(
                        ExecutorErrorCode.SYMLINK_NOT_ALLOWED
                    )
                raise ExecutorError(
                    ExecutorErrorCode.DESTINATION_COLLISION
                ) from None
            except OSError as error:
                raise ExecutorError(
                    filesystem_error_code(error)
                ) from None
            return FilesystemPreflightExecutor._open_existing_directory(
                root_fd,
                directory.name,
                missing_code=ExecutorErrorCode.MOVE_FAILED,
                nondirectory_code=(
                    ExecutorErrorCode.DESTINATION_COLLISION
                ),
            )
        finally:
            os.close(root_fd)

    @staticmethod
    def _require_bound_directory(
        manifest: ExecutionManifest,
        directory_fd: int,
    ) -> None:
        directory = manifest.required_absent_directory
        if (
            directory is None
            or directory.is_absolute()
            or len(directory.parts) != 1
        ):
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
        root_fd = FilesystemPreflightExecutor._open_bound_root(
            manifest.output_root
        )
        try:
            try:
                matching = tuple(
                    entry
                    for entry in os.listdir(root_fd)
                    if filesystem_name_key(entry)
                    == filesystem_name_key(directory.name)
                )
                if matching != (directory.name,):
                    raise ExecutorError(
                        ExecutorErrorCode.DESTINATION_COLLISION
                    )
                current = os.stat(
                    directory.name,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
                bound = os.fstat(directory_fd)
            except OSError:
                raise ExecutorError(
                    ExecutorErrorCode.DESTINATION_COLLISION
                ) from None
            if stat.S_ISLNK(current.st_mode):
                raise ExecutorError(
                    ExecutorErrorCode.SYMLINK_NOT_ALLOWED
                )
            if (
                not stat.S_ISDIR(current.st_mode)
                or not stat.S_ISDIR(bound.st_mode)
                or current.st_dev != bound.st_dev
                or current.st_ino != bound.st_ino
            ):
                raise ExecutorError(
                    ExecutorErrorCode.DESTINATION_COLLISION
                )
        finally:
            os.close(root_fd)

    @classmethod
    def _remove_created_directory(
        cls,
        manifest: ExecutionManifest,
        directory_fd: int,
    ) -> None:
        directory = manifest.required_absent_directory
        if directory is None:
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
        cls._require_bound_directory(manifest, directory_fd)
        try:
            if os.listdir(directory_fd):
                raise ExecutorError(
                    ExecutorErrorCode.STATE_AMBIGUOUS
                )
        except OSError:
            raise ExecutorError(
                ExecutorErrorCode.STATE_AMBIGUOUS
            ) from None
        root_fd = FilesystemPreflightExecutor._open_bound_root(
            manifest.output_root
        )
        try:
            os.rmdir(directory.name, dir_fd=root_fd)
            os.fsync(root_fd)
        except OSError:
            raise ExecutorError(
                ExecutorErrorCode.STATE_AMBIGUOUS
            ) from None
        finally:
            os.close(root_fd)

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
        if (
            summary.applied_count == 0
            and summary.failure_code
            in {
                ExecutorErrorCode.ATOMIC_MOVE_UNSUPPORTED,
                ExecutorErrorCode.PERMISSION_DENIED,
                ExecutorErrorCode.TRANSIENT_IO,
            }
        ):
            FilesystemPreflightExecutor(
                plans=self.plans
            ).validate(manifest)
            return self._apply_locked(
                manifest,
                transaction,
                approval_id=approval_id,
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

    def _record_failure_once(
        self,
        transaction: TransactionRecord,
        manifest: ExecutionManifest,
        code: ExecutorErrorCode,
    ) -> None:
        summary = self.journals.terminal_summary(
            transaction,
            tuple(move.source_id for move in manifest.moves),
        )
        if summary.failure_code is None:
            self.journals.record_failure(transaction, code)

    def _rollback_after_failure(
        self,
        manifest: ExecutionManifest,
        transaction: TransactionRecord,
        applied: list[ExecutionMove],
        failure: ExecutorError,
        *,
        bound_destination_fd: int | None = None,
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
                bound_destination_fd=bound_destination_fd,
            )
            raise ExecutorError(
                ExecutorErrorCode.RECOVERY_REQUIRED
            ) from None

        self._best_effort_rollback(
            manifest,
            transaction,
            applied,
            bound_destination_fd=bound_destination_fd,
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
        *,
        bound_destination_fd: int | None = None,
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
                    bound_destination_fd=bound_destination_fd,
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
        *,
        bound_destination_fd: int | None = None,
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
            if bound_destination_fd is None:
                output_root_fd = (
                    FilesystemPreflightExecutor._open_bound_root(
                        manifest.output_root
                    )
                )
                destination_parent_fd = cls._open_destination_parent(
                    output_root_fd,
                    move,
                    source,
                )
            else:
                destination_parent_fd = (
                    cls._duplicate_bound_destination_parent(
                        manifest,
                        move,
                        source,
                        bound_destination_fd,
                    )
                )
            source_parent_fd = cls._open_source_parent(
                source_root_fd,
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
            failure_code: ExecutorErrorCode | None = None
            try:
                _rename_noreplace(
                    source_parent_fd,
                    source.relative_path.name,
                    destination_parent_fd,
                    move.destination.name,
                )
            except OSError as error:
                failure_code = atomic_move_error_code(error)
            moved = cls._reconcile_forward_move(
                source_parent_fd,
                source.relative_path.name,
                destination_parent_fd,
                move.destination.name,
                source,
                failure_code=failure_code,
            )
            if not moved:
                if failure_code is None:
                    raise ExecutorError(
                        ExecutorErrorCode.STATE_AMBIGUOUS
                    )
                raise ExecutorError(failure_code)
            try:
                os.fsync(source_parent_fd)
                os.fsync(destination_parent_fd)
            except OSError:
                raise ExecutorError(
                    ExecutorErrorCode.STATE_AMBIGUOUS
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
        bound_destination_fd: int | None = None,
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
            if bound_destination_fd is not None:
                destination_parent_fd = (
                    cls._duplicate_bound_destination_parent(
                        manifest,
                        move,
                        source,
                        bound_destination_fd,
                    )
                )
            else:
                output_root_fd = (
                    FilesystemPreflightExecutor._open_bound_root(
                        manifest.output_root
                    )
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
                    except OSError as error:
                        raise ExecutorError(
                            filesystem_error_code(error)
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

    @staticmethod
    def _duplicate_bound_destination_parent(
        manifest: ExecutionManifest,
        move: ExecutionMove,
        source: ExecutionSource,
        directory_fd: int,
    ) -> int:
        directory = manifest.required_absent_directory
        if (
            directory is None
            or move.destination.parts[:-1] != directory.parts
        ):
            raise ExecutorError(ExecutorErrorCode.INVALID_PLAN)
        duplicated: int | None = None
        try:
            duplicated = os.dup(directory_fd)
            metadata = os.fstat(duplicated)
        except OSError:
            if duplicated is not None:
                os.close(duplicated)
            raise ExecutorError(
                ExecutorErrorCode.MOVE_FAILED
            ) from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != source.device
        ):
            os.close(duplicated)
            raise ExecutorError(ExecutorErrorCode.CROSS_FILESYSTEM)
        return duplicated

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
    def _reconcile_forward_move(
        cls,
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
        source: ExecutionSource,
        *,
        failure_code: ExecutorErrorCode | None,
    ) -> bool:
        try:
            source_state = cls._source_state(
                source_parent_fd,
                source_name,
                source,
                moved=False,
            )
            destination_state = cls._source_state(
                destination_parent_fd,
                destination_name,
                source,
                moved=True,
            )
        except ExecutorError:
            raise ExecutorError(
                ExecutorErrorCode.STATE_AMBIGUOUS
            ) from None
        if source_state == "absent" and destination_state == "expected":
            return True
        if source_state == "expected" and destination_state == "absent":
            return False
        if (
            failure_code is ExecutorErrorCode.DESTINATION_COLLISION
            and source_state == "expected"
            and destination_state == "other"
        ):
            raise ExecutorError(
                ExecutorErrorCode.DESTINATION_COLLISION
            )
        raise ExecutorError(ExecutorErrorCode.STATE_AMBIGUOUS)

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
