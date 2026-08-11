from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from reeloom.kernel.forward_execution import (
    ExecutionOperation,
    ExecutionOperationLease,
)
from reeloom.server.execution_lease_heartbeat import (
    ExecutionLeaseHeartbeat,
    ExecutionLeaseHeartbeatError,
)


_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _lease() -> ExecutionOperationLease:
    operation = ExecutionOperation.authorized(
        operation_id="operation:heartbeat",
        run_id="run:heartbeat",
        plan_hash="sha256:" + "a" * 64,
    ).begin_or_reconcile()
    return ExecutionOperationLease(
        operation=operation,
        worker_id="worker:heartbeat",
        expires_at=_NOW + timedelta(minutes=1),
    )


def test_heartbeat_publishes_renewed_lease_to_effect_thread() -> None:
    renewed = threading.Event()

    def renew(
        lease: ExecutionOperationLease,
        now: datetime,
        lease_for: timedelta,
    ) -> ExecutionOperationLease:
        renewed.set()
        return ExecutionOperationLease(
            operation=lease.operation,
            worker_id=lease.worker_id,
            expires_at=now + lease_for,
        )

    heartbeat = ExecutionLeaseHeartbeat(
        _lease(),
        renew=renew,
        clock=lambda: _NOW + timedelta(minutes=1),
        lease_for=timedelta(minutes=1),
        interval=timedelta(milliseconds=10),
    )

    with heartbeat:
        assert renewed.wait(timeout=1)

    assert heartbeat.current().expires_at == _NOW + timedelta(minutes=2)


def test_heartbeat_surfaces_renewal_failure() -> None:
    failed = threading.Event()

    def renew(
        _lease: ExecutionOperationLease,
        _now: datetime,
        _lease_for: timedelta,
    ) -> ExecutionOperationLease:
        failed.set()
        raise RuntimeError("lease_lost")

    heartbeat = ExecutionLeaseHeartbeat(
        _lease(),
        renew=renew,
        clock=lambda: _NOW,
        lease_for=timedelta(minutes=1),
        interval=timedelta(milliseconds=10),
    )

    with heartbeat:
        assert failed.wait(timeout=1)

    try:
        heartbeat.current()
    except RuntimeError as error:
        assert str(error) == "lease_lost"
    else:
        raise AssertionError("renewal failure was hidden")


def test_heartbeat_shutdown_is_bounded_when_renewal_never_returns() -> None:
    renewing = threading.Event()
    release = threading.Event()

    def renew(
        lease: ExecutionOperationLease,
        _now: datetime,
        _lease_for: timedelta,
    ) -> ExecutionOperationLease:
        renewing.set()
        release.wait(timeout=2)
        return lease

    heartbeat = ExecutionLeaseHeartbeat(
        _lease(),
        renew=renew,
        clock=lambda: _NOW,
        lease_for=timedelta(minutes=1),
        interval=timedelta(milliseconds=10),
        shutdown_timeout=timedelta(milliseconds=20),
    )

    started = time.monotonic()
    try:
        with pytest.raises(
            ExecutionLeaseHeartbeatError,
            match="lease_heartbeat_shutdown_timeout",
        ):
            with heartbeat:
                assert renewing.wait(timeout=1)
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
