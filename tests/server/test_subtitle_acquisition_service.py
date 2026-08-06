from __future__ import annotations

import asyncio
from typing import cast

from psycopg_pool import ConnectionPool

from reeloom.executor.subtitle_acquisition import SubtitleAcquisitionResult
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.server.subtitle_acquisition_service import (
    SubtitleAcquisitionCoordinator,
)


class _Executor:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None

    async def apply(
        self,
        *,
        plan_hash: str,
        approval_id: str,
    ) -> SubtitleAcquisitionResult:
        self.loop = asyncio.get_running_loop()
        return SubtitleAcquisitionResult(
            run_id="run-1",
            plan_hash=plan_hash,
            approval_id=approval_id,
            transaction_id="subtitle-txn-v1-" + "a" * 64,
            destination_name="reeloom-acquired-" + "b" * 64,
            destination_device=1,
            destination_inode=2,
            published_count=1,
        )


class _Lease:
    def __init__(self) -> None:
        self.executor = _Executor()
        self.closed = False

    async def close(self) -> None:
        assert asyncio.get_running_loop() is self.executor.loop
        self.closed = True


def test_executor_and_transport_close_share_one_event_loop() -> None:
    coordinator = SubtitleAcquisitionCoordinator(
        pool=cast(ConnectionPool, object()),
        plans=cast(object, object()),  # type: ignore[arg-type]
        approvals=cast(object, object()),  # type: ignore[arg-type]
        executor_factory=cast(object, object()),  # type: ignore[arg-type]
        successors=cast(object, object()),  # type: ignore[arg-type]
    )
    lease = _Lease()

    result, approval_id = asyncio.run(
        coordinator._execute_lease(
            lease=lease,
            run_id="run-1",
            plan_hash="sha256:" + "b" * 64,
            approval_id="approval-v1-" + "c" * 64,
        )
    )

    assert result.status == "completed"
    assert approval_id == "approval-v1-" + "c" * 64
    assert lease.closed


def test_destination_collision_diagnostic_is_strict_and_bounded() -> None:
    diagnostic = SubtitleAcquisitionCoordinator._failure_diagnostic(
        ExecutorError(
            ExecutorErrorCode.DESTINATION_COLLISION,
            context={
                "stage": "staging_validate",
                "reason": "unsafe_permissions",
                "actual_mode": 0o775,
                "expected_policy": "owner_rwx_no_group_or_other_write",
            },
        )
    )

    assert diagnostic == {
        "schema_version": 1,
        "stage": "staging_validate",
        "reason": "unsafe_permissions",
        "actual_mode": 0o775,
        "expected_policy": "owner_rwx_no_group_or_other_write",
    }


def test_untrusted_collision_context_is_not_persisted() -> None:
    diagnostic = SubtitleAcquisitionCoordinator._failure_diagnostic(
        ExecutorError(
            ExecutorErrorCode.DESTINATION_COLLISION,
            context={
                "stage": "staging_validate",
                "reason": "unsafe_permissions",
                "path": "/private/source",
            },
        )
    )

    assert diagnostic is None
