from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Literal

from psycopg_pool import ConnectionPool

from reeloom.kernel.candidates import CandidateKind
from reeloom.kernel.semantic_identity import SemanticCandidateSnapshot
from reeloom.server.config import ServerWorkType
from reeloom.server.config_repository import CONFIG_LOCK_ID
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.scheduler import (
    AgentJobContext,
    ClaimedJob,
    Discovery,
    FolderPollResult,
    JobStatus,
    PollResult,
    RunRegistration,
    _id,
)
from reeloom.server.watcher import (
    FolderScan,
    FolderSnapshot,
    WatchFile,
    WatchSnapshot,
)


def _snapshot_json(
    snapshot: WatchSnapshot,
    *,
    semantic_v2: bool = False,
) -> str:
    if semantic_v2:
        semantic = snapshot.semantic_snapshot
        return json.dumps(
            {
                "files": semantic.payload(),
                "schema": "semantic-watch-v2",
                "snapshot_id": semantic.snapshot_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
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
    if not isinstance(raw, dict):
        raise ValueError("invalid snapshot payload")
    if raw.get("schema") == "semantic-watch-v2":
        semantic = SemanticCandidateSnapshot.from_payload(
            raw["files"],
            snapshot_id=raw["snapshot_id"],
        )
        snapshot = WatchSnapshot(
            snapshot_id=semantic.snapshot_id,
            files=tuple(
                WatchFile(
                    relative_path=source.relative_path,
                    kind=source.kind,
                    size_bytes=source.size_bytes,
                    device=0,
                    inode=0,
                    mtime_ns=0,
                    ctime_ns=0,
                    sample_digest=None,
                    sha256=source.sha256,
                )
                for source in semantic.sources
            ),
        )
        if snapshot.semantic_snapshot_id != semantic.snapshot_id:
            raise ValueError("semantic snapshot identity mismatch")
        return snapshot
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


def _inventory_json(
    folder: FolderSnapshot,
    *,
    semantic_v2: bool = False,
) -> str:
    if semantic_v2:
        return json.dumps(
            {
                "candidate_snapshot_id": (
                    folder.candidates.semantic_snapshot_id
                ),
                "entries": [item.semantic_payload for item in folder.entries],
                "inventory_id": folder.semantic_inventory_id,
                "schema": "folder-inventory-v2",
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    return json.dumps(
        {
            "candidate_snapshot_id": folder.candidates.snapshot_id,
            "device": folder.device,
            "entries": [item.payload for item in folder.entries],
            "inode": folder.inode,
            "inventory_id": folder.inventory_id,
            "name": folder.name,
            "schema": "folder-inventory-v1",
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _missing_folder_action(
    *,
    status: str,
    has_discovery: bool,
    terminal_ready: bool,
    elapsed_seconds: float,
    settle_interval_seconds: int,
) -> Literal["keep", "remove", "start_missing", "confirm_missing"]:
    if not has_discovery or status == "settled":
        return "remove"
    if not terminal_ready:
        return "keep"
    if status == "active":
        return "start_missing"
    if (
        status == "settling"
        and elapsed_seconds >= settle_interval_seconds
    ):
        return "confirm_missing"
    return "keep"


def _invalidate_unclaimed(
    connection: Any, *, discovery_id: str
) -> bool:
    locked = connection.execute(
        """
        SELECT r.run_id, j.job_id
        FROM runs AS r
        JOIN jobs AS j ON j.run_id = r.run_id
        WHERE r.discovery_id = %s
          AND r.status IN (
              'registered', 'running', 'awaiting_approval',
              'failed', 'rolled_back'
          )
        FOR UPDATE OF r, j
        """,
        (discovery_id,),
    ).fetchone()
    if locked is None:
        return False
    return (
        connection.execute(
            """
            WITH eligible AS (
                SELECT r.run_id, j.job_id
                FROM runs AS r
                JOIN jobs AS j ON j.run_id = r.run_id
                WHERE r.discovery_id = %s
                  AND r.status IN (
                      'registered', 'running', 'awaiting_approval',
                      'failed', 'rolled_back'
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM approval_claims AS claim
                      JOIN approvals AS approval USING (approval_id)
                      LEFT JOIN approval_settlements AS settlement
                        USING (approval_id)
                      WHERE approval.run_id = r.run_id
                        AND settlement.approval_id IS NULL
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM run_operations AS operation
                      WHERE operation.run_id = r.run_id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM subtitle_acquisition_requests AS request
                      WHERE request.run_id = r.run_id
                        AND request.status IN ('planned', 'approved')
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM folder_disposition_claims AS claim
                      JOIN folder_disposition_approvals AS approval
                        USING (approval_id)
                      LEFT JOIN folder_disposition_transactions AS txn
                        USING (approval_id)
                      WHERE approval.run_id = r.run_id
                        AND (
                            txn.status IS NULL
                            OR txn.status <> 'blocked'
                        )
                  )
            ),
            failed_run AS (
                UPDATE runs AS r
                SET status = 'failed'
                FROM eligible AS e
                WHERE r.run_id = e.run_id
                RETURNING r.run_id
            )
            UPDATE jobs AS j
            SET status = 'failed', updated_at = clock_timestamp()
            FROM eligible AS e
            WHERE j.job_id = e.job_id
            RETURNING j.job_id
            """,
            (discovery_id,),
        ).fetchone()
        is not None
    )


def _terminal_missing_ready(
    connection: Any, *, discovery_id: str
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM runs AS r
        JOIN jobs AS j ON j.run_id = r.run_id
        WHERE r.discovery_id = %s
          AND r.status IN ('failed', 'rolled_back')
          AND j.status IN ('completed', 'failed')
          AND NOT EXISTS (
              SELECT 1 FROM run_operations AS operation
              WHERE operation.run_id = r.run_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM interactions AS interaction
              WHERE interaction.run_id = r.run_id
                AND interaction.status = 'active'
          )
          AND NOT EXISTS (
              SELECT 1
              FROM approval_claims AS claim
              LEFT JOIN approval_settlements AS settled
                ON settled.approval_id = claim.approval_id
              WHERE claim.run_id = r.run_id
                AND settled.approval_id IS NULL
          )
          AND NOT EXISTS (
              SELECT 1
              FROM folder_disposition_approvals AS approval
              JOIN folder_disposition_claims AS claim
                ON claim.approval_id = approval.approval_id
              LEFT JOIN folder_disposition_settlements AS settled
                ON settled.approval_id = claim.approval_id
              WHERE approval.run_id = r.run_id
                AND settled.approval_id IS NULL
          )
        FOR UPDATE OF r, j
        """,
        (discovery_id,),
    ).fetchone()
    return row is not None


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
        semantic_v2: bool = False,
    ) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    previous = connection.execute(
                        """
                        SELECT config_revision, fence, semantic_v2
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
                             settle_interval_seconds, semantic_v2)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (watch_id) DO UPDATE SET
                            config_revision = EXCLUDED.config_revision,
                            fence = EXCLUDED.fence,
                            work_type = EXCLUDED.work_type,
                            settle_interval_seconds =
                                EXCLUDED.settle_interval_seconds,
                            semantic_v2 = EXCLUDED.semantic_v2
                        """,
                        (
                            watch_id,
                            config_revision,
                            fence,
                            work_type.value,
                            settle_interval_seconds,
                            semantic_v2,
                        ),
                    )
                    if (
                        previous is None
                        or int(previous[0]) != config_revision
                        or int(previous[1]) != fence
                        or bool(previous[2]) != semantic_v2
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
                            ON CONFLICT DO NOTHING
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

    def reconcile_folders(
        self,
        *,
        watch_id: str,
        config_revision: int,
        fence: int,
        observed_at: datetime,
        scan: FolderScan,
    ) -> FolderPollResult:
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
                               settle_interval_seconds, semantic_v2
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
                    semantic_v2 = bool(state[4])
                    rows = connection.execute(
                        """
                        SELECT folder_name, config_revision, folder_device,
                               folder_inode, inventory_id, snapshot_id,
                               first_observed_at, stable_at, discovery_id,
                               status, blocked_reason
                        FROM watch_folder_observations
                        WHERE watch_id = %s
                        FOR UPDATE
                        """,
                        (watch_id,),
                    ).fetchall()
                    previous = {str(row[0]): row for row in rows}
                    current_names = {
                        item.name for item in scan.folders
                    } | {item.name for item in scan.blocked}
                    threshold = timedelta(seconds=int(state[3]))
                    removable = [
                        name
                        for name, row in previous.items()
                        if name not in current_names
                        and (row[8] is None or str(row[9]) == "settled")
                    ]
                    mutated = False
                    if removable:
                        connection.execute(
                            """
                            DELETE FROM watch_folder_observations
                            WHERE watch_id = %s
                              AND folder_name = ANY(%s)
                            """,
                            (watch_id, removable),
                        )
                        mutated = True
                    for name, row in previous.items():
                        if (
                            name in current_names
                            or name in removable
                            or row[8] is None
                        ):
                            continue
                        terminal_ready = _terminal_missing_ready(
                            connection,
                            discovery_id=str(row[8]),
                        )
                        action = _missing_folder_action(
                            status=str(row[9]),
                            has_discovery=True,
                            terminal_ready=terminal_ready,
                            elapsed_seconds=(
                                observed_at - row[6]
                            ).total_seconds(),
                            settle_interval_seconds=int(state[3]),
                        )
                        if action == "start_missing":
                            connection.execute(
                                """
                                UPDATE watch_folder_observations
                                SET status = 'settling',
                                    first_observed_at = %s,
                                    stable_at = NULL
                                WHERE watch_id = %s
                                  AND folder_name = %s
                                  AND discovery_id = %s
                                  AND status = 'active'
                                """,
                                (
                                    observed_at,
                                    watch_id,
                                    name,
                                    str(row[8]),
                                ),
                            )
                            mutated = True
                        elif action == "confirm_missing":
                            confirmed = connection.execute(
                                """
                                UPDATE watch_folder_observations
                                SET status = 'blocked',
                                    blocked_reason =
                                        'source_folder_missing',
                                    stable_at = %s
                                WHERE watch_id = %s
                                  AND folder_name = %s
                                  AND discovery_id = %s
                                  AND status = 'settling'
                                RETURNING discovery_id
                                """,
                                (
                                    observed_at,
                                    watch_id,
                                    name,
                                    str(row[8]),
                                ),
                            ).fetchone()
                            if confirmed is not None:
                                connection.execute(
                                    """
                                    INSERT INTO scheduler_audit
                                        (event_type, subject_id)
                                    VALUES (
                                        'folder_source_missing', %s
                                    )
                                    ON CONFLICT (
                                        event_type, subject_id
                                    ) DO NOTHING
                                    """,
                                    (str(confirmed[0]),),
                                )
                                mutated = True
                    for blocked in scan.blocked:
                        row = previous.get(blocked.name)
                        if row is not None and row[8] is not None:
                            if not _invalidate_unclaimed(
                                connection,
                                discovery_id=str(row[8]),
                            ):
                                continue
                            connection.execute(
                                """
                                UPDATE watch_folder_observations
                                SET discovery_id = NULL,
                                    status = 'settling',
                                    stable_at = NULL
                                WHERE watch_id = %s
                                  AND folder_name = %s
                                  AND discovery_id = %s
                                """,
                                (watch_id, blocked.name, str(row[8])),
                            )
                        connection.execute(
                            """
                            INSERT INTO watch_folder_observations
                                (watch_id, folder_name, config_revision,
                                 first_observed_at, status, blocked_reason)
                            VALUES (%s, %s, %s, %s, 'blocked', %s)
                            ON CONFLICT (watch_id, folder_name)
                            DO UPDATE SET
                                config_revision = EXCLUDED.config_revision,
                                folder_device = NULL,
                                folder_inode = NULL,
                                inventory_id = NULL,
                                inventory_payload = NULL,
                                snapshot_id = NULL,
                                snapshot_payload = NULL,
                                first_observed_at =
                                    EXCLUDED.first_observed_at,
                                stable_at = NULL,
                                discovery_id = NULL,
                                status = 'blocked',
                                blocked_reason = EXCLUDED.blocked_reason,
                                retry_count = 0
                            WHERE
                                watch_folder_observations.discovery_id IS NULL
                                OR watch_folder_observations.status = 'settled'
                            """,
                            (
                                watch_id,
                                blocked.name,
                                config_revision,
                                observed_at,
                                blocked.reason,
                            ),
                        )
                        mutated = True

                    legacy_active = connection.execute(
                        """
                        SELECT 1
                        FROM discoveries AS d
                        JOIN runs AS r ON r.discovery_id = d.discovery_id
                        WHERE d.watch_id = %s
                          AND d.folder_generation_id IS NULL
                          AND r.status NOT IN (
                              'completed', 'failed', 'rolled_back',
                              'superseded'
                          )
                        LIMIT 1
                        """,
                        (watch_id,),
                    ).fetchone() is not None
                    discoveries: list[Discovery] = []
                    disposition_runs: list[str] = []
                    for folder in scan.folders:
                        folder_device = (
                            None if semantic_v2 else folder.device
                        )
                        folder_inode = None if semantic_v2 else folder.inode
                        inventory_id = (
                            folder.semantic_inventory_id
                            if semantic_v2
                            else folder.inventory_id
                        )
                        snapshot_id = (
                            folder.candidates.semantic_snapshot_id
                            if semantic_v2
                            else folder.candidates.snapshot_id
                        )
                        row = previous.get(folder.name)
                        if row is not None and row[8] is not None:
                            if (
                                str(row[9]) == "blocked"
                                and str(row[10])
                                == "source_folder_missing"
                            ):
                                if not _invalidate_unclaimed(
                                    connection,
                                    discovery_id=str(row[8]),
                                ):
                                    continue
                                connection.execute(
                                    """
                                    UPDATE watch_folder_observations
                                    SET discovery_id = NULL,
                                        status = 'settling',
                                        blocked_reason = NULL,
                                        first_observed_at = %s,
                                        stable_at = NULL,
                                        retry_count = 0
                                    WHERE watch_id = %s
                                      AND folder_name = %s
                                      AND discovery_id = %s
                                      AND status = 'blocked'
                                      AND blocked_reason =
                                          'source_folder_missing'
                                    """,
                                    (
                                        observed_at,
                                        watch_id,
                                        folder.name,
                                        str(row[8]),
                                    ),
                                )
                                row = None
                                mutated = True
                            elif str(row[9]) == "settled":
                                connection.execute(
                                    """
                                    UPDATE watch_folder_observations
                                    SET discovery_id = NULL,
                                        status = 'settling',
                                        stable_at = NULL
                                    WHERE watch_id = %s
                                      AND folder_name = %s
                                    """,
                                    (watch_id, folder.name),
                                )
                                row = None
                                mutated = True
                            else:
                                run = connection.execute(
                                    """
                                    SELECT r.run_id, r.status,
                                           EXISTS (
                                               SELECT 1
                                               FROM folder_disposition_claims c
                                               JOIN folder_disposition_approvals a
                                                 USING (approval_id)
                                               LEFT JOIN folder_disposition_transactions t
                                                 USING (approval_id)
                                               WHERE a.run_id = r.run_id
                                                 AND (
                                                     t.status IS NULL
                                                     OR t.status <> 'blocked'
                                                 )
                                           ),
                                           EXISTS (
                                               SELECT 1
                                               FROM folder_disposition_settlements s
                                               JOIN folder_disposition_approvals a
                                                 USING (approval_id)
                                               WHERE a.run_id = r.run_id
                                           ),
                                           EXISTS (
                                               SELECT 1
                                               FROM folder_disposition_plans p
                                               JOIN folder_disposition_approvals a
                                                 ON a.run_id = p.run_id
                                                AND a.plan_hash = p.plan_hash
                                               JOIN folder_disposition_transactions t
                                                 USING (approval_id)
                                               WHERE p.run_id = r.run_id
                                                 AND t.status = 'blocked'
                                                 AND p.plan_hash = (
                                                     SELECT p2.plan_hash
                                                     FROM folder_disposition_plans p2
                                                     WHERE p2.run_id = r.run_id
                                                     ORDER BY p2.created_at DESC,
                                                              p2.plan_hash DESC
                                                     LIMIT 1
                                                 )
                                           )
                                    FROM runs AS r
                                    WHERE r.discovery_id = %s
                                    """,
                                    (str(row[8]),),
                                ).fetchone()
                                if run is None:
                                    raise ServerError(
                                        ServerErrorCode.DATABASE_UNAVAILABLE
                                    )
                                unchanged = (
                                    (
                                        semantic_v2
                                        or (
                                            row[2] == folder.device
                                            and row[3] == folder.inode
                                        )
                                    )
                                    and row[4] == inventory_id
                                    and row[5] == snapshot_id
                                )
                                if unchanged:
                                    if (
                                        str(row[9]) == "active"
                                        and (
                                            bool(run[4])
                                            or (
                                                str(run[1])
                                                in {"completed", "failed"}
                                                and not bool(run[2])
                                                and not bool(run[3])
                                            )
                                        )
                                    ):
                                        disposition_runs.append(str(run[0]))
                                        continue
                                    if (
                                        str(row[9]) == "settling"
                                        and observed_at - row[6] >= threshold
                                    ):
                                        connection.execute(
                                            """
                                            UPDATE watch_folder_observations
                                            SET status = 'active',
                                                stable_at = %s
                                            WHERE watch_id = %s
                                              AND folder_name = %s
                                              AND discovery_id = %s
                                            """,
                                            (
                                                observed_at,
                                                watch_id,
                                                folder.name,
                                                str(row[8]),
                                            ),
                                        )
                                        disposition_runs.append(str(run[0]))
                                        mutated = True
                                    continue
                                if (
                                    str(run[1]) == "completed"
                                    and not bool(run[2])
                                    and not bool(run[3])
                                ):
                                    connection.execute(
                                        """
                                        UPDATE watch_folder_observations
                                        SET config_revision = %s,
                                            folder_device = %s,
                                            folder_inode = %s,
                                            inventory_id = %s,
                                            inventory_payload = %s::jsonb,
                                            snapshot_id = %s,
                                            snapshot_payload = %s::jsonb,
                                            first_observed_at = %s,
                                            stable_at = NULL,
                                            status = 'settling',
                                            retry_count = 0
                                        WHERE watch_id = %s
                                          AND folder_name = %s
                                          AND discovery_id = %s
                                        """,
                                        (
                                            config_revision,
                                            folder_device,
                                            folder_inode,
                                            inventory_id,
                                            _inventory_json(
                                                folder,
                                                semantic_v2=semantic_v2,
                                            ),
                                            snapshot_id,
                                            _snapshot_json(
                                                folder.candidates,
                                                semantic_v2=semantic_v2,
                                            ),
                                            observed_at,
                                            watch_id,
                                            folder.name,
                                            str(row[8]),
                                        ),
                                    )
                                    mutated = True
                                    continue
                                if not _invalidate_unclaimed(
                                    connection,
                                    discovery_id=str(row[8]),
                                ):
                                    continue
                                connection.execute(
                                    """
                                    UPDATE watch_folder_observations
                                    SET discovery_id = NULL,
                                        status = 'settling',
                                        stable_at = NULL
                                    WHERE watch_id = %s
                                      AND folder_name = %s
                                      AND discovery_id = %s
                                    """,
                                    (watch_id, folder.name, str(row[8])),
                                )
                                row = None
                                mutated = True
                        same = (
                            row is not None
                            and int(row[1]) == config_revision
                            and (
                                semantic_v2
                                or (
                                    row[2] == folder.device
                                    and row[3] == folder.inode
                                )
                            )
                            and row[4] == inventory_id
                            and row[5] == snapshot_id
                            and str(row[9]) == "settling"
                        )
                        if not same:
                            connection.execute(
                                """
                                INSERT INTO watch_folder_observations
                                    (watch_id, folder_name, config_revision,
                                     folder_device, folder_inode, inventory_id,
                                     inventory_payload, snapshot_id,
                                     snapshot_payload, first_observed_at,
                                     stable_at, discovery_id, status,
                                     blocked_reason)
                                VALUES
                                    (%s, %s, %s, %s, %s, %s, %s::jsonb,
                                     %s, %s::jsonb, %s, NULL, NULL,
                                     'settling', NULL)
                                ON CONFLICT (watch_id, folder_name)
                                DO UPDATE SET
                                    config_revision =
                                        EXCLUDED.config_revision,
                                    folder_device = EXCLUDED.folder_device,
                                    folder_inode = EXCLUDED.folder_inode,
                                    inventory_id = EXCLUDED.inventory_id,
                                    inventory_payload =
                                        EXCLUDED.inventory_payload,
                                    snapshot_id = EXCLUDED.snapshot_id,
                                    snapshot_payload =
                                        EXCLUDED.snapshot_payload,
                                    first_observed_at =
                                        EXCLUDED.first_observed_at,
                                    stable_at = NULL,
                                    discovery_id = NULL,
                                    status = 'settling',
                                    blocked_reason = NULL,
                                    retry_count = 0
                                WHERE
                                    watch_folder_observations.discovery_id
                                        IS NULL
                                    OR watch_folder_observations.status =
                                        'settled'
                                """,
                                (
                                    watch_id,
                                    folder.name,
                                    config_revision,
                                    folder_device,
                                    folder_inode,
                                    inventory_id,
                                    _inventory_json(
                                        folder,
                                        semantic_v2=semantic_v2,
                                    ),
                                    snapshot_id,
                                    _snapshot_json(
                                        folder.candidates,
                                        semantic_v2=semantic_v2,
                                    ),
                                    observed_at,
                                ),
                            )
                            first_observed = observed_at
                            stable_at = None
                            mutated = True
                        else:
                            first_observed = row[6]
                            stable_at = row[7]
                        if (
                            legacy_active
                            or (
                                stable_at is None
                                and observed_at - first_observed < threshold
                            )
                        ):
                            continue
                        if stable_at is None:
                            connection.execute(
                                """
                                UPDATE watch_folder_observations
                                SET stable_at = %s
                                WHERE watch_id = %s AND folder_name = %s
                                  AND status = 'settling'
                                  AND stable_at IS NULL
                                """,
                                (observed_at, watch_id, folder.name),
                            )
                            mutated = True
                        generation_parts = (
                            (
                                watch_id,
                                folder.name,
                                inventory_id,
                                first_observed.isoformat(),
                            )
                            if semantic_v2
                            else (
                                watch_id,
                                folder.name,
                                str(folder.device),
                                str(folder.inode),
                                inventory_id,
                                first_observed.isoformat(),
                            )
                        )
                        generation_id = _id("folder", *generation_parts)
                        discovery_id = _id(
                            "discovery",
                            watch_id,
                            str(config_revision),
                            generation_id,
                            snapshot_id,
                        )
                        inserted = connection.execute(
                            """
                            INSERT INTO discoveries
                                (discovery_id, watch_id, config_revision,
                                 snapshot_id, snapshot_payload, work_type,
                                 discovered_at, source_folder,
                                 folder_generation_id, inventory_id)
                            VALUES
                                (%s, %s, %s, %s, %s::jsonb, %s, %s,
                                 %s, %s, %s)
                            ON CONFLICT DO NOTHING
                            RETURNING discovery_id
                            """,
                            (
                                discovery_id,
                                watch_id,
                                config_revision,
                                snapshot_id,
                                _snapshot_json(
                                    folder.candidates,
                                    semantic_v2=semantic_v2,
                                ),
                                str(state[2]),
                                observed_at,
                                folder.name,
                                generation_id,
                                inventory_id,
                            ),
                        ).fetchone()
                        discovery_row = connection.execute(
                            """
                            SELECT discovery_id, discovered_at
                            FROM discoveries
                            WHERE folder_generation_id = %s
                            """,
                            (generation_id,),
                        ).fetchone()
                        if discovery_row is None:
                            raise ServerError(
                                ServerErrorCode.DATABASE_UNAVAILABLE
                            )
                        connection.execute(
                            """
                            UPDATE watch_folder_observations
                            SET discovery_id = %s, status = 'active',
                                stable_at = COALESCE(stable_at, %s)
                            WHERE watch_id = %s AND folder_name = %s
                              AND inventory_id = %s
                            """,
                            (
                                str(discovery_row[0]),
                                observed_at,
                                watch_id,
                                folder.name,
                                inventory_id,
                            ),
                        )
                        if inserted is not None:
                            connection.execute(
                                """
                                INSERT INTO scheduler_audit
                                    (event_type, subject_id)
                                VALUES ('folder_discovery_stable', %s)
                                ON CONFLICT (event_type, subject_id)
                                DO NOTHING
                                """,
                                (str(discovery_row[0]),),
                            )
                            mutated = True
                        discoveries.append(
                            Discovery(
                                discovery_id=str(discovery_row[0]),
                                watch_id=watch_id,
                                config_revision=config_revision,
                                snapshot_id=snapshot_id,
                                work_type=ServerWorkType(str(state[2])),
                                discovered_at=discovery_row[1],
                                snapshot=folder.candidates,
                                source_folder=folder.name,
                                folder_generation_id=generation_id,
                                inventory_id=inventory_id,
                                source_folder_device=folder_device,
                                source_folder_inode=folder_inode,
                            )
                        )
                    return FolderPollResult(
                        mutated=mutated,
                        discoveries=tuple(discoveries),
                        disposition_run_ids=tuple(disposition_runs),
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
                          AND updated_at <= clock_timestamp()
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
                                WHEN run.status IN ('completed', 'superseded')
                                THEN 'completed'
                                ELSE 'failed'
                            END,
                            updated_at = clock_timestamp()
                        FROM runs AS run
                        WHERE job.run_id = run.run_id
                          AND job.status = 'running'
                          AND job.boot_id IS DISTINCT FROM %s
                          AND run.status IN (
                              'completed', 'failed', 'rolled_back',
                              'superseded'
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

    def retired_unplanned_folder_runs(
        self,
        *,
        schema_versions: tuple[str, ...],
        work_types: tuple[ServerWorkType, ...] = tuple(ServerWorkType),
    ) -> tuple[str, ...]:
        """Find unplanned folder runs whose retired Agent call cannot resume."""

        if (
            not work_types
            or any(not isinstance(item, ServerWorkType) for item in work_types)
            or len(set(work_types)) != len(work_types)
        ):
            raise ServerError(ServerErrorCode.INVALID_CONFIG)
        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT r.run_id
                    FROM runs AS r
                    JOIN jobs AS job ON job.run_id = r.run_id
                    JOIN discoveries AS discovery
                      ON discovery.discovery_id = r.discovery_id
                    JOIN agent_definitions AS definition
                      ON definition.definition_hash =
                         r.agent_definition_hash
                    JOIN watch_folder_observations AS observed
                      ON observed.watch_id = discovery.watch_id
                     AND observed.folder_name = discovery.source_folder
                     AND observed.discovery_id = discovery.discovery_id
                    LEFT JOIN plan_heads AS head ON head.run_id = r.run_id
                    LEFT JOIN completed_layout_heads AS layout
                      ON layout.run_id = r.run_id
                    WHERE discovery.folder_generation_id IS NOT NULL
                      AND observed.status = 'active'
                      AND job.status IN (
                          'pending', 'running', 'completed', 'failed'
                      )
                      AND r.status IN (
                          'registered', 'running', 'failed'
                      )
                      AND head.run_id IS NULL
                      AND layout.run_id IS NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM folder_disposition_plans AS disposition
                          WHERE disposition.run_id = r.run_id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM run_operations AS operation
                          WHERE operation.run_id = r.run_id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM approval_claims AS claim
                          JOIN approvals AS approval USING (approval_id)
                          LEFT JOIN approval_settlements AS settlement
                            USING (approval_id)
                          WHERE approval.run_id = r.run_id
                            AND settlement.approval_id IS NULL
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM folder_disposition_claims AS claim
                          JOIN folder_disposition_approvals AS approval
                            USING (approval_id)
                          LEFT JOIN folder_disposition_transactions AS txn
                            USING (approval_id)
                          WHERE approval.run_id = r.run_id
                            AND (
                                txn.status IS NULL
                                OR txn.status <> 'blocked'
                            )
                      )
                      AND definition.payload->>'schema_version'
                          = ANY(%s)
                      AND r.work_type = ANY(%s)
                    ORDER BY r.run_id
                    """,
                    (
                        list(schema_versions),
                        [item.value for item in work_types],
                    ),
                ).fetchall()
            return tuple(str(row[0]) for row in rows)
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
                           d.snapshot_payload, d.discovered_at,
                           d.source_folder, d.folder_generation_id,
                           d.inventory_id,
                           observed.folder_device,
                           observed.folder_inode
                    FROM runs AS r
                    JOIN jobs AS j ON j.run_id = r.run_id
                    JOIN discoveries AS d
                      ON d.discovery_id = r.discovery_id
                    LEFT JOIN watch_folder_observations AS observed
                      ON observed.discovery_id = d.discovery_id
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
            source_folder=None if row[10] is None else str(row[10]),
            folder_generation_id=(
                None if row[11] is None else str(row[11])
            ),
            inventory_id=None if row[12] is None else str(row[12]),
            source_folder_device=(
                None if row[13] is None else int(row[13])
            ),
            source_folder_inode=(
                None if row[14] is None else int(row[14])
            ),
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

    def mark_run_failed(self, *, run_id: str) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE runs
                        SET status = 'failed'
                        WHERE run_id = %s
                          AND status NOT IN (
                              'completed', 'rolled_back', 'applying'
                          )
                        RETURNING run_id
                        """,
                        (run_id,),
                    ).fetchone()
                    if row is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def retry_job(
        self,
        *,
        job_id: str,
        boot_id: str,
        delay_seconds: int = 30,
    ) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'pending', boot_id = NULL,
                            updated_at = clock_timestamp()
                                + make_interval(secs => %s)
                        WHERE job_id = %s
                          AND status = 'running'
                          AND boot_id = %s
                        RETURNING job_id
                        """,
                        (delay_seconds, job_id, boot_id),
                    ).fetchone()
                    if row is None:
                        raise ServerError(ServerErrorCode.JOB_NOT_FOUND)
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def restart_folder_generation(
        self,
        *,
        run_id: str,
        audit_event: str | None = None,
    ) -> None:
        """Retire a transiently failed run without moving its source folder."""

        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (CONFIG_LOCK_ID,),
                    )
                    row = connection.execute(
                        """
                        SELECT d.discovery_id, d.watch_id, d.source_folder
                        FROM runs AS r
                        JOIN discoveries AS d
                          ON d.discovery_id = r.discovery_id
                        WHERE r.run_id = %s
                          AND d.folder_generation_id IS NOT NULL
                        """,
                        (run_id,),
                    ).fetchone()
                    if row is None or not _invalidate_unclaimed(
                        connection,
                        discovery_id=str(row[0]),
                    ):
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    updated = connection.execute(
                        """
                        UPDATE watch_folder_observations
                        SET discovery_id = NULL,
                            status = 'settling',
                            first_observed_at = clock_timestamp(),
                            stable_at = NULL
                        WHERE watch_id = %s
                          AND folder_name = %s
                          AND discovery_id = %s
                          AND status = 'active'
                        RETURNING folder_name
                        """,
                        (str(row[1]), str(row[2]), str(row[0])),
                    ).fetchone()
                    if updated is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    if audit_event is not None:
                        connection.execute(
                            """
                            INSERT INTO scheduler_audit
                                (event_type, subject_id)
                            VALUES (%s, %s)
                            ON CONFLICT (event_type, subject_id) DO NOTHING
                            """,
                            (audit_event, run_id),
                        )
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def retry_folder_generation(
        self,
        *,
        run_id: str,
        max_retries: int,
    ) -> int | None:
        """Retire one failed generation while preserving a bounded retry count."""

        if (
            type(max_retries) is not int
            or max_retries < 1
            or max_retries > 3
        ):
            raise ValueError("invalid folder retry limit")
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (CONFIG_LOCK_ID,),
                    )
                    row = connection.execute(
                        """
                        SELECT d.discovery_id, d.watch_id, d.source_folder,
                               o.retry_count
                        FROM runs AS r
                        JOIN discoveries AS d
                          ON d.discovery_id = r.discovery_id
                        JOIN watch_folder_observations AS o
                          ON o.discovery_id = d.discovery_id
                        WHERE r.run_id = %s
                          AND d.folder_generation_id IS NOT NULL
                          AND o.status = 'active'
                        FOR UPDATE OF o
                        """,
                        (run_id,),
                    ).fetchone()
                    if row is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    retry_count = int(row[3])
                    if retry_count >= max_retries:
                        return None
                    if not _invalidate_unclaimed(
                        connection,
                        discovery_id=str(row[0]),
                    ):
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    updated = connection.execute(
                        """
                        UPDATE watch_folder_observations
                        SET discovery_id = NULL,
                            status = 'settling',
                            first_observed_at = clock_timestamp(),
                            stable_at = NULL,
                            retry_count = retry_count + 1
                        WHERE watch_id = %s
                          AND folder_name = %s
                          AND discovery_id = %s
                          AND status = 'active'
                        RETURNING retry_count
                        """,
                        (str(row[1]), str(row[2]), str(row[0])),
                    ).fetchone()
                    if updated is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    connection.execute(
                        """
                        INSERT INTO scheduler_audit
                            (event_type, subject_id)
                        VALUES ('folder_generation_retry', %s)
                        ON CONFLICT (event_type, subject_id) DO NOTHING
                        """,
                        (run_id,),
                    )
                    return int(updated[0])
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def retry_needs_attention(
        self,
        *,
        run_id: str,
        expected_event_sequence: int,
        max_retries: int = 3,
    ) -> int | None:
        """Retire an exact stopped run and let the watcher create a fresh run."""

        if (
            type(expected_event_sequence) is not int
            or expected_event_sequence < 1
            or type(max_retries) is not int
            or not 1 <= max_retries <= 3
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (CONFIG_LOCK_ID,),
                    )
                    row = connection.execute(
                        """
                        SELECT d.discovery_id, d.watch_id, d.source_folder,
                               observation.retry_count
                        FROM runs AS run
                        JOIN jobs AS job ON job.run_id = run.run_id
                        JOIN discoveries AS d
                          ON d.discovery_id = run.discovery_id
                        JOIN run_states AS state
                          ON state.run_id = run.run_id
                        JOIN watch_folder_observations AS observation
                          ON observation.discovery_id = d.discovery_id
                        WHERE run.run_id = %s
                          AND run.status = 'running'
                          AND job.status = 'completed'
                          AND d.folder_generation_id IS NOT NULL
                          AND observation.status = 'active'
                          AND state.plan_hash IS NULL
                          AND state.event_sequence = %s
                          AND (
                              (
                                  state.runtime_status = 'stopped'
                                  AND state.projection_payload->>'stop_reason'
                                      = 'needs_attention'
                              )
                              OR EXISTS (
                                  SELECT 1
                                  FROM subtitle_acquisition_requests
                                      AS acquisition
                                  WHERE acquisition.run_id = run.run_id
                                    AND acquisition.status = 'blocked'
                              )
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM run_operations AS operation
                              WHERE operation.run_id = run.run_id
                          )
                        FOR UPDATE OF run, job, observation
                        """,
                        (run_id, expected_event_sequence),
                    ).fetchone()
                    if row is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    retry_count = int(row[3])
                    if retry_count >= max_retries:
                        return None
                    if not _invalidate_unclaimed(
                        connection,
                        discovery_id=str(row[0]),
                    ):
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    updated = connection.execute(
                        """
                        UPDATE watch_folder_observations
                        SET discovery_id = NULL,
                            status = 'settling',
                            first_observed_at = clock_timestamp(),
                            stable_at = NULL,
                            retry_count = retry_count + 1
                        WHERE watch_id = %s
                          AND folder_name = %s
                          AND discovery_id = %s
                          AND status = 'active'
                        RETURNING retry_count
                        """,
                        (str(row[1]), str(row[2]), str(row[0])),
                    ).fetchone()
                    if updated is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    connection.execute(
                        """
                        INSERT INTO scheduler_audit
                            (event_type, subject_id)
                        VALUES ('needs_attention_retry', %s)
                        ON CONFLICT (event_type, subject_id) DO NOTHING
                        """,
                        (run_id,),
                    )
                    return int(updated[0])
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def mark_needs_attention_failed(
        self,
        *,
        run_id: str,
        expected_event_sequence: int,
    ) -> None:
        """Choose terminal failure for an exact, unplanned attention state."""

        if (
            type(expected_event_sequence) is not int
            or expected_event_sequence < 1
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (CONFIG_LOCK_ID,),
                    )
                    row = connection.execute(
                        """
                        SELECT run.status
                        FROM runs AS run
                        JOIN jobs AS job ON job.run_id = run.run_id
                        JOIN discoveries AS discovery
                          ON discovery.discovery_id = run.discovery_id
                        JOIN run_states AS state
                          ON state.run_id = run.run_id
                        JOIN watch_folder_observations AS observation
                          ON observation.discovery_id = discovery.discovery_id
                        WHERE run.run_id = %s
                          AND run.status IN ('running', 'failed')
                          AND job.status = 'completed'
                          AND discovery.folder_generation_id IS NOT NULL
                          AND observation.status = 'active'
                          AND state.runtime_status = 'stopped'
                          AND state.plan_hash IS NULL
                          AND state.event_sequence = %s
                          AND state.projection_payload->>'stop_reason'
                              = 'needs_attention'
                          AND NOT EXISTS (
                              SELECT 1 FROM run_operations AS operation
                              WHERE operation.run_id = run.run_id
                          )
                        FOR UPDATE OF run, job, observation
                        """,
                        (run_id, expected_event_sequence),
                    ).fetchone()
                    if row is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    connection.execute(
                        """
                        UPDATE runs SET status = 'failed'
                        WHERE run_id = %s
                        """,
                        (run_id,),
                    )
                    connection.execute(
                        """
                        INSERT INTO scheduler_audit
                            (event_type, subject_id)
                        VALUES ('needs_attention_failed', %s)
                        ON CONFLICT (event_type, subject_id) DO NOTHING
                        """,
                        (run_id,),
                    )
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
