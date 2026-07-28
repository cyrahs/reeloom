from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.kernel.folder_disposition import FolderDispositionPlan

_SCHEMA = "folder-transaction-v1"
_ID = re.compile(r"^folder-txn-v1-[0-9a-f]{64}$")


def _canonical(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _transaction_id(
    *, run_id: str, plan_hash: str, approval_id: str
) -> str:
    digest = hashlib.sha256(
        _canonical(
            {
                "approval_id": approval_id,
                "plan_hash": plan_hash,
                "run_id": run_id,
                "schema_version": _SCHEMA,
            }
        )
    ).hexdigest()
    return f"folder-txn-v1-{digest}"


@dataclass(frozen=True, slots=True)
class FolderTransactionRecord:
    transaction_id: str
    run_id: str
    plan_hash: str
    approval_id: str
    source_device: int
    source_inode: int

    @classmethod
    def create(
        cls,
        plan: FolderDispositionPlan,
        *,
        approval_id: str,
    ) -> FolderTransactionRecord:
        return cls(
            transaction_id=_transaction_id(
                run_id=plan.run_id,
                plan_hash=plan.plan_hash,
                approval_id=approval_id,
            ),
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
            approval_id=approval_id,
            source_device=plan.folder_device,
            source_inode=plan.folder_inode,
        )

    def verify_id(self) -> bool:
        return (
            _ID.fullmatch(self.transaction_id) is not None
            and hmac.compare_digest(
                self.transaction_id,
                _transaction_id(
                    run_id=self.run_id,
                    plan_hash=self.plan_hash,
                    approval_id=self.approval_id,
                ),
            )
        )

    def canonical_bytes(self) -> bytes:
        if not self.verify_id():
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        return _canonical(
            {
                "approval_id": self.approval_id,
                "plan_hash": self.plan_hash,
                "run_id": self.run_id,
                "schema_version": _SCHEMA,
                "source_device": self.source_device,
                "source_inode": self.source_inode,
                "transaction_id": self.transaction_id,
            }
        )

    def event_bytes(self, event_type: str) -> bytes:
        if (
            not self.verify_id()
            or event_type
            not in {
                "folder_rename_started",
                "folder_renamed",
                "folder_completed",
                "folder_rolled_back",
            }
        ):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        return _canonical(
            {
                "event_type": event_type,
                "schema_version": _SCHEMA,
                "transaction_id": self.transaction_id,
            }
        )
