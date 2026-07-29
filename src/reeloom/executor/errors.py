from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType

from reeloom.executor.atomic_rename import (
    AtomicRenameFailure,
    classify_atomic_rename_error,
)


class ApprovalErrorCode(StrEnum):
    INVALID_RECORD = "invalid_record"
    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    BINDING_MISMATCH = "binding_mismatch"
    ALREADY_EXISTS = "already_exists"
    ALREADY_CLAIMED = "already_claimed"
    STORE_FAILURE = "store_failure"


class ApprovalError(RuntimeError):
    def __init__(
        self,
        code: ApprovalErrorCode,
        *,
        context: dict[str, object] | None = None,
    ) -> None:
        self.code = code
        self.context = MappingProxyType(dict(context or {}))
        super().__init__(code.value)


class ExecutorErrorCode(StrEnum):
    INVALID_PLAN = "invalid_plan"
    PLAN_NOT_FOUND = "plan_not_found"
    PLAN_ALREADY_EXISTS = "plan_already_exists"
    PLAN_STORE_FAILURE = "plan_store_failure"
    ROOT_DRIFT = "root_drift"
    SOURCE_DRIFT = "source_drift"
    DESTINATION_COLLISION = "destination_collision"
    SYMLINK_NOT_ALLOWED = "symlink_not_allowed"
    CROSS_FILESYSTEM = "cross_filesystem"
    PREFLIGHT_FAILED = "preflight_failed"
    TRANSACTION_BUSY = "transaction_busy"
    JOURNAL_NOT_FOUND = "journal_not_found"
    INVALID_JOURNAL = "invalid_journal"
    JOURNAL_FAILURE = "journal_failure"
    ATOMIC_MOVE_UNSUPPORTED = "atomic_move_unsupported"
    TRANSIENT_IO = "transient_io"
    STATE_AMBIGUOUS = "state_ambiguous"
    MOVE_FAILED = "move_failed"
    RECOVERY_REQUIRED = "recovery_required"


class ExecutorError(RuntimeError):
    def __init__(
        self,
        code: ExecutorErrorCode,
        *,
        context: dict[str, object] | None = None,
    ) -> None:
        self.code = code
        self.context = MappingProxyType(dict(context or {}))
        super().__init__(code.value)


def atomic_move_error_code(error: OSError) -> ExecutorErrorCode:
    failure = classify_atomic_rename_error(error)
    return {
        AtomicRenameFailure.COLLISION: (
            ExecutorErrorCode.DESTINATION_COLLISION
        ),
        AtomicRenameFailure.CROSS_FILESYSTEM: (
            ExecutorErrorCode.CROSS_FILESYSTEM
        ),
        AtomicRenameFailure.TRANSIENT_IO: ExecutorErrorCode.TRANSIENT_IO,
        AtomicRenameFailure.UNSUPPORTED: (
            ExecutorErrorCode.ATOMIC_MOVE_UNSUPPORTED
        ),
        AtomicRenameFailure.UNKNOWN: ExecutorErrorCode.MOVE_FAILED,
    }[failure]
