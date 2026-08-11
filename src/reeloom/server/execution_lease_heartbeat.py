from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from reeloom.kernel.forward_execution import ExecutionOperationLease


class ExecutionLeaseHeartbeatError(RuntimeError):
    pass


class ExecutionLeaseHeartbeat:
    """Keep one durable operation lease alive while an effect is running."""

    def __init__(
        self,
        lease: ExecutionOperationLease,
        *,
        renew: Callable[
            [ExecutionOperationLease, datetime, timedelta],
            ExecutionOperationLease,
        ],
        clock: Callable[[], datetime],
        lease_for: timedelta,
        interval: timedelta,
        shutdown_timeout: timedelta | None = None,
    ) -> None:
        effective_shutdown_timeout = (
            min(timedelta(seconds=5), lease_for / 2)
            if shutdown_timeout is None
            else shutdown_timeout
        )
        if (
            interval <= timedelta(0)
            or interval >= lease_for
            or effective_shutdown_timeout <= timedelta(0)
            or effective_shutdown_timeout > lease_for
        ):
            raise ValueError("invalid lease heartbeat interval")
        self._lease = lease
        self._renew = renew
        self._clock = clock
        self._lease_for = lease_for
        self._interval_seconds = interval.total_seconds()
        self._shutdown_timeout_seconds = (
            effective_shutdown_timeout.total_seconds()
        )
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._failure: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"operation-lease-{lease.operation.operation_id}",
            daemon=True,
        )

    def __enter__(self) -> ExecutionLeaseHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=self._shutdown_timeout_seconds)
        if self._thread.is_alive():
            error = ExecutionLeaseHeartbeatError(
                "lease_heartbeat_shutdown_timeout"
            )
            with self._lock:
                if self._failure is None:
                    self._failure = error
            raise error

    def current(self) -> ExecutionOperationLease:
        with self._lock:
            failure = self._failure
            lease = self._lease
        if failure is not None:
            raise failure
        if self._clock() >= lease.expires_at:
            raise ExecutionLeaseHeartbeatError("lease_expired")
        return lease

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                with self._lock:
                    lease = self._lease
                renewed = self._renew(
                    lease,
                    self._clock(),
                    self._lease_for,
                )
            except BaseException as error:
                with self._lock:
                    self._failure = error
                self._stop.set()
                return
            with self._lock:
                self._lease = renewed
