from __future__ import annotations

from enum import StrEnum

from reeloom.kernel.forward_execution import ExecutionOperationStatus
from reeloom.server.config import ApplyPolicy


class ForwardAvailableAction(StrEnum):
    EXECUTE = "execute"
    RESCAN = "rescan"


def forward_available_actions(
    *,
    policy: ApplyPolicy,
    operation_status: ExecutionOperationStatus | None,
) -> tuple[ForwardAvailableAction, ...]:
    """One policy function shared by v2 command admission and read models."""

    if (
        policy is ApplyPolicy.MANUAL
        and (
            operation_status is None
            or operation_status
            in {
                ExecutionOperationStatus.AUTHORIZED,
                ExecutionOperationStatus.RUNNING,
            }
        )
    ):
        return (ForwardAvailableAction.EXECUTE,)
    if operation_status in {
        ExecutionOperationStatus.PARTIAL,
        ExecutionOperationStatus.STALE,
        ExecutionOperationStatus.COLLISION,
        ExecutionOperationStatus.UNSAFE,
        ExecutionOperationStatus.UNAVAILABLE,
    }:
        return (ForwardAvailableAction.RESCAN,)
    if operation_status in {
        ExecutionOperationStatus.AUTHORIZED,
        ExecutionOperationStatus.RUNNING,
    }:
        return ()
    return ()
