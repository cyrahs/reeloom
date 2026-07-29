from __future__ import annotations

import json
import os
import secrets
import stat
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from psycopg_pool import ConnectionPool

from reeloom.adapters.filesystem import FilesystemScanner
from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.executor.errors import (
    ApprovalError,
    ApprovalErrorCode,
    ExecutorError,
    ExecutorErrorCode,
    filesystem_error_code,
)
from reeloom.executor.manifest import ExecutionManifest
from reeloom.executor.folder_transaction import FolderTransactionRecord
from reeloom.executor.folder_disposition import (
    FolderDispositionExecutor,
    FolderDispositionResult,
)
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.folder_disposition import (
    FolderDispositionAction,
    FolderDispositionPlan,
)
from reeloom.kernel.naming import filesystem_name_key
from reeloom.kernel.rename_plan import RootBinding
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.config import ConfigRevision
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.scheduler_repository import _snapshot_from_json
from reeloom.server.watcher import (
    FolderEntry,
    FolderEntryKind,
    FolderSnapshot,
    NoFollowWatcher,
    WatchSnapshot,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _entry(value: object) -> FolderEntry:
    if not isinstance(value, dict):
        raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
    try:
        return FolderEntry(
            relative_path=PurePosixPath(value["relative_path"]),
            kind=FolderEntryKind(value["kind"]),
            size_bytes=value["size_bytes"],
            device=value["device"],
            inode=value["inode"],
            mtime_ns=value["mtime_ns"],
            ctime_ns=value["ctime_ns"],
        )
    except (KeyError, TypeError, ValueError):
        raise ServerError(ServerErrorCode.INTERACTION_CONFLICT) from None


def _inventory(
    value: object,
) -> tuple[str, int, int, str, tuple[FolderEntry, ...]]:
    try:
        raw = value if isinstance(value, dict) else json.loads(str(value))
        if raw["schema"] != "folder-inventory-v1":
            raise ValueError
        name = str(raw["name"])
        device = int(raw["device"])
        inode = int(raw["inode"])
        inventory_id = str(raw["inventory_id"])
        entries = tuple(_entry(item) for item in raw["entries"])
        candidate_snapshot_id = str(raw["candidate_snapshot_id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ServerError(ServerErrorCode.INTERACTION_CONFLICT) from None
    verified = FolderSnapshot.create(
        name=name,
        device=device,
        inode=inode,
        entries=entries,
        candidates=WatchSnapshot(candidate_snapshot_id, ()),
    )
    if verified.inventory_id != inventory_id:
        raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
    return name, device, inode, inventory_id, entries


class PostgresFolderDispositionRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def plan_for_media(
        self, *, run_id: str, media_plan_hash: str
    ) -> FolderDispositionPlan | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT canonical_record
                    FROM folder_disposition_plans
                    WHERE run_id = %s AND media_plan_hash = %s
                    ORDER BY created_at DESC, plan_hash DESC
                    LIMIT 1
                    """,
                    (run_id, media_plan_hash),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        return (
            None
            if row is None
            else FolderDispositionPlan.from_canonical_bytes(bytes(row[0]))
        )

    def current_plan(self, *, run_id: str) -> FolderDispositionPlan | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT canonical_record
                    FROM folder_disposition_plans
                    WHERE run_id = %s
                    ORDER BY created_at DESC, plan_hash DESC
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        return (
            None
            if row is None
            else FolderDispositionPlan.from_canonical_bytes(bytes(row[0]))
        )

    def failure_plan(
        self, *, run_id: str, reason_code: str
    ) -> FolderDispositionPlan | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT canonical_record
                    FROM folder_disposition_plans
                    WHERE run_id = %s
                      AND media_plan_hash IS NULL
                      AND reason_code = %s
                    ORDER BY created_at DESC, plan_hash DESC
                    LIMIT 1
                    """,
                    (run_id, reason_code),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        return (
            None
            if row is None
            else FolderDispositionPlan.from_canonical_bytes(bytes(row[0]))
        )

    def is_blocked(self, *, plan_hash: str) -> bool:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT 1
                    FROM folder_disposition_plans AS p
                    JOIN folder_disposition_approvals AS a
                      ON a.run_id = p.run_id
                     AND a.plan_hash = p.plan_hash
                    JOIN folder_disposition_transactions AS t
                      USING (approval_id)
                    WHERE p.plan_hash = %s AND t.status = 'blocked'
                    """,
                    (plan_hash,),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        return row is not None

    def append_plan(self, plan: FolderDispositionPlan) -> bool:
        if not plan.verify_hash():
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    inserted = connection.execute(
                        """
                        INSERT INTO folder_disposition_plans
                            (plan_hash, run_id, media_plan_hash,
                             folder_generation_id, action, target_relative,
                             source_root_device, source_root_inode,
                             target_name_key,
                             inventory_id, file_count, reason_code,
                             canonical_record)
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s
                        )
                        ON CONFLICT DO NOTHING
                        RETURNING plan_hash
                        """,
                        (
                            plan.plan_hash,
                            plan.run_id,
                            plan.media_plan_hash,
                            plan.folder_generation_id,
                            plan.action.value,
                            (
                                None
                                if plan.target_relative is None
                                else plan.target_relative.as_posix()
                            ),
                            plan.source_root.device,
                            plan.source_root.inode,
                            (
                                None
                                if plan.target_relative is None
                                else filesystem_name_key(
                                    plan.target_relative.name
                                )
                            ),
                            plan.inventory_id,
                            plan.file_count,
                            plan.reason_code,
                            plan.canonical_bytes(),
                        ),
                    ).fetchone()
                    return inserted is not None
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def reserved_target_keys(
        self,
        *,
        root: AuthorizedRoot,
        action: FolderDispositionAction,
    ) -> frozenset[str]:
        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT target_name_key
                    FROM folder_disposition_plans
                    WHERE source_root_device = %s
                      AND source_root_inode = %s
                      AND action = %s
                      AND target_name_key IS NOT NULL
                    """,
                    (root.device, root.inode, action.value),
                ).fetchall()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        return frozenset(str(row[0]) for row in rows)

    def issue(
        self,
        approval: ApprovalRecord,
    ) -> ApprovalRecord:
        if (
            not approval.verify_id()
            or approval.scope is not ApprovalScope.FOLDER_DISPOSITION
            or approval.is_expired(_now())
        ):
            raise ApprovalError(ApprovalErrorCode.INVALID_RECORD)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended(%s, 0))",
                        (approval.plan_hash,),
                    )
                    existing = connection.execute(
                        """
                        SELECT a.canonical_record
                        FROM folder_disposition_approvals AS a
                        LEFT JOIN folder_disposition_claims AS c
                          USING (approval_id)
                        LEFT JOIN folder_disposition_settlements AS s
                          USING (approval_id)
                        WHERE a.run_id = %s AND a.plan_hash = %s
                          AND (
                              a.expires_at > %s
                              OR (
                                  c.approval_id IS NOT NULL
                                  AND s.approval_id IS NULL
                              )
                          )
                        ORDER BY a.issued_at DESC
                        LIMIT 1
                        """,
                        (approval.run_id, approval.plan_hash, _now()),
                    ).fetchone()
                    if existing is not None:
                        record = ApprovalRecord.from_canonical_bytes(
                            bytes(existing[0])
                        )
                        if (
                            record.scope
                            is not ApprovalScope.FOLDER_DISPOSITION
                        ):
                            raise ApprovalError(
                                ApprovalErrorCode.BINDING_MISMATCH
                            )
                        return record
                    connection.execute(
                        """
                        INSERT INTO folder_disposition_approvals
                            (approval_id, run_id, plan_hash, expires_at,
                             canonical_record)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            approval.approval_id,
                            approval.run_id,
                            approval.plan_hash,
                            approval.expires_at,
                            approval.canonical_bytes(),
                        ),
                    )
                    return approval
        except ApprovalError:
            raise
        except Exception:
            raise ApprovalError(ApprovalErrorCode.STORE_FAILURE) from None

    def claim(
        self,
        *,
        approval_id: str,
        run_id: str,
        plan_hash: str,
    ) -> ApprovalRecord:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT canonical_record
                        FROM folder_disposition_approvals
                        WHERE approval_id = %s
                        """,
                        (approval_id,),
                    ).fetchone()
                    if row is None:
                        raise ApprovalError(ApprovalErrorCode.NOT_FOUND)
                    record = ApprovalRecord.from_canonical_bytes(bytes(row[0]))
                    if (
                        record.run_id != run_id
                        or record.plan_hash != plan_hash
                        or record.scope
                        is not ApprovalScope.FOLDER_DISPOSITION
                    ):
                        raise ApprovalError(
                            ApprovalErrorCode.BINDING_MISMATCH
                        )
                    if record.is_expired(_now()):
                        raise ApprovalError(ApprovalErrorCode.EXPIRED)
                    inserted = connection.execute(
                        """
                        INSERT INTO folder_disposition_claims (approval_id)
                        VALUES (%s)
                        ON CONFLICT (approval_id) DO NOTHING
                        RETURNING approval_id
                        """,
                        (approval_id,),
                    ).fetchone()
                    if inserted is None:
                        raise ApprovalError(
                            ApprovalErrorCode.ALREADY_CLAIMED
                        )
                    return record
        except ApprovalError:
            raise
        except Exception:
            raise ApprovalError(ApprovalErrorCode.STORE_FAILURE) from None

    def require_claim(
        self,
        *,
        approval_id: str,
        run_id: str,
        plan_hash: str,
    ) -> ApprovalRecord:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT a.canonical_record
                    FROM folder_disposition_claims AS c
                    JOIN folder_disposition_approvals AS a
                      USING (approval_id)
                    WHERE c.approval_id = %s
                    """,
                    (approval_id,),
                ).fetchone()
        except Exception:
            raise ApprovalError(ApprovalErrorCode.STORE_FAILURE) from None
        if row is None:
            raise ApprovalError(ApprovalErrorCode.NOT_FOUND)
        record = ApprovalRecord.from_canonical_bytes(bytes(row[0]))
        if (
            record.run_id != run_id
            or record.plan_hash != plan_hash
            or record.scope is not ApprovalScope.FOLDER_DISPOSITION
        ):
            raise ApprovalError(ApprovalErrorCode.BINDING_MISMATCH)
        return record

    def begin_transaction(
        self, transaction: FolderTransactionRecord
    ) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO folder_disposition_transactions
                            (transaction_id, approval_id, status,
                             source_device, source_inode)
                        VALUES (%s, %s, 'prepared', %s, %s)
                        ON CONFLICT (transaction_id) DO NOTHING
                        """,
                        (
                            transaction.transaction_id,
                            transaction.approval_id,
                            transaction.source_device,
                            transaction.source_inode,
                        ),
                    )
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def mark_transaction(
        self,
        transaction: FolderTransactionRecord,
        *,
        status: str,
        failure_code: ExecutorErrorCode | None = None,
    ) -> None:
        if status not in {
            "renamed",
            "completed",
            "blocked",
            "recovery_required",
        }:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE folder_disposition_transactions
                        SET status = %s,
                            failure_code = COALESCE(%s, failure_code),
                            updated_at = clock_timestamp()
                        WHERE transaction_id = %s
                          AND approval_id = %s
                        RETURNING transaction_id
                        """,
                        (
                            status,
                            (
                                None
                                if failure_code is None
                                else failure_code.value
                            ),
                            transaction.transaction_id,
                            transaction.approval_id,
                        ),
                    ).fetchone()
                    if row is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
        except ServerError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def settle(
        self,
        transaction: FolderTransactionRecord,
        *,
        run_id: str,
    ) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        UPDATE folder_disposition_transactions
                        SET status = 'completed',
                            updated_at = clock_timestamp()
                        WHERE transaction_id = %s
                          AND approval_id = %s
                        """,
                        (
                            transaction.transaction_id,
                            transaction.approval_id,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO folder_disposition_settlements
                            (approval_id, transaction_id, status)
                        VALUES (%s, %s, 'completed')
                        ON CONFLICT (approval_id) DO NOTHING
                        """,
                        (
                            transaction.approval_id,
                            transaction.transaction_id,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE watch_folder_observations AS o
                        SET status = 'settled'
                        FROM discoveries AS d
                        JOIN runs AS r ON r.discovery_id = d.discovery_id
                        WHERE r.run_id = %s
                          AND o.discovery_id = d.discovery_id
                        """,
                        (run_id,),
                    )
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def settlement(
        self,
        *,
        run_id: str,
        plan_hash: str,
        approval_id: str | None = None,
    ) -> FolderDispositionResult | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT a.approval_id, s.transaction_id, p.action,
                           p.target_relative
                    FROM folder_disposition_plans AS p
                    JOIN folder_disposition_approvals AS a
                      ON a.run_id = p.run_id
                     AND a.plan_hash = p.plan_hash
                    JOIN folder_disposition_settlements AS s
                      ON s.approval_id = a.approval_id
                    WHERE p.run_id = %s AND p.plan_hash = %s
                      AND (%s::text IS NULL OR a.approval_id = %s)
                    ORDER BY s.settled_at DESC, a.approval_id DESC
                    LIMIT 1
                    """,
                    (run_id, plan_hash, approval_id, approval_id),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        if row is None:
            return None
        return FolderDispositionResult(
            run_id=run_id,
            plan_hash=plan_hash,
            approval_id=str(row[0]),
            transaction_id=str(row[1]),
            action=FolderDispositionAction(str(row[2])),
            target_relative=None if row[3] is None else str(row[3]),
        )


class FolderDispositionPlanner:
    def __init__(
        self,
        *,
        pool: ConnectionPool,
        plans: FilesystemPlanStore,
        repository: PostgresFolderDispositionRepository,
    ) -> None:
        self._pool = pool
        self._plans = plans
        self._repository = repository

    def prepare_success(
        self, *, run_id: str, media_plan_hash: str
    ) -> FolderDispositionPlan | None:
        existing = self._repository.plan_for_media(
            run_id=run_id, media_plan_hash=media_plan_hash
        )
        if existing is not None:
            return existing
        scope = self._scope(run_id)
        if scope is None:
            return None
        (
            generation_id,
            source_folder,
            inventory_payload,
            snapshot_payload,
            config_payload,
            watch_id,
        ) = scope
        name, device, inode, _, entries = _inventory(inventory_payload)
        if name != source_folder:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        config = ConfigRevision.from_json(json.dumps(config_payload))
        watch = next(
            (item for item in config.watches if item.watch_id == watch_id),
            None,
        )
        if watch is None:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        root = AuthorizedRoot.create(watch.root)
        manifest = ExecutionManifest.from_canonical_bytes(
            self._plans.load(media_plan_hash),
            plan_hash=media_plan_hash,
        )
        if manifest.run_id != run_id:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        sources = {
            item.candidate_id: item.relative_path
            for item in manifest.sources
        }
        moved: set[PurePosixPath] = set()
        for move in manifest.moves:
            source = sources[move.source_id]
            if (
                len(source.parts) < 2
                or source.parts[0] != source_folder
            ):
                raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
            moved.add(PurePosixPath(*source.parts[1:]))
        remaining = tuple(
            item for item in entries if item.relative_path not in moved
        )
        snapshot = _snapshot_from_json(snapshot_payload)
        remaining_candidates = NoFollowWatcher._candidate_snapshot(
            [
                item
                for item in snapshot.files
                if PurePosixPath(*item.relative_path.parts[1:])
                not in moved
            ]
        )
        expected = FolderSnapshot.create(
            name=source_folder,
            device=device,
            inode=inode,
            entries=remaining,
            candidates=remaining_candidates,
        )
        file_count = sum(
            item.kind is not FolderEntryKind.DIRECTORY
            for item in remaining
        )
        action = (
            FolderDispositionAction.REMOVE_EMPTY
            if not remaining
            else FolderDispositionAction.ARCHIVE
        )
        target = (
            None
            if action is FolderDispositionAction.REMOVE_EMPTY
            else self._target(root, action, source_folder)
        )
        plan = FolderDispositionPlan.create(
            run_id=run_id,
            folder_generation_id=generation_id,
            created_at=_now(),
            source_root=RootBinding(
                PurePosixPath(root.path.as_posix()),
                root.device,
                root.inode,
            ),
            source_folder=source_folder,
            folder_device=device,
            folder_inode=inode,
            inventory_id=expected.disposition_inventory_id,
            action=action,
            target_relative=target,
            media_plan_hash=media_plan_hash,
            file_count=file_count,
            reason_code="media_completed",
        )
        if self._persist(plan):
            return plan
        existing = self._repository.plan_for_media(
            run_id=run_id,
            media_plan_hash=media_plan_hash,
        )
        return (
            existing
            if existing is not None
            else self.prepare_success(
                run_id=run_id,
                media_plan_hash=media_plan_hash,
            )
        )

    def prepare_failure(
        self, *, run_id: str, reason_code: str
    ) -> FolderDispositionPlan | None:
        existing = self._repository.failure_plan(
            run_id=run_id, reason_code=reason_code
        )
        if existing is not None and not self._repository.is_blocked(
            plan_hash=existing.plan_hash
        ):
            return existing
        scope = self._scope(run_id)
        if scope is None:
            return None
        (
            generation_id,
            source_folder,
            inventory_payload,
            _snapshot_payload,
            config_payload,
            watch_id,
        ) = scope
        name, device, inode, inventory_id, entries = _inventory(
            inventory_payload
        )
        if name != source_folder:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        config = ConfigRevision.from_json(json.dumps(config_payload))
        watch = next(
            (item for item in config.watches if item.watch_id == watch_id),
            None,
        )
        if watch is None:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        root = AuthorizedRoot.create(watch.root)
        plan = FolderDispositionPlan.create(
            run_id=run_id,
            folder_generation_id=generation_id,
            created_at=_now(),
            source_root=RootBinding(
                PurePosixPath(root.path.as_posix()),
                root.device,
                root.inode,
            ),
            source_folder=source_folder,
            folder_device=device,
            folder_inode=inode,
            inventory_id=inventory_id,
            action=FolderDispositionAction.FAIL,
            target_relative=self._target(
                root, FolderDispositionAction.FAIL, source_folder
            ),
            media_plan_hash=None,
            file_count=sum(
                item.kind is not FolderEntryKind.DIRECTORY
                for item in entries
            ),
            reason_code=reason_code,
        )
        if self._persist(plan):
            return plan
        existing = self._repository.failure_plan(
            run_id=run_id,
            reason_code=reason_code,
        )
        return (
            existing
            if existing is not None
            and not self._repository.is_blocked(
                plan_hash=existing.plan_hash
            )
            else self.prepare_failure(
                run_id=run_id,
                reason_code=reason_code,
            )
        )

    def prepare_late(self, *, run_id: str) -> FolderDispositionPlan | None:
        media_plan_hash = self._completed_media_plan(run_id)
        if media_plan_hash is None:
            return None
        scope = self._scope(run_id)
        if scope is None:
            return None
        (
            generation_id,
            source_folder,
            inventory_payload,
            snapshot_payload,
            config_payload,
            watch_id,
        ) = scope
        name, device, inode, inventory_id, entries = _inventory(
            inventory_payload
        )
        if name != source_folder:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        snapshot = _snapshot_from_json(snapshot_payload)
        expected = FolderSnapshot.create(
            name=source_folder,
            device=device,
            inode=inode,
            entries=entries,
            candidates=snapshot,
        )
        existing = self._repository.plan_for_media(
            run_id=run_id,
            media_plan_hash=media_plan_hash,
        )
        if (
            existing is not None
            and existing.inventory_id
            == expected.disposition_inventory_id
            and not self._repository.is_blocked(
                plan_hash=existing.plan_hash
            )
        ):
            return existing
        config = ConfigRevision.from_json(json.dumps(config_payload))
        watch = next(
            (item for item in config.watches if item.watch_id == watch_id),
            None,
        )
        if watch is None:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        root = AuthorizedRoot.create(watch.root)
        action = (
            FolderDispositionAction.REMOVE_EMPTY
            if not entries
            else FolderDispositionAction.ARCHIVE
        )
        plan = FolderDispositionPlan.create(
            run_id=run_id,
            folder_generation_id=generation_id,
            created_at=_now(),
            source_root=RootBinding(
                PurePosixPath(root.path.as_posix()),
                root.device,
                root.inode,
            ),
            source_folder=source_folder,
            folder_device=device,
            folder_inode=inode,
            inventory_id=expected.disposition_inventory_id,
            action=action,
            target_relative=(
                None
                if action is FolderDispositionAction.REMOVE_EMPTY
                else self._target(root, action, source_folder)
            ),
            media_plan_hash=media_plan_hash,
            file_count=sum(
                item.kind is not FolderEntryKind.DIRECTORY
                for item in entries
            ),
            reason_code="late_content",
        )
        if self._persist(plan):
            return plan
        existing = self._repository.plan_for_media(
            run_id=run_id,
            media_plan_hash=media_plan_hash,
        )
        return (
            existing
            if existing is not None
            else self.prepare_late(run_id=run_id)
        )

    def issue(self, plan: FolderDispositionPlan) -> ApprovalRecord:
        return self._repository.issue(
            ApprovalRecord.create(
                run_id=plan.run_id,
                plan_hash=plan.plan_hash,
                scope=ApprovalScope.FOLDER_DISPOSITION,
                expires_at=_now() + timedelta(minutes=15),
                nonce=secrets.token_urlsafe(32),
            )
        )

    def _persist(self, plan: FolderDispositionPlan) -> bool:
        try:
            self._plans.save_folder_disposition(plan)
        except ExecutorError as error:
            if error.code is not ExecutorErrorCode.PLAN_ALREADY_EXISTS:
                raise
        return self._repository.append_plan(plan)

    def _scope(
        self, run_id: str
    ) -> tuple[object, ...] | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT d.folder_generation_id, d.source_folder,
                           o.inventory_payload, o.snapshot_payload,
                           c.payload, d.watch_id
                    FROM runs AS r
                    JOIN discoveries AS d
                      ON d.discovery_id = r.discovery_id
                    JOIN watch_folder_observations AS o
                      ON o.discovery_id = d.discovery_id
                    JOIN config_revisions AS c
                      ON c.revision = r.config_revision
                    WHERE r.run_id = %s
                      AND d.folder_generation_id IS NOT NULL
                      AND o.status = 'active'
                    """,
                    (run_id,),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        return None if row is None else tuple(row)

    def _completed_media_plan(self, run_id: str) -> str | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT h.plan_hash
                    FROM plan_heads AS h
                    JOIN approvals AS a
                      ON a.run_id = h.run_id
                     AND a.plan_hash = h.plan_hash
                    JOIN approval_settlements AS s
                      USING (approval_id)
                    WHERE h.run_id = %s AND s.status = 'completed'
                    """,
                    (run_id,),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        return None if row is None else str(row[0])

    def _target(
        self,
        root: AuthorizedRoot,
        action: FolderDispositionAction,
        source_folder: str,
    ) -> PurePosixPath:
        bucket = action.value
        root_fd = FilesystemScanner._open_root(root)
        bucket_fd: int | None = None
        try:
            try:
                os.mkdir(bucket, mode=0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
            except OSError as error:
                raise ExecutorError(filesystem_error_code(error)) from None
            try:
                metadata = os.stat(
                    bucket,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise ExecutorError(filesystem_error_code(error)) from None
            if not stat.S_ISDIR(metadata.st_mode):
                raise ExecutorError(ExecutorErrorCode.DESTINATION_COLLISION)
            bucket_fd = FilesystemScanner._open_directory(
                bucket, parent_fd=root_fd
            )
            try:
                names = {
                    filesystem_name_key(name)
                    for name in os.listdir(bucket_fd)
                }
            except OSError as error:
                raise ExecutorError(filesystem_error_code(error)) from None
            names.update(
                self._repository.reserved_target_keys(
                    root=root,
                    action=action,
                )
            )
            candidate = source_folder
            suffix = 0
            while filesystem_name_key(candidate) in names:
                suffix += 1
                candidate = f"{source_folder}.{suffix}"
            return PurePosixPath(bucket) / candidate
        finally:
            if bucket_fd is not None:
                os.close(bucket_fd)
            os.close(root_fd)


class FolderDispositionCoordinator:
    def __init__(
        self,
        *,
        pool: ConnectionPool,
        plans: FilesystemPlanStore,
        repository: PostgresFolderDispositionRepository,
        planner: FolderDispositionPlanner,
        executor: FolderDispositionExecutor,
    ) -> None:
        self._pool = pool
        self._plans = plans
        self._repository = repository
        self._planner = planner
        self._executor = executor

    def prepare_success(
        self, *, run_id: str, media_plan_hash: str
    ) -> FolderDispositionPlan | None:
        return self._planner.prepare_success(
            run_id=run_id, media_plan_hash=media_plan_hash
        )

    def prepare_failure(
        self, *, run_id: str, reason_code: str
    ) -> FolderDispositionPlan | None:
        return self._planner.prepare_failure(
            run_id=run_id, reason_code=reason_code
        )

    def prepare_late(self, *, run_id: str) -> FolderDispositionPlan | None:
        return self._planner.prepare_late(run_id=run_id)

    def prepare_current(self, *, run_id: str) -> FolderDispositionPlan | None:
        current = self._repository.current_plan(run_id=run_id)
        if current is None:
            return None
        if current.media_plan_hash is not None:
            return self._planner.prepare_late(run_id=run_id)
        return self._planner.prepare_failure(
            run_id=run_id,
            reason_code=current.reason_code,
        )

    def approve_and_execute(
        self,
        *,
        run_id: str,
        plan_hash: str,
        automatic: bool,
    ) -> FolderDispositionResult:
        settled = self._repository.settlement(
            run_id=run_id, plan_hash=plan_hash
        )
        if settled is not None:
            return settled
        plan = FolderDispositionPlan.from_canonical_bytes(
            self._plans.load_folder_disposition(plan_hash)
        )
        if plan.run_id != run_id:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        current = self._repository.current_plan(run_id=run_id)
        if current is None or current.plan_hash != plan.plan_hash:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        self._validate_policy(plan, automatic=automatic)
        approval = self._planner.issue(plan)
        try:
            return self._executor.apply(
                plan_hash=plan.plan_hash,
                approval_id=approval.approval_id,
            )
        except ApprovalError as error:
            if error.code is not ApprovalErrorCode.ALREADY_CLAIMED:
                raise
            settled = self._repository.settlement(
                run_id=run_id, plan_hash=plan_hash
            )
            if settled is not None:
                return settled
            raise ExecutorError(
                ExecutorErrorCode.RECOVERY_REQUIRED
            ) from None

    def recover(
        self,
        *,
        run_id: str,
        plan_hash: str,
        approval_id: str,
    ) -> FolderDispositionResult:
        plan = FolderDispositionPlan.from_canonical_bytes(
            self._plans.load_folder_disposition(plan_hash)
        )
        if plan.run_id != run_id:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        current = self._repository.current_plan(run_id=run_id)
        if current is None or current.plan_hash != plan_hash:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        if self._repository.is_blocked(plan_hash=plan_hash):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        settled = self._repository.settlement(
            run_id=run_id,
            plan_hash=plan_hash,
            approval_id=approval_id,
        )
        if settled is not None:
            return settled
        return self._executor.recover(
            plan_hash=plan_hash,
            approval_id=approval_id,
        )

    def resolve(
        self,
        *,
        run_id: str,
        plan_hash: str,
        approval_id: str | None = None,
    ) -> FolderDispositionResult | None:
        return self._repository.settlement(
            run_id=run_id,
            plan_hash=plan_hash,
            approval_id=approval_id,
        )

    def _validate_policy(
        self,
        plan: FolderDispositionPlan,
        *,
        automatic: bool,
    ) -> None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT c.payload,
                           EXISTS (
                               SELECT 1
                               FROM approvals AS a
                               JOIN approval_settlements AS s
                                 USING (approval_id)
                               WHERE a.run_id = p.run_id
                                 AND a.plan_hash = p.media_plan_hash
                                 AND s.status = 'completed'
                           )
                    FROM folder_disposition_plans AS p
                    JOIN runs AS r ON r.run_id = p.run_id
                    JOIN discoveries AS d
                      ON d.discovery_id = r.discovery_id
                    JOIN config_revisions AS c
                      ON c.revision = r.config_revision
                    WHERE p.run_id = %s AND p.plan_hash = %s
                      AND EXISTS (
                          SELECT 1
                          FROM watch_folder_observations AS o
                          WHERE o.discovery_id = d.discovery_id
                            AND o.status = 'active'
                      )
                    """,
                    (plan.run_id, plan.plan_hash),
                ).fetchone()
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None
        if row is None:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        config = ConfigRevision.from_json(json.dumps(row[0]))
        if automatic and config.apply_policy.value != "automatic":
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        if plan.media_plan_hash is not None and not bool(row[1]):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
