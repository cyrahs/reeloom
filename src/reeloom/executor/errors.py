from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


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
