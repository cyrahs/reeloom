from __future__ import annotations

import asyncio
from typing import cast

from psycopg_pool import ConnectionPool

from reeloom.executor.subtitle_acquisition import SubtitleAcquisitionResult
from reeloom.executor.errors import ExecutorError, ExecutorErrorCode
from reeloom.server.config import SubtitleAcquisitionPolicy
from reeloom.server.errors import ServerError, ServerErrorCode
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


class _RetryConnection:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row
        self.params: tuple[object, ...] | None = None

    def __enter__(self) -> _RetryConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def transaction(self) -> _RetryConnection:
        return self

    def execute(
        self,
        query: str,
        params: tuple[object, ...],
    ) -> _RetryConnection:
        assert "request.status = 'blocked'" in query
        assert "request.failure_code" in query
        assert "'destination_collision'" in query
        assert "'atomic_move_unsupported'" in query
        assert "run.status = 'running'" in query
        self.params = params
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row


class _RetryPool:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.connection_value = _RetryConnection(row)

    def connection(self) -> _RetryConnection:
        return self.connection_value


class _FailConnection:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row
        self.current: tuple[object, ...] | None = None
        self.queries: list[str] = []
        self.params: list[tuple[object, ...]] = []

    def __enter__(self) -> _FailConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def transaction(self) -> _FailConnection:
        return self

    def execute(
        self,
        query: str,
        params: tuple[object, ...],
    ) -> _FailConnection:
        self.queries.append(query)
        self.params.append(params)
        self.current = self.row if "UPDATE runs AS run" in query else None
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return self.current


class _FailPool:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.connection_value = _FailConnection(row)

    def connection(self) -> _FailConnection:
        return self.connection_value


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


def test_retry_reopens_only_an_exact_retryable_blocked_failure() -> None:
    pool = _RetryPool(("automatic",))
    coordinator = SubtitleAcquisitionCoordinator(
        pool=cast(ConnectionPool, pool),
        plans=cast(object, object()),  # type: ignore[arg-type]
        approvals=cast(object, object()),  # type: ignore[arg-type]
        executor_factory=cast(object, object()),  # type: ignore[arg-type]
        successors=cast(object, object()),  # type: ignore[arg-type]
    )

    policy = coordinator._reopen_retryable_failure(
        run_id="run-1",
        plan_hash="sha256:" + "b" * 64,
    )

    assert policy is SubtitleAcquisitionPolicy.AUTOMATIC
    assert pool.connection_value.params == (
        "run-1",
        "sha256:" + "b" * 64,
    )


def test_retry_rejects_nonmatching_blocked_request() -> None:
    coordinator = SubtitleAcquisitionCoordinator(
        pool=cast(ConnectionPool, _RetryPool(None)),
        plans=cast(object, object()),  # type: ignore[arg-type]
        approvals=cast(object, object()),  # type: ignore[arg-type]
        executor_factory=cast(object, object()),  # type: ignore[arg-type]
        successors=cast(object, object()),  # type: ignore[arg-type]
    )

    try:
        coordinator._reopen_retryable_failure(
            run_id="run-1",
            plan_hash="sha256:" + "b" * 64,
        )
    except ServerError as error:
        assert error.code is ServerErrorCode.INTERACTION_CONFLICT
    else:
        raise AssertionError("retry unexpectedly reopened a nonmatch")


def test_fail_blocked_ends_only_exact_terminal_acquisition() -> None:
    plan_hash = "sha256:" + "b" * 64
    pool = _FailPool(
        (
            plan_hash,
            "automatic",
            "blocked",
            "approval-subtitle-1",
            None,
            "source_drift",
            None,
        )
    )
    coordinator = SubtitleAcquisitionCoordinator(
        pool=cast(ConnectionPool, pool),
        plans=cast(object, object()),  # type: ignore[arg-type]
        approvals=cast(object, object()),  # type: ignore[arg-type]
        executor_factory=cast(object, object()),  # type: ignore[arg-type]
        successors=cast(object, object()),  # type: ignore[arg-type]
    )

    record = coordinator.fail_blocked(
        run_id="run-1",
        plan_hash=plan_hash,
    )

    assert record.status == "blocked"
    assert record.failure_code == "source_drift"
    update = pool.connection_value.queries[0]
    assert "request.status = 'blocked'" in update
    assert "run.status = 'running'" in update
    assert "job.status = 'completed'" in update
    assert "NOT EXISTS" in update
    assert "subtitle_acquisition_failed" in pool.connection_value.queries[1]


def test_fail_blocked_rejects_nonmatching_request() -> None:
    coordinator = SubtitleAcquisitionCoordinator(
        pool=cast(ConnectionPool, _FailPool(None)),
        plans=cast(object, object()),  # type: ignore[arg-type]
        approvals=cast(object, object()),  # type: ignore[arg-type]
        executor_factory=cast(object, object()),  # type: ignore[arg-type]
        successors=cast(object, object()),  # type: ignore[arg-type]
    )

    try:
        coordinator.fail_blocked(
            run_id="run-1",
            plan_hash="sha256:" + "b" * 64,
        )
    except ServerError as error:
        assert error.code is ServerErrorCode.INTERACTION_CONFLICT
    else:
        raise AssertionError("fail unexpectedly ended a nonmatch")
