from __future__ import annotations

from typing import Protocol

from reeloom.kernel.approval import ApprovalRecord, ApprovalScope


class ApprovalStore(Protocol):
    def issue(self, approval: ApprovalRecord) -> None: ...

    def claim(
        self,
        *,
        approval_id: str,
        run_id: str,
        plan_hash: str,
        scope: ApprovalScope,
    ) -> ApprovalRecord: ...

    def require_claim(
        self,
        *,
        approval_id: str,
        run_id: str,
        plan_hash: str,
        scope: ApprovalScope,
    ) -> ApprovalRecord: ...
