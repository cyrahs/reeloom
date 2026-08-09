from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg_pool import ConnectionPool

from reeloom.executor.subtitle_publication import (
    SubtitlePublicationResult,
    SubtitlePublicationState,
)
from reeloom.kernel.subtitle_acquisition import SubtitleAcquisitionPlanV2
from reeloom.kernel.subtitle_publication import SubtitlePublicationManifest
from reeloom.server.config import ServerWorkType
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.subtitle_successor import subtitle_lineage_key

_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class SubtitleScanClaim:
    request_id: str
    run_id: str
    worker_id: str
    attempt_count: int
    lease_expires_at: datetime


def _publication_id(manifest: SubtitlePublicationManifest) -> str:
    return f"subtitle-publication-v2-{manifest.digest}"


def _scan_request_id(lineage_key: str) -> str:
    return "subtitle-scan-v2-" + hashlib.sha256(
        lineage_key.encode("ascii")
    ).hexdigest()


class PostgresSubtitlePublicationRepository:
    """Current-state subtitle settlement plus an ordinary scan request."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def settle(
        self,
        *,
        plan: SubtitleAcquisitionPlanV2,
        approval_id: str,
        result: SubtitlePublicationResult,
        origin_discovery_id: str,
    ) -> str:
        if (
            not isinstance(plan, SubtitleAcquisitionPlanV2)
            or not plan.verify_hash()
            or not isinstance(result, SubtitlePublicationResult)
            or result.state is not SubtitlePublicationState.COMPLETED
            or result.published_count != len(plan.members)
            or not isinstance(approval_id, str)
            or _TEXT.fullmatch(approval_id) is None
            or not isinstance(origin_discovery_id, str)
            or _TEXT.fullmatch(origin_discovery_id) is None
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        manifest = SubtitlePublicationManifest.from_plan(plan)
        publication_id = _publication_id(manifest)
        lineage_key = subtitle_lineage_key(origin_discovery_id)
        request_id = _scan_request_id(lineage_key)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT r.discovery_id, d.watch_id, d.source_folder,
                               d.work_type, r.status,
                               r.subtitle_acquisition_lineage_key,
                               request.status, request.approval_id
                        FROM runs AS r
                        JOIN discoveries AS d
                          ON d.discovery_id = r.discovery_id
                        JOIN subtitle_acquisition_requests AS request
                          ON request.run_id = r.run_id
                        WHERE r.run_id = %s
                          AND request.plan_hash = %s
                        FOR UPDATE OF r, request
                        """,
                        (plan.run_id, plan.plan_hash),
                    ).fetchone()
                    if row is None:
                        raise ServerError(ServerErrorCode.RUN_NOT_FOUND)
                    if (
                        str(row[0]) != origin_discovery_id
                        or str(row[2]) != plan.source_folder
                        or str(row[3]) != ServerWorkType.ANIME.value
                        or str(row[4])
                        not in {"running", "awaiting_approval", "applying"}
                        or row[5] is not None
                        or str(row[6]) != "approved"
                        or str(row[7]) != approval_id
                    ):
                        existing = connection.execute(
                            """
                            SELECT publication_id
                            FROM subtitle_publication_settlements_v2
                            WHERE origin_run_id = %s
                              AND acquisition_plan_hash = %s
                              AND approval_id = %s
                              AND publication_id = %s
                            """,
                            (
                                plan.run_id,
                                plan.plan_hash,
                                approval_id,
                                publication_id,
                            ),
                        ).fetchone()
                        if existing is not None:
                            return publication_id
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    connection.execute(
                        """
                        INSERT INTO subtitle_acquisition_lineages
                            (lineage_key, root_discovery_id)
                        VALUES (%s, %s)
                        """,
                        (lineage_key, origin_discovery_id),
                    )
                    updated = connection.execute(
                        """
                        UPDATE runs
                        SET status = 'superseded',
                            subtitle_acquisition_lineage_key = %s
                        WHERE run_id = %s
                          AND subtitle_acquisition_lineage_key IS NULL
                        RETURNING run_id
                        """,
                        (lineage_key, plan.run_id),
                    ).fetchone()
                    if updated is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'completed', boot_id = NULL,
                            updated_at = clock_timestamp()
                        WHERE run_id = %s
                        """,
                        (plan.run_id,),
                    )
                    connection.execute(
                        """
                        INSERT INTO subtitle_publication_settlements_v2
                            (lineage_key, origin_run_id,
                             acquisition_plan_hash, approval_id,
                             publication_id, watch_id, source_folder,
                             publication_directory, manifest_digest,
                             member_count)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            lineage_key,
                            plan.run_id,
                            plan.plan_hash,
                            approval_id,
                            publication_id,
                            str(row[1]),
                            plan.source_folder,
                            manifest.publication_directory,
                            manifest.digest,
                            len(manifest.members),
                        ),
                    )
                    changed = connection.execute(
                        """
                        UPDATE subtitle_acquisition_requests
                        SET status = 'published', transaction_id = %s,
                            failure_code = NULL,
                            failure_diagnostic = NULL,
                            updated_at = clock_timestamp()
                        WHERE run_id = %s AND plan_hash = %s
                          AND status = 'approved'
                          AND approval_id = %s
                        RETURNING run_id
                        """,
                        (
                            publication_id,
                            plan.run_id,
                            plan.plan_hash,
                            approval_id,
                        ),
                    ).fetchone()
                    if changed is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    connection.execute(
                        """
                        INSERT INTO subtitle_scan_requests_v2
                            (request_id, lineage_key, run_id,
                             watch_id, source_folder)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            request_id,
                            lineage_key,
                            plan.run_id,
                            str(row[1]),
                            plan.source_folder,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO scheduler_audit
                            (event_type, subject_id)
                        VALUES ('subtitle_publication_settled_v2', %s)
                        ON CONFLICT (event_type, subject_id) DO NOTHING
                        """,
                        (plan.run_id,),
                    )
                    return publication_id
        except ServerError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def claim_scan(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> SubtitleScanClaim | None:
        if (
            not isinstance(worker_id, str)
            or _TEXT.fullmatch(worker_id) is None
            or not isinstance(now, datetime)
            or now.tzinfo is None
            or not timedelta(seconds=1) <= lease_for <= timedelta(hours=1)
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        UPDATE subtitle_scan_requests_v2
                        SET state = CASE WHEN attempt_count >= 100
                                   THEN 'blocked' ELSE 'retry_wait' END,
                            lease_owner = NULL, lease_expires_at = NULL,
                            available_at = %s, updated_at = %s
                        WHERE state = 'leased' AND lease_expires_at <= %s
                        """,
                        (now, now, now),
                    )
                    connection.execute(
                        """
                        UPDATE subtitle_scan_requests_v2
                        SET state = 'blocked', last_error = 'retry_exhausted',
                            updated_at = %s
                        WHERE state IN ('queued', 'retry_wait')
                          AND attempt_count >= 100
                        """,
                        (now,),
                    )
                    row = connection.execute(
                        """
                        SELECT request_id, run_id, attempt_count
                        FROM subtitle_scan_requests_v2
                        WHERE available_at <= %s
                          AND state IN ('queued', 'retry_wait')
                          AND attempt_count < 100
                        ORDER BY available_at, created_at, request_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                        """,
                        (now,),
                    ).fetchone()
                    if row is None:
                        return None
                    attempt = int(row[2]) + 1
                    expires = now + lease_for
                    connection.execute(
                        """
                        UPDATE subtitle_scan_requests_v2
                        SET state = 'leased', attempt_count = %s,
                            lease_owner = %s, lease_expires_at = %s,
                            last_error = NULL, updated_at = %s
                        WHERE request_id = %s
                        """,
                        (attempt, worker_id, expires, now, str(row[0])),
                    )
                    return SubtitleScanClaim(
                        request_id=str(row[0]),
                        run_id=str(row[1]),
                        worker_id=worker_id,
                        attempt_count=attempt,
                        lease_expires_at=expires,
                    )
        except ServerError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def dispatched(self, claim: SubtitleScanClaim, *, now: datetime) -> None:
        self._finish(claim, now=now, state="dispatched")

    def retry(
        self,
        claim: SubtitleScanClaim,
        *,
        now: datetime,
        delay: timedelta,
        error: str,
    ) -> None:
        if not timedelta(seconds=1) <= delay <= timedelta(hours=1):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        self._finish(
            claim,
            now=now,
            state="retry_wait",
            available_at=now + delay,
            error=error[:128],
        )

    def _finish(
        self,
        claim: SubtitleScanClaim,
        *,
        now: datetime,
        state: str,
        available_at: datetime | None = None,
        error: str | None = None,
    ) -> None:
        effective_state = (
            "blocked"
            if state == "retry_wait" and claim.attempt_count >= 100
            else state
        )
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE subtitle_scan_requests_v2
                        SET state = %s, lease_owner = NULL,
                            lease_expires_at = NULL,
                            available_at = COALESCE(%s, available_at),
                            last_error = %s, updated_at = %s
                        WHERE request_id = %s AND state = 'leased'
                          AND lease_owner = %s AND attempt_count = %s
                          AND lease_expires_at = %s
                          AND lease_expires_at > %s
                        RETURNING request_id
                        """,
                        (
                            effective_state,
                            available_at,
                            error,
                            now,
                            claim.request_id,
                            claim.worker_id,
                            claim.attempt_count,
                            claim.lease_expires_at,
                            now,
                        ),
                    ).fetchone()
                    if row is None:
                        raise ServerError(ServerErrorCode.RUN_BUSY)
        except ServerError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def lineage_allows_automatic_acquisition(self, run_id: str) -> bool:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT r.subtitle_acquisition_lineage_key,
                           EXISTS (
                               SELECT 1
                               FROM subtitle_publication_settlements_v2 AS s
                               WHERE s.lineage_key =
                                   r.subtitle_acquisition_lineage_key
                           ) OR EXISTS (
                               SELECT 1
                               FROM subtitle_acquisition_settlements AS legacy
                               WHERE legacy.lineage_key =
                                   r.subtitle_acquisition_lineage_key
                           )
                    FROM runs AS r
                    WHERE r.run_id = %s
                    """,
                    (run_id,),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        if row is None:
            raise ServerError(ServerErrorCode.RUN_NOT_FOUND)
        return row[0] is None or not bool(row[1])
