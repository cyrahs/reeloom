from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.forward_operation_repository import (
    PostgresForwardOperationRepository,
)
from reeloom.server.scheduler_repository import PostgresSchedulerRepository

_LOG = logging.getLogger(__name__)
_LEASE = timedelta(minutes=1)
_RETRY = timedelta(seconds=5)


@dataclass(frozen=True, slots=True)
class ForwardRescanWorker:
    """Idempotently dispatch terminal v2 operations back to the watcher."""

    operations: PostgresForwardOperationRepository
    scheduler: PostgresSchedulerRepository

    def process_one(self, *, worker_id: str, now: datetime | None = None) -> bool:
        current = datetime.now(UTC) if now is None else now
        claim = self.operations.claim_generation_request(
            worker_id=worker_id,
            now=current,
            lease_for=_LEASE,
        )
        if claim is None:
            return False
        try:
            self.scheduler.accept_generation_request(
                request_id=claim.request_id,
                worker_id=claim.worker_id,
                attempt_count=claim.attempt_count,
                lease_expires_at=claim.lease_expires_at,
                now=current,
            )
        except ServerError as error:
            if error.code is ServerErrorCode.DATABASE_UNAVAILABLE:
                raise
            self.operations.retry_generation_request(
                claim,
                now=datetime.now(UTC) if now is None else now,
                delay=_RETRY,
                warning=error.code.value,
            )
            _LOG.warning(
                "forward_rescan_retry request_id=%s error=%s",
                claim.request_id,
                error.code.value,
            )
        except Exception as error:
            self.operations.retry_generation_request(
                claim,
                now=datetime.now(UTC) if now is None else now,
                delay=_RETRY,
                warning=type(error).__name__[:128],
            )
            _LOG.warning(
                "forward_rescan_retry request_id=%s error_type=%s",
                claim.request_id,
                type(error).__name__,
            )
        return True
