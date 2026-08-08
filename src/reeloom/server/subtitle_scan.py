from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.scheduler_repository import PostgresSchedulerRepository
from reeloom.server.subtitle_publication_repository import (
    PostgresSubtitlePublicationRepository,
)

_LOG = logging.getLogger(__name__)
_LEASE = timedelta(minutes=1)
_RETRY = timedelta(seconds=5)


@dataclass(frozen=True, slots=True)
class SubtitleScanWorker:
    publications: PostgresSubtitlePublicationRepository
    scheduler: PostgresSchedulerRepository

    def process_one(self, *, worker_id: str, now: datetime | None = None) -> bool:
        current = datetime.now(UTC) if now is None else now
        claim = self.publications.claim_scan(
            worker_id=worker_id,
            now=current,
            lease_for=_LEASE,
        )
        if claim is None:
            return False
        try:
            self.scheduler.dispatch_subtitle_scan(
                request_id=claim.request_id,
                run_id=claim.run_id,
                worker_id=claim.worker_id,
                attempt_count=claim.attempt_count,
                lease_expires_at=claim.lease_expires_at,
                now=current,
            )
        except ServerError as error:
            if error.code is ServerErrorCode.DATABASE_UNAVAILABLE:
                raise
            self.publications.retry(
                claim,
                now=current,
                delay=_RETRY,
                error=error.code.value,
            )
            _LOG.warning(
                "subtitle_scan_retry request_id=%s error=%s",
                claim.request_id,
                error.code.value,
            )
        except Exception as error:
            self.publications.retry(
                claim,
                now=current,
                delay=_RETRY,
                error=type(error).__name__,
            )
            _LOG.warning(
                "subtitle_scan_retry request_id=%s error_type=%s",
                claim.request_id,
                type(error).__name__,
            )
        return True
