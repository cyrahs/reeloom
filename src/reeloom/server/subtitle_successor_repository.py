from __future__ import annotations

import json
from datetime import datetime, timedelta

from psycopg_pool import ConnectionPool

from reeloom.server.config import ServerWorkType
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.scheduler import Discovery, RunRegistration, _id
from reeloom.server.scheduler_repository import _inventory_json, _snapshot_json
from reeloom.server.subtitle_successor import (
    SubtitleAcquisitionSettlement,
    SubtitleSettlementResult,
    SubtitleSuccessorClaim,
    SubtitleSuccessorError,
    SubtitleSuccessorErrorCode,
    SubtitleSuccessorMember,
    SubtitleSuccessorRegistration,
    _validate_fresh_snapshot,
    _validate_lease,
    subtitle_lineage_key,
)
from reeloom.server.watcher import FolderSnapshot


class PostgresSubtitleSuccessorOutbox:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def settle(
        self,
        settlement: SubtitleAcquisitionSettlement,
    ) -> SubtitleSettlementResult:
        if not isinstance(settlement, SubtitleAcquisitionSettlement):
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.INVALID_REQUEST
            )
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    origin = connection.execute(
                        """
                        SELECT r.discovery_id, d.watch_id,
                               r.config_revision, r.work_type, r.status,
                               r.subtitle_acquisition_lineage_key,
                               d.source_folder, d.snapshot_id,
                               observation.folder_device,
                               observation.folder_inode
                        FROM runs AS r
                        JOIN discoveries AS d
                          ON d.discovery_id = r.discovery_id
                        JOIN watch_folder_observations AS observation
                          ON observation.discovery_id = d.discovery_id
                        WHERE r.run_id = %s
                        FOR UPDATE OF r
                        """,
                        (settlement.origin_run_id,),
                    ).fetchone()
                    if origin is None:
                        raise SubtitleSuccessorError(
                            SubtitleSuccessorErrorCode.ORIGIN_NOT_FOUND
                        )
                    if (
                        str(origin[0]) != settlement.origin_discovery_id
                        or str(origin[3]) != ServerWorkType.ANIME.value
                        or origin[6] is None
                        or str(origin[6]) != settlement.source_folder
                        or str(origin[7]) != settlement.original_snapshot_id
                        or int(origin[8])
                        != settlement.source_folder_device
                        or int(origin[9])
                        != settlement.source_folder_inode
                    ):
                        raise SubtitleSuccessorError(
                            SubtitleSuccessorErrorCode.INVALID_REQUEST
                        )
                    lineage_key = (
                        None if origin[5] is None else str(origin[5])
                    ) or subtitle_lineage_key(settlement.origin_discovery_id)
                    existing = connection.execute(
                        """
                        SELECT origin_run_id, acquisition_plan_hash,
                               approval_id, transaction_id, source_folder,
                               source_folder_device, source_folder_inode,
                               original_snapshot_id, destination_name,
                               destination_device, destination_inode,
                               member_manifest_json
                        FROM subtitle_acquisition_settlements
                        WHERE lineage_key = %s
                        """,
                        (lineage_key,),
                    ).fetchone()
                    if existing is not None:
                        if self._settlement_from_row(
                            settlement.origin_discovery_id,
                            existing,
                        ) == settlement:
                            return SubtitleSettlementResult(
                                lineage_key, False
                            )
                        raise SubtitleSuccessorError(
                            SubtitleSuccessorErrorCode.LINEAGE_ALREADY_ACQUIRED
                        )
                    if origin[5] is not None:
                        raise SubtitleSuccessorError(
                            SubtitleSuccessorErrorCode.LINEAGE_ALREADY_ACQUIRED
                        )
                    if str(origin[4]) not in {
                        "running",
                        "awaiting_approval",
                        "applying",
                    }:
                        raise SubtitleSuccessorError(
                            SubtitleSuccessorErrorCode.ORIGIN_STATE_CONFLICT
                        )
                    connection.execute(
                        """
                        INSERT INTO subtitle_acquisition_lineages
                            (lineage_key, root_discovery_id)
                        VALUES (%s, %s)
                        """,
                        (lineage_key, settlement.origin_discovery_id),
                    )
                    updated = connection.execute(
                        """
                        UPDATE runs
                        SET status = 'superseded',
                            subtitle_acquisition_lineage_key = %s
                        WHERE run_id = %s
                          AND subtitle_acquisition_lineage_key IS NULL
                          AND status IN (
                              'running', 'awaiting_approval', 'applying'
                          )
                        RETURNING run_id
                        """,
                        (lineage_key, settlement.origin_run_id),
                    ).fetchone()
                    if updated is None:
                        raise SubtitleSuccessorError(
                            SubtitleSuccessorErrorCode.ORIGIN_STATE_CONFLICT
                        )
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'completed', boot_id = NULL,
                            updated_at = clock_timestamp()
                        WHERE run_id = %s
                        """,
                        (settlement.origin_run_id,),
                    )
                    connection.execute(
                        """
                        INSERT INTO subtitle_acquisition_settlements
                            (lineage_key, origin_run_id,
                             acquisition_plan_hash, approval_id,
                             transaction_id, source_folder,
                             source_folder_device, source_folder_inode,
                             original_snapshot_id, destination_name,
                             destination_device, destination_inode,
                             member_manifest_json)
                        VALUES
                            (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                             %s, %s, %s, %s::jsonb)
                        """,
                        (
                            lineage_key,
                            settlement.origin_run_id,
                            settlement.plan_hash,
                            settlement.approval_id,
                            settlement.transaction_id,
                            settlement.source_folder,
                            settlement.source_folder_device,
                            settlement.source_folder_inode,
                            settlement.original_snapshot_id,
                            settlement.destination_name,
                            settlement.destination_device,
                            settlement.destination_inode,
                            settlement.member_manifest_json,
                        ),
                    )
                    request = connection.execute(
                        """
                        UPDATE subtitle_acquisition_requests
                        SET status = 'published',
                            transaction_id = %s,
                            failure_code = NULL,
                            updated_at = clock_timestamp()
                        WHERE run_id = %s AND plan_hash = %s
                          AND status = 'approved'
                          AND approval_id = %s
                        RETURNING run_id
                        """,
                        (
                            settlement.transaction_id,
                            settlement.origin_run_id,
                            settlement.plan_hash,
                            settlement.approval_id,
                        ),
                    ).fetchone()
                    if request is None:
                        raise SubtitleSuccessorError(
                            SubtitleSuccessorErrorCode.SUCCESSOR_CONFLICT
                        )
                    connection.execute(
                        """
                        INSERT INTO subtitle_successor_outbox
                            (lineage_key, watch_id, config_revision)
                        VALUES (%s, %s, %s)
                        """,
                        (lineage_key, str(origin[1]), int(origin[2])),
                    )
                    connection.execute(
                        """
                        INSERT INTO scheduler_audit
                            (event_type, subject_id)
                        VALUES ('subtitle_acquisition_settled', %s)
                        ON CONFLICT (event_type, subject_id) DO NOTHING
                        """,
                        (settlement.origin_run_id,),
                    )
                    return SubtitleSettlementResult(lineage_key, True)
        except (SubtitleSuccessorError, ServerError):
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_for: timedelta,
    ) -> SubtitleSuccessorClaim | None:
        _validate_lease(worker_id, now, lease_for)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        UPDATE subtitle_successor_outbox
                        SET state = CASE
                                WHEN attempt_count >= 100 THEN 'blocked'
                                ELSE 'retry_wait'
                            END,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            available_at = %s,
                            updated_at = clock_timestamp()
                        WHERE state = 'leased'
                          AND lease_expires_at <= %s
                        """,
                        (now, now),
                    )
                    row = connection.execute(
                        """
                        SELECT o.lineage_key, o.watch_id,
                               o.config_revision, o.attempt_count,
                               l.root_discovery_id,
                               s.origin_run_id,
                               s.acquisition_plan_hash,
                               s.approval_id, s.transaction_id,
                               s.source_folder,
                               s.source_folder_device,
                               s.source_folder_inode,
                               s.original_snapshot_id,
                               s.destination_name,
                               s.destination_device,
                               s.destination_inode,
                               s.member_manifest_json
                        FROM subtitle_successor_outbox AS o
                        JOIN subtitle_acquisition_settlements AS s
                          USING (lineage_key)
                        JOIN subtitle_acquisition_lineages AS l
                          USING (lineage_key)
                        WHERE o.state IN ('queued', 'retry_wait')
                          AND o.available_at <= %s
                          AND o.attempt_count < 100
                        ORDER BY o.available_at, o.created_at,
                                 o.lineage_key
                        FOR UPDATE OF o SKIP LOCKED
                        LIMIT 1
                        """,
                        (now,),
                    ).fetchone()
                    if row is None:
                        return None
                    expires = now + lease_for
                    attempt_count = int(row[3]) + 1
                    connection.execute(
                        """
                        UPDATE subtitle_successor_outbox
                        SET state = 'leased', attempt_count = %s,
                            lease_owner = %s, lease_expires_at = %s,
                            updated_at = clock_timestamp()
                        WHERE lineage_key = %s
                        """,
                        (attempt_count, worker_id, expires, str(row[0])),
                    )
                    settlement = self._settlement_from_claim_row(row)
                    return SubtitleSuccessorClaim(
                        lineage_key=str(row[0]),
                        settlement=settlement,
                        watch_id=str(row[1]),
                        config_revision=int(row[2]),
                        worker_id=worker_id,
                        attempt_count=attempt_count,
                        lease_expires_at=expires,
                    )
        except SubtitleSuccessorError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def retry(
        self,
        claim: SubtitleSuccessorClaim,
        *,
        now: datetime,
        delay: timedelta,
    ) -> None:
        if delay < timedelta(0) or delay > timedelta(hours=24):
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.INVALID_REQUEST
            )
        self._update_lease(
            claim,
            now=now,
            sql="""
                UPDATE subtitle_successor_outbox
                SET state = CASE
                        WHEN attempt_count >= 100 THEN 'blocked'
                        ELSE 'retry_wait'
                    END,
                    lease_owner = NULL,
                    lease_expires_at = NULL, available_at = %s,
                    updated_at = clock_timestamp()
                WHERE lineage_key = %s AND state = 'leased'
                  AND lease_owner = %s AND attempt_count = %s
                  AND lease_expires_at = %s AND lease_expires_at > %s
                RETURNING lineage_key
            """,
            parameters=(
                now + delay,
                claim.lineage_key,
                claim.worker_id,
                claim.attempt_count,
                claim.lease_expires_at,
                now,
            ),
        )

    def block(
        self,
        claim: SubtitleSuccessorClaim,
        *,
        now: datetime,
    ) -> None:
        self._update_lease(
            claim,
            now=now,
            sql="""
                UPDATE subtitle_successor_outbox
                SET state = 'blocked', lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = clock_timestamp()
                WHERE lineage_key = %s AND state = 'leased'
                  AND lease_owner = %s AND attempt_count = %s
                  AND lease_expires_at = %s AND lease_expires_at > %s
                RETURNING lineage_key
            """,
            parameters=(
                claim.lineage_key,
                claim.worker_id,
                claim.attempt_count,
                claim.lease_expires_at,
                now,
            ),
        )

    def stabilize(
        self,
        claim: SubtitleSuccessorClaim,
        *,
        snapshot: FolderSnapshot,
        now: datetime,
        delay: timedelta,
    ) -> bool:
        if (
            not isinstance(claim, SubtitleSuccessorClaim)
            or not timedelta(seconds=1) <= delay <= timedelta(days=7)
        ):
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.INVALID_REQUEST
            )
        _validate_fresh_snapshot(claim.settlement, snapshot)
        fingerprint = (
            snapshot.inventory_id,
            snapshot.candidates.snapshot_id,
        )
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT stabilizing_inventory_id,
                               stabilizing_snapshot_id
                        FROM subtitle_successor_outbox
                        WHERE lineage_key = %s AND state = 'leased'
                          AND lease_owner = %s AND attempt_count = %s
                          AND lease_expires_at = %s
                          AND lease_expires_at > %s
                        FOR UPDATE
                        """,
                        (
                            claim.lineage_key,
                            claim.worker_id,
                            claim.attempt_count,
                            claim.lease_expires_at,
                            now,
                        ),
                    ).fetchone()
                    if row is None:
                        raise SubtitleSuccessorError(
                            SubtitleSuccessorErrorCode.LEASE_EXPIRED
                            if now >= claim.lease_expires_at
                            else SubtitleSuccessorErrorCode.LEASE_CONFLICT
                        )
                    previous = (
                        None if row[0] is None else str(row[0]),
                        None if row[1] is None else str(row[1]),
                    )
                    if previous == fingerprint:
                        return True
                    updated = connection.execute(
                        """
                        UPDATE subtitle_successor_outbox
                        SET state = 'retry_wait', lease_owner = NULL,
                            lease_expires_at = NULL, available_at = %s,
                            stabilizing_inventory_id = %s,
                            stabilizing_snapshot_id = %s,
                            updated_at = clock_timestamp()
                        WHERE lineage_key = %s AND state = 'leased'
                        RETURNING lineage_key
                        """,
                        (
                            now + delay,
                            fingerprint[0],
                            fingerprint[1],
                            claim.lineage_key,
                        ),
                    ).fetchone()
                    if updated is None:
                        raise SubtitleSuccessorError(
                            SubtitleSuccessorErrorCode.LEASE_CONFLICT
                        )
                    return False
        except SubtitleSuccessorError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def complete(
        self,
        claim: SubtitleSuccessorClaim,
        *,
        snapshot: FolderSnapshot,
        now: datetime,
    ) -> SubtitleSuccessorRegistration:
        _validate_fresh_snapshot(claim.settlement, snapshot)
        discovery_id = _id(
            "discovery",
            claim.lineage_key,
            snapshot.inventory_id,
            snapshot.candidates.snapshot_id,
        )
        run_id = _id("run", discovery_id)
        job_id = _id("job", run_id)
        generation_id = _id(
            "generation", claim.lineage_key, snapshot.inventory_id
        )
        capability = _id("capability", run_id)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    leased = connection.execute(
                        """
                        SELECT state, successor_discovery_id,
                               successor_run_id, fresh_snapshot_id,
                               o.watch_id, o.config_revision,
                               l.root_discovery_id,
                               s.origin_run_id,
                               s.acquisition_plan_hash,
                               s.approval_id, s.transaction_id,
                               s.source_folder,
                               s.source_folder_device,
                               s.source_folder_inode,
                               s.original_snapshot_id,
                               s.destination_name,
                               s.destination_device,
                               s.destination_inode,
                               s.member_manifest_json,
                               d.discovered_at
                        FROM subtitle_successor_outbox AS o
                        JOIN subtitle_acquisition_settlements AS s
                          USING (lineage_key)
                        JOIN subtitle_acquisition_lineages AS l
                          USING (lineage_key)
                        LEFT JOIN discoveries AS d
                          ON d.discovery_id = o.successor_discovery_id
                        WHERE o.lineage_key = %s
                        FOR UPDATE OF o
                        """,
                        (claim.lineage_key,),
                    ).fetchone()
                    if leased is None:
                        raise SubtitleSuccessorError(
                            SubtitleSuccessorErrorCode.LEASE_CONFLICT
                        )
                    stored_settlement = SubtitleAcquisitionSettlement(
                        origin_run_id=str(leased[7]),
                        origin_discovery_id=str(leased[6]),
                        plan_hash=str(leased[8]),
                        approval_id=str(leased[9]),
                        transaction_id=str(leased[10]),
                        source_folder=str(leased[11]),
                        source_folder_device=int(leased[12]),
                        source_folder_inode=int(leased[13]),
                        original_snapshot_id=str(leased[14]),
                        destination_name=str(leased[15]),
                        destination_device=int(leased[16]),
                        destination_inode=int(leased[17]),
                        members=_members(leased[18]),
                    )
                    if (
                        stored_settlement != claim.settlement
                        or str(leased[4]) != claim.watch_id
                        or int(leased[5]) != claim.config_revision
                    ):
                        raise SubtitleSuccessorError(
                            SubtitleSuccessorErrorCode.LEASE_CONFLICT
                        )
                    if str(leased[0]) == "completed":
                        if (
                            str(leased[1]) != discovery_id
                            or str(leased[2]) != run_id
                            or str(leased[3])
                            != snapshot.candidates.snapshot_id
                        ):
                            raise SubtitleSuccessorError(
                                SubtitleSuccessorErrorCode.SUCCESSOR_CONFLICT
                            )
                        return self._registration(
                            claim,
                            snapshot,
                            leased[19],
                            discovery_id,
                            run_id,
                            job_id,
                            generation_id,
                            capability,
                        )
                    valid = connection.execute(
                        """
                        SELECT 1
                        FROM subtitle_successor_outbox
                        WHERE lineage_key = %s AND state = 'leased'
                          AND lease_owner = %s AND attempt_count = %s
                          AND lease_expires_at = %s
                          AND lease_expires_at > %s
                          AND stabilizing_inventory_id = %s
                          AND stabilizing_snapshot_id = %s
                        """,
                        (
                            claim.lineage_key,
                            claim.worker_id,
                            claim.attempt_count,
                            claim.lease_expires_at,
                            now,
                            snapshot.inventory_id,
                            snapshot.candidates.snapshot_id,
                        ),
                    ).fetchone()
                    if valid is None:
                        raise SubtitleSuccessorError(
                            SubtitleSuccessorErrorCode.FRESH_SCAN_REQUIRED
                        )
                    connection.execute(
                        """
                        INSERT INTO discoveries
                            (discovery_id, watch_id, config_revision,
                             snapshot_id, snapshot_payload, work_type,
                             discovered_at, source_folder,
                             folder_generation_id, inventory_id)
                        VALUES
                            (%s, %s, %s, %s, %s::jsonb, 'anime', %s,
                             %s, %s, %s)
                        """,
                        (
                            discovery_id,
                            claim.watch_id,
                            claim.config_revision,
                            snapshot.candidates.snapshot_id,
                            _snapshot_json(snapshot.candidates),
                            now,
                            snapshot.name,
                            generation_id,
                            snapshot.inventory_id,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO runs
                            (run_id, discovery_id, config_revision,
                             work_type, source_capability, status,
                             subtitle_acquisition_lineage_key)
                        VALUES (%s, %s, %s, 'anime', %s, 'registered', %s)
                        """,
                        (
                            run_id,
                            discovery_id,
                            claim.config_revision,
                            capability,
                            claim.lineage_key,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO jobs (job_id, run_id, status)
                        VALUES (%s, %s, 'pending')
                        """,
                        (job_id, run_id),
                    )
                    observation = connection.execute(
                        """
                        UPDATE watch_folder_observations
                        SET config_revision = %s,
                            folder_device = %s, folder_inode = %s,
                            inventory_id = %s,
                            inventory_payload = %s::jsonb,
                            snapshot_id = %s,
                            snapshot_payload = %s::jsonb,
                            first_observed_at = %s, stable_at = %s,
                            discovery_id = %s, status = 'active',
                            blocked_reason = NULL, retry_count = 0
                        WHERE watch_id = %s AND folder_name = %s
                          AND discovery_id = %s
                        RETURNING watch_id
                        """,
                        (
                            claim.config_revision,
                            snapshot.device,
                            snapshot.inode,
                            snapshot.inventory_id,
                            _inventory_json(snapshot),
                            snapshot.candidates.snapshot_id,
                            _snapshot_json(snapshot.candidates),
                            now,
                            now,
                            discovery_id,
                            claim.watch_id,
                            snapshot.name,
                            claim.settlement.origin_discovery_id,
                        ),
                    ).fetchone()
                    if observation is None:
                        raise SubtitleSuccessorError(
                            SubtitleSuccessorErrorCode.FRESH_SCAN_REQUIRED
                        )
                    connection.execute(
                        """
                        UPDATE subtitle_successor_outbox
                        SET state = 'completed', lease_owner = NULL,
                            lease_expires_at = NULL,
                            successor_discovery_id = %s,
                            successor_run_id = %s,
                            fresh_snapshot_id = %s,
                            updated_at = clock_timestamp()
                        WHERE lineage_key = %s
                        """,
                        (
                            discovery_id,
                            run_id,
                            snapshot.candidates.snapshot_id,
                            claim.lineage_key,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO scheduler_audit
                            (event_type, subject_id)
                        VALUES ('subtitle_successor_registered', %s)
                        ON CONFLICT (event_type, subject_id) DO NOTHING
                        """,
                        (run_id,),
                    )
                    return self._registration(
                        claim,
                        snapshot,
                        now,
                        discovery_id,
                        run_id,
                        job_id,
                        generation_id,
                        capability,
                    )
        except (SubtitleSuccessorError, ServerError):
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def lineage_allows_automatic_acquisition(self, run_id: str) -> bool:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT r.subtitle_acquisition_lineage_key,
                           EXISTS (
                               SELECT 1
                               FROM subtitle_acquisition_settlements AS s
                               WHERE s.lineage_key =
                                   r.subtitle_acquisition_lineage_key
                           )
                    FROM runs AS r
                    WHERE r.run_id = %s
                    """,
                    (run_id,),
                ).fetchone()
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
        if row is None:
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.ORIGIN_NOT_FOUND
            )
        return row[0] is None or not bool(row[1])

    def _update_lease(
        self,
        claim: SubtitleSuccessorClaim,
        *,
        now: datetime,
        sql: str,
        parameters: tuple[object, ...],
    ) -> None:
        if not isinstance(claim, SubtitleSuccessorClaim):
            raise SubtitleSuccessorError(
                SubtitleSuccessorErrorCode.INVALID_REQUEST
            )
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(sql, parameters).fetchone()
                    if row is None:
                        raise SubtitleSuccessorError(
                            SubtitleSuccessorErrorCode.LEASE_EXPIRED
                            if now >= claim.lease_expires_at
                            else SubtitleSuccessorErrorCode.LEASE_CONFLICT
                        )
        except SubtitleSuccessorError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    @staticmethod
    def _settlement_from_row(
        origin_discovery_id: str,
        row: object,
    ) -> SubtitleAcquisitionSettlement:
        values = tuple(row)  # type: ignore[arg-type]
        members = _members(values[11])
        return SubtitleAcquisitionSettlement(
            origin_run_id=str(values[0]),
            origin_discovery_id=origin_discovery_id,
            plan_hash=str(values[1]),
            approval_id=str(values[2]),
            transaction_id=str(values[3]),
            source_folder=str(values[4]),
            source_folder_device=int(values[5]),
            source_folder_inode=int(values[6]),
            original_snapshot_id=str(values[7]),
            destination_name=str(values[8]),
            destination_device=int(values[9]),
            destination_inode=int(values[10]),
            members=members,
        )

    @classmethod
    def _settlement_from_claim_row(
        cls,
        row: object,
    ) -> SubtitleAcquisitionSettlement:
        values = tuple(row)  # type: ignore[arg-type]
        return SubtitleAcquisitionSettlement(
            origin_run_id=str(values[5]),
            origin_discovery_id=str(values[4]),
            plan_hash=str(values[6]),
            approval_id=str(values[7]),
            transaction_id=str(values[8]),
            source_folder=str(values[9]),
            source_folder_device=int(values[10]),
            source_folder_inode=int(values[11]),
            original_snapshot_id=str(values[12]),
            destination_name=str(values[13]),
            destination_device=int(values[14]),
            destination_inode=int(values[15]),
            members=_members(values[16]),
        )

    @staticmethod
    def _registration(
        claim: SubtitleSuccessorClaim,
        snapshot: FolderSnapshot,
        now: datetime,
        discovery_id: str,
        run_id: str,
        job_id: str,
        generation_id: str,
        capability: str,
    ) -> SubtitleSuccessorRegistration:
        return SubtitleSuccessorRegistration(
            lineage_key=claim.lineage_key,
            predecessor_run_id=claim.settlement.origin_run_id,
            discovery=Discovery(
                discovery_id=discovery_id,
                watch_id=claim.watch_id,
                config_revision=claim.config_revision,
                snapshot_id=snapshot.candidates.snapshot_id,
                work_type=ServerWorkType.ANIME,
                discovered_at=now,
                snapshot=snapshot.candidates,
                source_folder=snapshot.name,
                folder_generation_id=generation_id,
                inventory_id=snapshot.inventory_id,
                source_folder_device=snapshot.device,
                source_folder_inode=snapshot.inode,
            ),
            registration=RunRegistration(
                run_id=run_id,
                job_id=job_id,
                discovery_id=discovery_id,
                config_revision=claim.config_revision,
                work_type=ServerWorkType.ANIME,
                source_capability=capability,
            ),
        )


def _members(value: object) -> tuple[SubtitleSuccessorMember, ...]:
    try:
        raw = json.loads(value) if isinstance(value, str) else value
        if not isinstance(raw, list):
            raise ValueError
        members = tuple(
            SubtitleSuccessorMember(
                destination_name=item["destination_name"],
                size_bytes=item["size_bytes"],
            )
            for item in raw
            if isinstance(item, dict)
            and set(item) == {"destination_name", "size_bytes"}
        )
        if len(members) != len(raw):
            raise ValueError
        return members
    except (KeyError, TypeError, ValueError):
        raise SubtitleSuccessorError(
            SubtitleSuccessorErrorCode.INVALID_REQUEST
        ) from None
