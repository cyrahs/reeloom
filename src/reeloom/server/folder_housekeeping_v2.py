from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg_pool import ConnectionPool

from reeloom.executor.folder_housekeeping_v2 import (
    FolderHousekeepingExecutor,
    FolderHousekeepingOutcome,
    housekeeping_target_name,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.errors import ServerError, ServerErrorCode


@dataclass(frozen=True, slots=True)
class FolderHousekeepingClaim:
    housekeeping_id: str
    run_id: str
    config_revision: int
    watch_id: str
    source_folder: str
    target_folder: str
    action: str
    worker_id: str
    attempt_count: int
    lease_expires_at: datetime


def _housekeeping_id(run_id: str) -> str:
    return "folder-housekeeping-v2-" + hashlib.sha256(
        run_id.encode("utf-8")
    ).hexdigest()


class PostgresFolderHousekeepingRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def enqueue_failure(
        self, *, run_id: str, reason_code: str, now: datetime
    ) -> bool:
        """Terminalize one semantic run and queue non-blocking fail cleanup."""

        if not run_id or not reason_code or now.tzinfo is None:
            raise ValueError("invalid housekeeping failure")
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT d.watch_id, d.source_folder, d.inventory_id,
                               r.config_revision, control.effect_plan_hash
                        FROM runs AS r
                        JOIN discoveries AS d USING (discovery_id)
                        JOIN watch_states AS w ON w.watch_id = d.watch_id
                        JOIN run_lifecycle_controls_v2 AS control
                          ON control.run_id = r.run_id
                        WHERE r.run_id = %s
                          AND d.folder_generation_id IS NOT NULL
                          AND w.semantic_v2 = true
                          AND control.mode = 'forward_v2'
                          AND control.operation_id IS NULL
                        FOR UPDATE OF r
                        """,
                        (run_id,),
                    ).fetchone()
                    if row is None:
                        return False
                    source_folder = str(row[1])
                    connection.execute(
                        """
                        INSERT INTO planning_terminal_results_v2
                            (run_id, plan_hash, outcome, reason_code,
                             source_disposition)
                        VALUES (
                            %s, %s,
                            CASE WHEN %s = 'no_supported_video'
                                 THEN 'unsupported_source'
                                 ELSE 'agent_failed' END,
                            %s, 'fail'
                        )
                        ON CONFLICT (run_id) DO NOTHING
                        """,
                        (run_id, row[4], reason_code, reason_code),
                    )
                    connection.execute(
                        """
                        UPDATE runs SET status = 'failed'
                        WHERE run_id = %s
                          AND status NOT IN ('completed', 'superseded')
                        """,
                        (run_id,),
                    )
                    connection.execute(
                        """
                        UPDATE run_states
                        SET phase = 'failed', runtime_status = 'failed',
                            projection_payload = projection_payload
                                || jsonb_build_object(
                                    'phase', 'failed',
                                    'status', 'failed',
                                    'stop_reason', NULL,
                                    'failure_code', %s
                                ),
                            updated_at = clock_timestamp()
                        WHERE run_id = %s
                        """,
                        (reason_code, run_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO handled_folder_inventories_v2
                            (watch_id, source_folder, inventory_id, run_id,
                             terminal_status, handled_at)
                        VALUES (%s, %s, %s, %s, 'agent_failed', %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (str(row[0]), source_folder, str(row[2]), run_id, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO folder_housekeeping_v2
                            (housekeeping_id, run_id, config_revision,
                             watch_id, source_folder, target_folder, action,
                             created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, 'fail', %s, %s)
                        ON CONFLICT (run_id) DO NOTHING
                        """,
                        (
                            _housekeeping_id(run_id),
                            run_id,
                            int(row[3]),
                            str(row[0]),
                            source_folder,
                            housekeeping_target_name(source_folder, run_id),
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO scheduler_audit (event_type, subject_id)
                        VALUES (%s, %s)
                        ON CONFLICT (event_type, subject_id) DO NOTHING
                        """,
                        (f"semantic_failure:{reason_code}"[:128], run_id),
                    )
                    return True
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> FolderHousekeepingClaim | None:
        if not worker_id or now.tzinfo is None:
            raise ValueError("invalid housekeeping claim")
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT housekeeping_id, run_id, config_revision,
                               watch_id, source_folder, target_folder, action,
                               attempt_count
                        FROM folder_housekeeping_v2
                        WHERE attempt_count < 20
                          AND available_at <= %s
                          AND (
                              state IN ('queued', 'retry_wait')
                              OR (state = 'leased' AND lease_expires_at <= %s)
                          )
                        ORDER BY available_at, created_at, housekeeping_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                        """,
                        (now, now),
                    ).fetchone()
                    if row is None:
                        return None
                    attempt = int(row[7]) + 1
                    expires = now + lease_for
                    connection.execute(
                        """
                        UPDATE folder_housekeeping_v2
                        SET state = 'leased', attempt_count = %s,
                            lease_owner = %s, lease_expires_at = %s,
                            warning = NULL, updated_at = %s
                        WHERE housekeeping_id = %s
                        """,
                        (attempt, worker_id, expires, now, str(row[0])),
                    )
                    return FolderHousekeepingClaim(
                        housekeeping_id=str(row[0]),
                        run_id=str(row[1]),
                        config_revision=int(row[2]),
                        watch_id=str(row[3]),
                        source_folder=str(row[4]),
                        target_folder=str(row[5]),
                        action=str(row[6]),
                        worker_id=worker_id,
                        attempt_count=attempt,
                        lease_expires_at=expires,
                    )
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def finish(
        self,
        claim: FolderHousekeepingClaim,
        *,
        completed: bool,
        warning: str | None,
        retry: bool,
        now: datetime,
    ) -> None:
        state = "retry_wait" if retry else ("completed" if completed else "warning")
        recorded_warning = None if state != "warning" else (warning or "unavailable")
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE folder_housekeeping_v2
                        SET state = %s, lease_owner = NULL,
                            lease_expires_at = NULL,
                            available_at = CASE
                                WHEN %s = 'retry_wait'
                                THEN %s + interval '5 seconds'
                                ELSE available_at
                            END,
                            warning = %s, updated_at = %s
                        WHERE housekeeping_id = %s AND state = 'leased'
                          AND lease_owner = %s AND attempt_count = %s
                          AND lease_expires_at = %s
                        RETURNING housekeeping_id
                        """,
                        (
                            state,
                            state,
                            now,
                            recorded_warning,
                            now,
                            claim.housekeeping_id,
                            claim.worker_id,
                            claim.attempt_count,
                            claim.lease_expires_at,
                        ),
                    ).fetchone()
                    if row is None:
                        raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        except ServerError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None


@dataclass(frozen=True, slots=True)
class FolderHousekeepingWorker:
    repository: PostgresFolderHousekeepingRepository
    configs: PostgresConfigRepository
    executor: FolderHousekeepingExecutor

    def enqueue_failure(
        self, *, run_id: str, reason_code: str, now: datetime | None = None
    ) -> bool:
        return self.repository.enqueue_failure(
            run_id=run_id,
            reason_code=reason_code,
            now=datetime.now(UTC) if now is None else now,
        )

    def process_one(self, *, worker_id: str, now: datetime | None = None) -> bool:
        current = datetime.now(UTC) if now is None else now
        claim = self.repository.claim(
            worker_id=worker_id,
            now=current,
            lease_for=timedelta(minutes=1),
        )
        if claim is None:
            return False
        try:
            config = self.configs.get(claim.config_revision)
            watch = next(
                (item for item in config.watches if item.watch_id == claim.watch_id),
                None,
            )
            if watch is None:
                self.repository.finish(
                    claim,
                    completed=False,
                    warning="watch_unavailable",
                    retry=False,
                    now=current,
                )
                return True
            result = self.executor.execute(
                root=Path(watch.root),
                source_folder=claim.source_folder,
                target_folder=claim.target_folder,
                action=claim.action,
            )
            completed = result.outcome is FolderHousekeepingOutcome.COMPLETED
            retry = (
                result.outcome is FolderHousekeepingOutcome.UNAVAILABLE
                and claim.attempt_count < 3
            )
            self.repository.finish(
                claim,
                completed=completed,
                warning=result.warning,
                retry=retry,
                now=current,
            )
        except ServerError as error:
            if error.code is ServerErrorCode.DATABASE_UNAVAILABLE:
                raise
            self.repository.finish(
                claim,
                completed=False,
                warning=error.code.value,
                retry=False,
                now=current,
            )
        except Exception as error:
            self.repository.finish(
                claim,
                completed=False,
                warning=type(error).__name__[:128],
                retry=claim.attempt_count < 3,
                now=current,
            )
        return True
