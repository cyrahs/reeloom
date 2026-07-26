from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from psycopg_pool import ConnectionPool

from reeloom.kernel.candidates import CandidateKind
from reeloom.server.config import ServerWorkType
from reeloom.server.config_repository import CONFIG_LOCK_ID
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.scheduler import (
    AgentJobContext,
    ClaimedJob,
    Discovery,
    JobStatus,
    PollResult,
    RunRegistration,
    _id,
)
from reeloom.server.watcher import WatchFile, WatchSnapshot


def _snapshot_json(snapshot: WatchSnapshot) -> str:
    return json.dumps(
        {
            "files": [
                {
                    "ctime_ns": item.ctime_ns,
                    "device": item.device,
                    "inode": item.inode,
                    "kind": item.kind.value,
                    "mtime_ns": item.mtime_ns,
                    "relative_path": item.relative_path.as_posix(),
                    "sample_digest": item.sample_digest,
                    "size_bytes": item.size_bytes,
                }
                for item in snapshot.files
            ],
            "snapshot_id": snapshot.snapshot_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _snapshot_from_json(value: object) -> WatchSnapshot:
    raw = value if isinstance(value, dict) else json.loads(str(value))
    return WatchSnapshot(
        snapshot_id=raw["snapshot_id"],
        files=tuple(
            WatchFile(
                relative_path=PurePosixPath(item["relative_path"]),
                kind=CandidateKind(item["kind"]),
                size_bytes=item["size_bytes"],
                device=item["device"],
                inode=item["inode"],
                mtime_ns=item["mtime_ns"],
                ctime_ns=item["ctime_ns"],
                sample_digest=item["sample_digest"],
            )
            for item in raw["files"]
        ),
    )


class PostgresSchedulerRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def configure_watch(
        self,
        *,
        watch_id: str,
        config_revision: int,
        fence: int,
        work_type: ServerWorkType,
        settle_interval_seconds: int,
    ) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    previous = connection.execute(
                        """
                        SELECT config_revision, fence
                        FROM watch_states
                        WHERE watch_id = %s
                        FOR UPDATE
                        """,
                        (watch_id,),
                    ).fetchone()
                    connection.execute(
                        """
                        INSERT INTO watch_states
                            (watch_id, config_revision, fence, work_type,
                             settle_interval_seconds)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (watch_id) DO UPDATE SET
                            config_revision = EXCLUDED.config_revision,
                            fence = EXCLUDED.fence,
                            work_type = EXCLUDED.work_type,
                            settle_interval_seconds =
                                EXCLUDED.settle_interval_seconds
                        """,
                        (
                            watch_id,
                            config_revision,
                            fence,
                            work_type.value,
                            settle_interval_seconds,
                        ),
                    )
                    if (
                        previous is None
                        or int(previous[0]) != config_revision
                        or int(previous[1]) != fence
                    ):
                        connection.execute(
                            """
                            DELETE FROM watch_observations
                            WHERE watch_id = %s
                            """,
                            (watch_id,),
                        )
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def reconcile_poll(
        self,
        *,
        watch_id: str,
        config_revision: int,
        fence: int,
        observed_at: datetime,
        snapshot: WatchSnapshot,
    ) -> PollResult:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (CONFIG_LOCK_ID,),
                    )
                    head = connection.execute(
                        """
                        SELECT revision
                        FROM config_heads
                        WHERE singleton = true
                        """
                    ).fetchone()
                    if head is None or int(head[0]) != config_revision:
                        raise ServerError(
                            ServerErrorCode.STALE_WATCH_SCAN
                        )
                    state = connection.execute(
                        """
                        SELECT config_revision, fence, work_type,
                               settle_interval_seconds
                        FROM watch_states
                        WHERE watch_id = %s
                        FOR UPDATE
                        """,
                        (watch_id,),
                    ).fetchone()
                    if state is None:
                        raise ServerError(
                            ServerErrorCode.WATCH_NOT_FOUND
                        )
                    if int(state[0]) != config_revision or int(state[1]) != fence:
                        raise ServerError(
                            ServerErrorCode.STALE_WATCH_SCAN
                        )
                    rows = connection.execute(
                        """
                        SELECT relative_path, kind, size_bytes, device, inode,
                               mtime_ns, ctime_ns, sample_digest,
                               first_observed_at, stable_at
                        FROM watch_observations
                        WHERE watch_id = %s
                        """,
                        (watch_id,),
                    ).fetchall()
                    previous = {str(row[0]): row for row in rows}
                    current = {
                        item.relative_path.as_posix(): item
                        for item in snapshot.files
                    }
                    mutated = False
                    removed = tuple(set(previous) - set(current))
                    if removed:
                        connection.execute(
                            """
                            DELETE FROM watch_observations
                            WHERE watch_id = %s
                              AND relative_path = ANY(%s)
                            """,
                            (watch_id, list(removed)),
                        )
                        mutated = True
                    first_seen: dict[str, datetime] = {}
                    stable_at: dict[str, datetime | None] = {}
                    for path, item in current.items():
                        row = previous.get(path)
                        identity = (
                            item.kind.value,
                            item.size_bytes,
                            item.device,
                            item.inode,
                            item.mtime_ns,
                            item.ctime_ns,
                            item.sample_digest,
                        )
                        old_identity = None if row is None else tuple(row[1:8])
                        if old_identity != identity:
                            connection.execute(
                                """
                                INSERT INTO watch_observations
                                    (watch_id, relative_path, kind, size_bytes,
                                     device, inode, mtime_ns, ctime_ns,
                                     sample_digest, first_observed_at, stable_at)
                                VALUES
                                    (%s, %s, %s, %s, %s, %s, %s, %s,
                                     %s, %s, NULL)
                                ON CONFLICT (watch_id, relative_path)
                                DO UPDATE SET
                                    kind = EXCLUDED.kind,
                                    size_bytes = EXCLUDED.size_bytes,
                                    device = EXCLUDED.device,
                                    inode = EXCLUDED.inode,
                                    mtime_ns = EXCLUDED.mtime_ns,
                                    ctime_ns = EXCLUDED.ctime_ns,
                                    sample_digest = EXCLUDED.sample_digest,
                                    first_observed_at =
                                        EXCLUDED.first_observed_at,
                                    stable_at = NULL
                                """,
                                (
                                    watch_id,
                                    path,
                                    *identity,
                                    observed_at,
                                ),
                            )
                            first_seen[path] = observed_at
                            stable_at[path] = None
                            mutated = True
                        else:
                            first_seen[path] = row[8]
                            stable_at[path] = row[9]

                    threshold = timedelta(seconds=int(state[3]))
                    has_video = any(
                        item.kind is CandidateKind.VIDEO
                        for item in current.values()
                    )
                    all_settled = bool(current) and has_video
                    for path in current:
                        if stable_at[path] is None:
                            if observed_at - first_seen[path] >= threshold:
                                connection.execute(
                                    """
                                    UPDATE watch_observations
                                    SET stable_at = %s
                                    WHERE watch_id = %s
                                      AND relative_path = %s
                                      AND stable_at IS NULL
                                    """,
                                    (observed_at, watch_id, path),
                                )
                                stable_at[path] = observed_at
                                mutated = True
                            else:
                                all_settled = False

                    discovery: Discovery | None = None
                    if all_settled:
                        discovery_id = _id(
                            "discovery",
                            watch_id,
                            str(config_revision),
                            snapshot.snapshot_id,
                        )
                        inserted = connection.execute(
                            """
                            INSERT INTO discoveries
                                (discovery_id, watch_id, config_revision,
                                 snapshot_id, snapshot_payload, work_type,
                                 discovered_at)
                            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                            ON CONFLICT
                                (watch_id, config_revision, snapshot_id)
                            DO NOTHING
                            RETURNING discovery_id
                            """,
                            (
                                discovery_id,
                                watch_id,
                                config_revision,
                                snapshot.snapshot_id,
                                _snapshot_json(snapshot),
                                str(state[2]),
                                observed_at,
                            ),
                        ).fetchone()
                        if inserted is not None:
                            connection.execute(
                                """
                                INSERT INTO scheduler_audit
                                    (event_type, subject_id)
                                VALUES ('discovery_stable', %s)
                                """,
                                (discovery_id,),
                            )
                            mutated = True
                        row = connection.execute(
                            """
                            SELECT discovery_id, work_type, discovered_at,
                                   snapshot_payload
                            FROM discoveries
                            WHERE watch_id = %s
                              AND config_revision = %s
                              AND snapshot_id = %s
                            """,
                            (
                                watch_id,
                                config_revision,
                                snapshot.snapshot_id,
                            ),
                        ).fetchone()
                        discovery = Discovery(
                            discovery_id=str(row[0]),
                            watch_id=watch_id,
                            config_revision=config_revision,
                            snapshot_id=snapshot.snapshot_id,
                            work_type=ServerWorkType(str(row[1])),
                            discovered_at=row[2],
                            snapshot=_snapshot_from_json(row[3]),
                        )
                    return PollResult(
                        mutated=mutated,
                        discovery=discovery,
                    )
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def register_run(self, *, discovery_id: str) -> RunRegistration:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    discovery = connection.execute(
                        """
                        SELECT config_revision, work_type
                        FROM discoveries
                        WHERE discovery_id = %s
                        """,
                        (discovery_id,),
                    ).fetchone()
                    if discovery is None:
                        raise ServerError(
                            ServerErrorCode.DISCOVERY_NOT_FOUND
                    )
                    run_id = _id("run", discovery_id)
                    capability = _id("capability", run_id)
                    connection.execute(
                        """
                        INSERT INTO runs
                            (run_id, discovery_id, config_revision, work_type,
                             source_capability, status)
                        VALUES (%s, %s, %s, %s, %s, 'registered')
                        ON CONFLICT (discovery_id) DO NOTHING
                        """,
                        (
                            run_id,
                            discovery_id,
                            int(discovery[0]),
                            str(discovery[1]),
                            capability,
                        ),
                    )
                    row = connection.execute(
                        """
                        SELECT run_id, config_revision, work_type,
                               source_capability
                        FROM runs
                        WHERE discovery_id = %s
                        """,
                        (discovery_id,),
                    ).fetchone()
                    actual_run = str(row[0])
                    actual_job = _id("job", actual_run)
                    connection.execute(
                        """
                        INSERT INTO jobs (job_id, run_id, status)
                        VALUES (%s, %s, 'pending')
                        ON CONFLICT (run_id) DO NOTHING
                        """,
                        (actual_job, actual_run),
                    )
                    connection.execute(
                        """
                        INSERT INTO scheduler_audit
                            (event_type, subject_id)
                        SELECT 'run_registered', %s
                        WHERE NOT EXISTS (
                            SELECT 1 FROM scheduler_audit
                            WHERE event_type = 'run_registered'
                              AND subject_id = %s
                        )
                        """,
                        (actual_run, actual_run),
                    )
                    return RunRegistration(
                        run_id=actual_run,
                        job_id=actual_job,
                        discovery_id=discovery_id,
                        config_revision=int(row[1]),
                        work_type=ServerWorkType(str(row[2])),
                        source_capability=str(row[3]),
                    )
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def claim_job(self, *, boot_id: str) -> ClaimedJob | None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT job_id, run_id
                        FROM jobs
                        WHERE status = 'pending'
                        ORDER BY updated_at, job_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                        """
                    ).fetchone()
                    if row is None:
                        return None
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'running', boot_id = %s,
                            updated_at = clock_timestamp()
                        WHERE job_id = %s
                        """,
                        (boot_id, str(row[0])),
                    )
                    return ClaimedJob(
                        job_id=str(row[0]),
                        run_id=str(row[1]),
                        boot_id=boot_id,
                        status=JobStatus.RUNNING,
                    )
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def reconcile_boot(self, *, current_boot_id: str) -> int:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    terminal = connection.execute(
                        """
                        UPDATE jobs AS job
                        SET status = CASE
                                WHEN run.status = 'completed'
                                THEN 'completed'
                                ELSE 'failed'
                            END,
                            updated_at = clock_timestamp()
                        FROM runs AS run
                        WHERE job.run_id = run.run_id
                          AND job.status = 'running'
                          AND job.boot_id IS DISTINCT FROM %s
                          AND run.status IN (
                              'completed', 'failed', 'rolled_back'
                          )
                        RETURNING job.job_id, job.status
                        """,
                        (current_boot_id,),
                    ).fetchall()
                    pending = connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'pending', boot_id = NULL,
                            updated_at = clock_timestamp()
                        WHERE status = 'running'
                          AND boot_id IS DISTINCT FROM %s
                        RETURNING job_id
                        """,
                        (current_boot_id,),
                    ).fetchall()
                    audits = [
                        (
                            (
                                "job_completed"
                                if str(row[1]) == "completed"
                                else "job_failed"
                            ),
                            str(row[0]),
                        )
                        for row in terminal
                    ]
                    audits.extend(
                        ("job_reconciled", str(row[0]))
                        for row in pending
                    )
                    for event_type, job_id in audits:
                        connection.execute(
                            """
                            INSERT INTO scheduler_audit
                                (event_type, subject_id)
                            VALUES (%s, %s)
                            ON CONFLICT (event_type, subject_id) DO NOTHING
                            """,
                            (event_type, job_id),
                        )
                    return len(terminal) + len(pending)
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def get_job_context(self, *, run_id: str) -> AgentJobContext:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT r.run_id, j.job_id, r.discovery_id,
                           r.config_revision, r.work_type,
                           r.source_capability,
                           d.watch_id, d.snapshot_id,
                           d.snapshot_payload, d.discovered_at
                    FROM runs AS r
                    JOIN jobs AS j ON j.run_id = r.run_id
                    JOIN discoveries AS d
                      ON d.discovery_id = r.discovery_id
                    WHERE r.run_id = %s
                    """,
                    (run_id,),
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        if row is None:
            raise ServerError(ServerErrorCode.DISCOVERY_NOT_FOUND)
        registration = RunRegistration(
            run_id=str(row[0]),
            job_id=str(row[1]),
            discovery_id=str(row[2]),
            config_revision=int(row[3]),
            work_type=ServerWorkType(str(row[4])),
            source_capability=str(row[5]),
        )
        discovery = Discovery(
            discovery_id=registration.discovery_id,
            watch_id=str(row[6]),
            config_revision=registration.config_revision,
            snapshot_id=str(row[7]),
            work_type=registration.work_type,
            discovered_at=row[9],
            snapshot=_snapshot_from_json(row[8]),
        )
        return AgentJobContext(registration, discovery)

    def settle_job(
        self,
        *,
        job_id: str,
        boot_id: str,
        succeeded: bool,
    ) -> None:
        status = "completed" if succeeded else "failed"
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE jobs
                        SET status = %s, updated_at = clock_timestamp()
                        WHERE job_id = %s
                          AND status = 'running'
                          AND boot_id = %s
                        RETURNING run_id
                        """,
                        (status, job_id, boot_id),
                    ).fetchone()
                    if row is None:
                        raise ServerError(
                            ServerErrorCode.JOB_NOT_FOUND
                        )
                    connection.execute(
                        """
                        INSERT INTO scheduler_audit
                            (event_type, subject_id)
                        VALUES (%s, %s)
                        ON CONFLICT (event_type, subject_id) DO NOTHING
                        """,
                        (
                            (
                                "job_completed"
                                if succeeded
                                else "job_failed"
                            ),
                            job_id,
                        ),
                    )
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
