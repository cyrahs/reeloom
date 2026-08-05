from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.kernel.approval import ApprovalRecord
from reeloom.kernel.subtitle_acquisition import SubtitleAcquisitionPlan

CURRENT_SUBTITLE_TRANSACTION_SCHEMA_VERSION = "1"

_TRANSACTION_ID = re.compile(r"^subtitle-txn-v1-[0-9a-f]{64}$")
_PLAN_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVENT_TYPES = frozenset(
    {
        "approval_claimed",
        "downloads_verified",
        "staging_create_started",
        "staging_created",
        "member_written",
        "publish_started",
        "published",
        "completed",
        "failed",
    }
)


def _canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _transaction_id(
    *,
    run_id: str,
    plan_hash: str,
    approval_id: str,
) -> str:
    content = _canonical_json(
        {
            "approval_id": approval_id,
            "plan_hash": plan_hash,
            "run_id": run_id,
            "schema_version": CURRENT_SUBTITLE_TRANSACTION_SCHEMA_VERSION,
        }
    )
    return "subtitle-txn-v1-" + hashlib.sha256(content).hexdigest()


@dataclass(frozen=True, slots=True)
class SubtitleAcquisitionTransactionRecord:
    transaction_id: str
    run_id: str
    plan_hash: str
    approval_id: str
    source_folder_device: int
    source_folder_inode: int
    staging_name: str
    destination_name: str
    archive_count: int
    member_count: int

    @classmethod
    def create(
        cls,
        plan: SubtitleAcquisitionPlan,
        *,
        approval_id: str,
    ) -> SubtitleAcquisitionTransactionRecord:
        if (
            not isinstance(plan, SubtitleAcquisitionPlan)
            or not plan.verify_hash()
            or not ApprovalRecord.is_valid_id(approval_id)
        ):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        transaction_id = _transaction_id(
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
            approval_id=approval_id,
        )
        suffix = transaction_id.removeprefix("subtitle-txn-v1-")
        record = cls(
            transaction_id=transaction_id,
            run_id=plan.run_id,
            plan_hash=plan.plan_hash,
            approval_id=approval_id,
            source_folder_device=plan.source_folder_device,
            source_folder_inode=plan.source_folder_inode,
            staging_name=f".reeloom-acquiring-{suffix}",
            destination_name=plan.destination_directory.as_posix(),
            archive_count=len(plan.archives),
            member_count=len(plan.members),
        )
        if not record.verify(plan):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        return record

    def verify(self, plan: SubtitleAcquisitionPlan) -> bool:
        if not isinstance(plan, SubtitleAcquisitionPlan) or not plan.verify_hash():
            return False
        expected_id = _transaction_id(
            run_id=self.run_id,
            plan_hash=self.plan_hash,
            approval_id=self.approval_id,
        )
        suffix = expected_id.removeprefix("subtitle-txn-v1-")
        return (
            _TRANSACTION_ID.fullmatch(self.transaction_id) is not None
            and hmac.compare_digest(self.transaction_id, expected_id)
            and self.run_id == plan.run_id
            and self.plan_hash == plan.plan_hash
            and ApprovalRecord.is_valid_id(self.approval_id)
            and self.source_folder_device == plan.source_folder_device
            and self.source_folder_inode == plan.source_folder_inode
            and self.staging_name == f".reeloom-acquiring-{suffix}"
            and self.destination_name == plan.destination_directory.as_posix()
            and self.archive_count == len(plan.archives)
            and self.member_count == len(plan.members)
        )

    def canonical_bytes(self) -> bytes:
        suffix = self.transaction_id.removeprefix("subtitle-txn-v1-")
        plan_suffix = self.plan_hash.removeprefix("sha256:")
        if (
            _TRANSACTION_ID.fullmatch(self.transaction_id) is None
            or _PLAN_HASH.fullmatch(self.plan_hash) is None
            or not isinstance(self.run_id, str)
            or not self.run_id
            or len(self.run_id.encode("utf-8")) > 128
            or not ApprovalRecord.is_valid_id(self.approval_id)
            or type(self.source_folder_device) is not int
            or self.source_folder_device < 0
            or type(self.source_folder_inode) is not int
            or self.source_folder_inode < 0
            or self.staging_name != f".reeloom-acquiring-{suffix}"
            or self.destination_name != f"reeloom-acquired-{plan_suffix}"
            or type(self.archive_count) is not int
            or not 1 <= self.archive_count <= 12
            or type(self.member_count) is not int
            or not 1 <= self.member_count <= 256
        ):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        return _canonical_json(
            {
                "approval_id": self.approval_id,
                "archive_count": self.archive_count,
                "destination_name": self.destination_name,
                "member_count": self.member_count,
                "plan_hash": self.plan_hash,
                "run_id": self.run_id,
                "schema_version": CURRENT_SUBTITLE_TRANSACTION_SCHEMA_VERSION,
                "source_folder": {
                    "device": self.source_folder_device,
                    "inode": self.source_folder_inode,
                },
                "staging_name": self.staging_name,
                "transaction_id": self.transaction_id,
            }
        )

    def event_bytes(
        self,
        event_type: str,
        *,
        member_index: int | None = None,
        failure_code: str | None = None,
        staging_device: int | None = None,
        staging_inode: int | None = None,
    ) -> bytes:
        if (
            event_type not in _EVENT_TYPES
            or (event_type == "member_written")
            != (member_index is not None)
            or (
                member_index is not None
                and (
                    type(member_index) is not int
                    or not 0 <= member_index < self.member_count
                )
            )
            or (event_type == "failed") != (failure_code is not None)
            or (event_type == "staging_created")
            != (staging_device is not None and staging_inode is not None)
            or (
                event_type != "staging_created"
                and (staging_device is not None or staging_inode is not None)
            )
            or (
                staging_device is not None
                and (
                    type(staging_device) is not int
                    or staging_device < 0
                    or type(staging_inode) is not int
                    or staging_inode < 0
                )
            )
            or (
                failure_code is not None
                and (
                    not isinstance(failure_code, str)
                    or not failure_code
                    or len(failure_code.encode("utf-8")) > 128
                )
            )
        ):
            raise ExecutorError(ExecutorErrorCode.INVALID_JOURNAL)
        return _canonical_json(
            {
                "event_type": event_type,
                "failure_code": failure_code,
                "member_index": member_index,
                "schema_version": CURRENT_SUBTITLE_TRANSACTION_SCHEMA_VERSION,
                "staging_device": staging_device,
                "staging_inode": staging_inode,
                "transaction_id": self.transaction_id,
            }
        )
