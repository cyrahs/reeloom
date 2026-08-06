from __future__ import annotations

import hmac
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from reeloom.adapters._immutable_file import (
    ImmutableFileError,
    ImmutableFileErrorCode,
    open_root,
    read_at,
    write_once_at,
)
from reeloom.executor.errors import ApprovalError, ApprovalErrorCode
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.errors import DomainError
from reeloom.policy.path_policy import AuthorizedRoot

_MAX_APPROVAL_BYTES = 4096


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FilesystemApprovalStore:
    """Persist and atomically claim approvals inside one authorized root."""

    root: AuthorizedRoot
    clock: Callable[[], datetime] = _utc_now

    def issue(self, approval: ApprovalRecord) -> None:
        if (
            not isinstance(approval, ApprovalRecord)
            or not approval.verify_id()
        ):
            raise ApprovalError(ApprovalErrorCode.INVALID_RECORD)
        try:
            expired = approval.is_expired(self.clock())
        except (DomainError, TypeError):
            raise ApprovalError(ApprovalErrorCode.STORE_FAILURE) from None
        if expired:
            raise ApprovalError(ApprovalErrorCode.EXPIRED)

        root_fd = self._open_root()
        try:
            name = self._approval_name(approval.approval_id)
            content = approval.canonical_bytes()
            try:
                self._write_exclusive(
                    root_fd,
                    name,
                    content,
                    exists_code=ApprovalErrorCode.ALREADY_EXISTS,
                )
            except ApprovalError as error:
                if error.code is not ApprovalErrorCode.ALREADY_EXISTS:
                    raise
                existing = self._read_approval(root_fd, name)
                if not hmac.compare_digest(
                    existing.canonical_bytes(),
                    content,
                ):
                    raise
        finally:
            os.close(root_fd)

    def claim(
        self,
        *,
        approval_id: str,
        run_id: str,
        plan_hash: str,
        scope: ApprovalScope,
    ) -> ApprovalRecord:
        approval_name = self._approval_name(approval_id)
        root_fd = self._open_root()
        try:
            approval = self._read_approval(root_fd, approval_name)
            if (
                approval.run_id != run_id
                or approval.plan_hash != plan_hash
                or approval.scope is not scope
            ):
                raise ApprovalError(
                    ApprovalErrorCode.BINDING_MISMATCH
                )
            try:
                claimed = self._read_approval(
                    root_fd,
                    self._claim_name(approval_id),
                )
            except ApprovalError as error:
                if error.code is not ApprovalErrorCode.NOT_FOUND:
                    raise
            else:
                if not hmac.compare_digest(
                    claimed.canonical_bytes(),
                    approval.canonical_bytes(),
                ):
                    raise ApprovalError(ApprovalErrorCode.STORE_FAILURE)
                # A claim remains recoverable after its issuance expiry. The
                # executor must resume that exact transaction instead of
                # replacing the approval and abandoning its staging state.
                raise ApprovalError(ApprovalErrorCode.ALREADY_CLAIMED)
            try:
                expired = approval.is_expired(self.clock())
            except (DomainError, TypeError):
                raise ApprovalError(
                    ApprovalErrorCode.STORE_FAILURE
                ) from None
            if expired:
                raise ApprovalError(ApprovalErrorCode.EXPIRED)

            self._write_exclusive(
                root_fd,
                self._claim_name(approval_id),
                approval.canonical_bytes(),
                exists_code=ApprovalErrorCode.ALREADY_CLAIMED,
            )
            return approval
        finally:
            os.close(root_fd)

    def require_claim(
        self,
        *,
        approval_id: str,
        run_id: str,
        plan_hash: str,
        scope: ApprovalScope,
    ) -> ApprovalRecord:
        claim_name = self._claim_name(approval_id)
        root_fd = self._open_root()
        try:
            approval = self._read_approval(root_fd, claim_name)
        finally:
            os.close(root_fd)
        if (
            approval.run_id != run_id
            or approval.plan_hash != plan_hash
            or approval.scope is not scope
        ):
            raise ApprovalError(ApprovalErrorCode.BINDING_MISMATCH)
        return approval

    def _open_root(self) -> int:
        try:
            return open_root(self.root)
        except ImmutableFileError:
            raise ApprovalError(
                ApprovalErrorCode.STORE_FAILURE
            ) from None

    @staticmethod
    def _approval_name(approval_id: object) -> str:
        if not ApprovalRecord.is_valid_id(approval_id):
            raise ApprovalError(ApprovalErrorCode.INVALID_RECORD)
        return f"{approval_id}.approval.json"

    @staticmethod
    def _claim_name(approval_id: object) -> str:
        if not ApprovalRecord.is_valid_id(approval_id):
            raise ApprovalError(ApprovalErrorCode.INVALID_RECORD)
        return f"{approval_id}.claim.json"

    @staticmethod
    def _read_approval(root_fd: int, name: str) -> ApprovalRecord:
        try:
            content = read_at(
                root_fd,
                name,
                limit=_MAX_APPROVAL_BYTES,
            )
        except ImmutableFileError as error:
            if error.code is ImmutableFileErrorCode.NOT_FOUND:
                raise ApprovalError(
                    ApprovalErrorCode.NOT_FOUND
                ) from None
            if error.code is ImmutableFileErrorCode.INVALID:
                raise ApprovalError(ApprovalErrorCode.INVALID_RECORD)
            raise ApprovalError(ApprovalErrorCode.STORE_FAILURE) from None

        try:
            return ApprovalRecord.from_canonical_bytes(content)
        except DomainError:
            raise ApprovalError(ApprovalErrorCode.INVALID_RECORD) from None

    @staticmethod
    def _write_exclusive(
        root_fd: int,
        name: str,
        content: bytes,
        *,
        exists_code: ApprovalErrorCode,
    ) -> None:
        try:
            write_once_at(
                root_fd,
                name,
                content,
                limit=_MAX_APPROVAL_BYTES,
            )
        except ImmutableFileError as error:
            if error.code is ImmutableFileErrorCode.EXISTS:
                raise ApprovalError(exists_code) from None
            # A partially created claim remains consumed so recovery fails closed.
            raise ApprovalError(ApprovalErrorCode.STORE_FAILURE) from None
