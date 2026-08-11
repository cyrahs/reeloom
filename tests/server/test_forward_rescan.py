from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.forward_operation_repository import GenerationRequestClaim
from reeloom.server.forward_rescan import ForwardRescanWorker

_NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


class _Operations:
    def __init__(self, claim: GenerationRequestClaim | None) -> None:
        self.claim = claim
        self.completed: list[str] = []
        self.retried: list[tuple[str, str]] = []

    def claim_generation_request(
        self, **_: object
    ) -> GenerationRequestClaim | None:
        claim, self.claim = self.claim, None
        return claim

    def retry_generation_request(
        self,
        claim: GenerationRequestClaim,
        *,
        warning: str,
        **_: object,
    ) -> None:
        self.retried.append((claim.request_id, warning))


class _Scheduler:
    def __init__(self, error: ServerError | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def accept_generation_request(
        self, *, request_id: str, **_: object
    ) -> None:
        self.calls.append(request_id)
        if self.error is not None:
            raise self.error


def _claim() -> GenerationRequestClaim:
    return GenerationRequestClaim(
        request_id="generation:1",
        origin_run_id="run:1",
        worker_id="worker:1",
        attempt_count=1,
        lease_expires_at=_NOW + timedelta(minutes=1),
    )


def test_forward_rescan_accepts_one_truthful_generation_request() -> None:
    operations = _Operations(_claim())
    scheduler = _Scheduler()
    worker = ForwardRescanWorker(operations, scheduler)  # type: ignore[arg-type]

    assert worker.process_one(worker_id="worker:1", now=_NOW)
    assert not worker.process_one(worker_id="worker:1", now=_NOW)

    assert scheduler.calls == ["generation:1"]
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
        ("generation:1", "interaction_conflict")
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
