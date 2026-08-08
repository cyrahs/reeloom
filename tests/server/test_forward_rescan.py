from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.forward_operation_repository import ForwardRescanClaim
from reeloom.server.forward_rescan import ForwardRescanWorker

_NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


class _Operations:
    def __init__(self, claim: ForwardRescanClaim | None) -> None:
        self.claim = claim
        self.completed: list[str] = []
        self.retried: list[tuple[str, str]] = []

    def claim_rescan(self, **_: object) -> ForwardRescanClaim | None:
        claim, self.claim = self.claim, None
        return claim

    def complete_rescan(
        self, claim: ForwardRescanClaim, **_: object
    ) -> None:
        self.completed.append(claim.operation_id)

    def retry_rescan(
        self,
        claim: ForwardRescanClaim,
        *,
        error: str,
        **_: object,
    ) -> None:
        self.retried.append((claim.operation_id, error))


class _Scheduler:
    def __init__(self, error: ServerError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str | None]] = []

    def acknowledge_forward_rescan(
        self, *, run_id: str, audit_event: str | None = None
    ) -> None:
        self.calls.append((run_id, audit_event))
        if self.error is not None:
            raise self.error


def _claim() -> ForwardRescanClaim:
    return ForwardRescanClaim(
        operation_id="operation:1",
        run_id="run:1",
        worker_id="worker:1",
        attempt_count=1,
        lease_expires_at=_NOW + timedelta(minutes=1),
    )


def test_forward_rescan_dispatches_once_and_completes_outbox() -> None:
    operations = _Operations(_claim())
    scheduler = _Scheduler()
    worker = ForwardRescanWorker(operations, scheduler)  # type: ignore[arg-type]

    assert worker.process_one(worker_id="worker:1", now=_NOW)
    assert not worker.process_one(worker_id="worker:1", now=_NOW)

    assert scheduler.calls == [("run:1", "forward_operation_rescan")]
    assert operations.completed == ["operation:1"]
    assert operations.retried == []


def test_forward_rescan_retries_control_plane_conflict() -> None:
    operations = _Operations(_claim())
    scheduler = _Scheduler(
        ServerError(ServerErrorCode.INTERACTION_CONFLICT)
    )
    worker = ForwardRescanWorker(operations, scheduler)  # type: ignore[arg-type]

    assert worker.process_one(worker_id="worker:1", now=_NOW)

    assert operations.completed == []
    assert operations.retried == [
        ("operation:1", "interaction_conflict")
    ]


def test_forward_rescan_does_not_mask_database_outage() -> None:
    operations = _Operations(_claim())
    scheduler = _Scheduler(
        ServerError(ServerErrorCode.DATABASE_UNAVAILABLE)
    )
    worker = ForwardRescanWorker(operations, scheduler)  # type: ignore[arg-type]

    with pytest.raises(ServerError) as raised:
        worker.process_one(worker_id="worker:1", now=_NOW)

    assert raised.value.code is ServerErrorCode.DATABASE_UNAVAILABLE
    assert operations.completed == []
    assert operations.retried == []
