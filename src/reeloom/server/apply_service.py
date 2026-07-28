from __future__ import annotations

import json
import logging
import secrets
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Iterator

from psycopg_pool import ConnectionPool

from reeloom.executor.apply import ApplyResult, FilesystemExecutor
from reeloom.executor.apply import ApplyStatus
from reeloom.executor.errors import (
    ApprovalError,
    ApprovalErrorCode,
    ExecutorError,
    ExecutorErrorCode,
)
from reeloom.executor.manifest import ExecutionManifest
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.approval_repository import PostgresApprovalStore
from reeloom.server.config import ApplyPolicy, ConfigRevision
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.completed_layout import (
    PostgresCompletedLayoutRepository,
    capture_completed_layout,
)

_LOGGER = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _Reservation:
    config_payload: object
    watch_id: str
    work_type: str
    plan_kind: str


class ApplyCoordinator:
    """Single-instance effect gate around the existing no-LLM executor."""

    def __init__(
        self,
        *,
        pool: ConnectionPool,
        approvals: PostgresApprovalStore,
        executor: FilesystemExecutor,
        completed_layouts: PostgresCompletedLayoutRepository,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._pool = pool
        self._approvals = approvals
        self._executor = executor
        self._completed_layouts = completed_layouts
        self._clock = clock
        self._global_gate = threading.Lock()

    def approve_and_apply(
        self,
        *,
        run_id: str,
        plan_hash: str,
        automatic: bool,
    ) -> ApplyResult:
        settled = self._completed_layouts.settlement_for_plan(
            run_id=run_id,
            plan_hash=plan_hash,
        )
        if settled is not None:
            return settled
        operation_id = f"apply-{secrets.token_hex(16)}"
        with self._operation(
            run_id=run_id,
            plan_hash=plan_hash,
            operation_id=operation_id,
            operation_kind="automatic_apply" if automatic else "manual_apply",
            automatic=automatic,
        ):
            with self._global_gate:
                try:
                    approval = self._approvals.issue_or_reuse(
                        ApprovalRecord.create(
                            run_id=run_id,
                            plan_hash=plan_hash,
                            scope=ApprovalScope.APPLY,
                            expires_at=(
                                self._clock() + timedelta(minutes=15)
                            ),
                            nonce=secrets.token_urlsafe(32),
                        )
                    )
                except ApprovalError as error:
                    if error.code is not ApprovalErrorCode.ALREADY_CLAIMED:
                        raise
                    settled = self._completed_layouts.settlement_for_plan(
                        run_id=run_id,
                        plan_hash=plan_hash,
                    )
                    if settled is not None:
                        return settled
                    raise ExecutorError(
                        ExecutorErrorCode.RECOVERY_REQUIRED,
                        context=dict(error.context),
                    ) from None
                result = self._executor.apply(
                    plan_hash=plan_hash,
                    approval_id=approval.approval_id,
                )
                self._settle(result)
                return result

    def recover(
        self,
        *,
        run_id: str,
        plan_hash: str,
        approval_id: str,
    ) -> ApplyResult:
        settled = self._completed_layouts.settlement(
            run_id=run_id,
            plan_hash=plan_hash,
            approval_id=approval_id,
        )
        if settled is not None:
            return settled
        operation_id = f"recover-{secrets.token_hex(16)}"
        with self._operation(
            run_id=run_id,
            plan_hash=plan_hash,
            operation_id=operation_id,
            operation_kind="recover",
            automatic=None,
        ):
            with self._global_gate:
                result = self._executor.recover(
                    plan_hash=plan_hash,
                    approval_id=approval_id,
                )
                self._settle(result)
                return result

    def resolve(
        self,
        *,
        run_id: str,
        plan_hash: str,
        approval_id: str | None = None,
    ) -> ApplyResult | None:
        if approval_id is None:
            settled = self._completed_layouts.settlement_for_plan(
                run_id=run_id,
                plan_hash=plan_hash,
            )
            if settled is not None:
                return settled
            approval_id = self._approvals.claimed_id(
                run_id=run_id,
                plan_hash=plan_hash,
            )
            if approval_id is None:
                return None
        else:
            settled = self._completed_layouts.settlement(
                run_id=run_id,
                plan_hash=plan_hash,
                approval_id=approval_id,
            )
            if settled is not None:
                return settled
        try:
            return self.recover(
                run_id=run_id,
                plan_hash=plan_hash,
                approval_id=approval_id,
            )
        except ApprovalError as error:
            if error.code is ApprovalErrorCode.NOT_FOUND:
                return None
            raise

    @contextmanager
    def _operation(
        self,
        *,
        run_id: str,
        plan_hash: str,
        operation_id: str,
        operation_kind: str,
        automatic: bool | None,
    ) -> Iterator[None]:
        reservation = self._reserve(
            run_id=run_id,
            plan_hash=plan_hash,
            operation_id=operation_id,
            operation_kind=operation_kind,
        )
        primary: BaseException | None = None
        try:
            self._validate_reservation(
                reservation,
                run_id=run_id,
                plan_hash=plan_hash,
                automatic=automatic,
            )
            yield
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                self._release_operation(
                    run_id=run_id,
                    operation_id=operation_id,
                )
            except Exception:
                if primary is None:
                    raise
                _LOGGER.exception(
                    "failed to release operation after primary failure"
                )

    def _reserve(
        self,
        *,
        run_id: str,
        plan_hash: str,
        operation_id: str,
        operation_kind: str,
    ) -> _Reservation:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT r.work_type, c.payload, d.watch_id,
                               lineage.plan_kind
                        FROM runs AS r
                        JOIN config_revisions AS c
                          ON c.revision = r.config_revision
                        JOIN discoveries AS d
                          ON d.discovery_id = r.discovery_id
                        JOIN plan_heads AS h ON h.run_id = r.run_id
                        JOIN plan_lineage AS lineage
                          ON lineage.run_id = h.run_id
                         AND lineage.version = h.version
                        WHERE r.run_id = %s AND h.plan_hash = %s
                          AND (
                              lineage.plan_kind = 'amendment'
                              OR d.folder_generation_id IS NULL
                              OR EXISTS (
                                  SELECT 1
                                  FROM watch_folder_observations AS folder
                                  WHERE folder.discovery_id =
                                        d.discovery_id
                                    AND folder.status = 'active'
                                    AND folder.inventory_id =
                                        d.inventory_id
                              )
                          )
                        FOR UPDATE OF r
                        """,
                        (run_id, plan_hash),
                    ).fetchone()
                    if row is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    inserted = connection.execute(
                        """
                        INSERT INTO run_operations
                            (run_id, operation_id, operation_kind)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (run_id) DO NOTHING
                        RETURNING operation_id
                        """,
                        (run_id, operation_id, operation_kind),
                    ).fetchone()
                    if inserted is None:
                        raise ServerError(ServerErrorCode.RUN_BUSY)
                    return _Reservation(
                        config_payload=row[1],
                        watch_id=str(row[2]),
                        work_type=str(row[0]),
                        plan_kind=str(row[3]),
                    )
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def _validate_reservation(
        self,
        reservation: _Reservation,
        *,
        run_id: str,
        plan_hash: str,
        automatic: bool | None,
    ) -> None:
        try:
            config = ConfigRevision.from_json(
                json.dumps(reservation.config_payload)
            )
            if config.apply_policy is ApplyPolicy.PLAN_ONLY:
                raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
            if (
                automatic is True
                and config.apply_policy is not ApplyPolicy.AUTOMATIC
            ):
                raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
            watch = next(
                (
                    item
                    for item in config.watches
                    if item.watch_id == reservation.watch_id
                    and item.work_type.value == reservation.work_type
                ),
                None,
            )
            route = next(
                (
                    item
                    for item in config.archive_routes
                    if item.work_type.value == reservation.work_type
                ),
                None,
            )
            if watch is None or route is None:
                raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
            source = AuthorizedRoot.create(watch.root)
            output = AuthorizedRoot.create(route.root)
            content = self._executor.plans.load(plan_hash)
            manifest = ExecutionManifest.from_canonical_bytes(
                content,
                plan_hash=plan_hash,
            )
            if (
                manifest.run_id != run_id
                or (
                    manifest.work_type is not None
                    and manifest.work_type.value
                    != (
                        "tv_series"
                        if reservation.work_type == "tv"
                        else reservation.work_type
                    )
                )
                or not self._same_root(manifest.output_root, output)
                or (
                    reservation.plan_kind == "initial"
                    and not self._same_root(
                        manifest.source_root, source
                    )
                )
                or (
                    reservation.plan_kind == "amendment"
                    and not self._same_root(
                        manifest.source_root, output
                    )
                )
                or reservation.plan_kind
                not in {"initial", "amendment"}
            ):
                raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
            if reservation.plan_kind == "amendment":
                self._validate_amendment_head(
                    run_id=run_id,
                    content=content,
                )
        except (ServerError, ExecutorError):
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.INTERACTION_CONFLICT
            ) from None

    def _validate_amendment_head(
        self,
        *,
        run_id: str,
        content: bytes,
    ) -> None:
        try:
            payload = json.loads(content)
            parent_hash = payload["parent_plan_hash"]
            transaction_id = payload["completed_transaction_id"]
            sources = payload["sources"]
            source_root = payload["roots"]["source"]
            if (
                not isinstance(parent_hash, str)
                or not isinstance(transaction_id, str)
                or not isinstance(sources, list)
                or not isinstance(source_root, dict)
            ):
                raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT h.plan_hash, l.transaction_id, l.layout_payload
                    FROM completed_layout_heads AS h
                    JOIN completed_layouts AS l
                      ON l.run_id = h.run_id
                     AND l.version = h.version
                     AND l.plan_hash = h.plan_hash
                    WHERE h.run_id = %s
                    """,
                    (run_id,),
                ).fetchone()
            layout = (
                None
                if row is None
                else (
                    row[2]
                    if isinstance(row[2], dict)
                    else json.loads(str(row[2]))
                )
            )
            if (
                row is None
                or str(row[0]) != parent_hash
                or str(row[1]) != transaction_id
                or not isinstance(layout, dict)
                or layout.get("run_id") != run_id
                or layout.get("files") != sources
                or layout.get("root") != source_root
            ):
                raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        except ServerError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise ServerError(
                ServerErrorCode.INTERACTION_CONFLICT
            ) from None
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    @staticmethod
    def _same_root(binding: object, root: AuthorizedRoot) -> bool:
        return (
            getattr(binding, "path", None)
            == PurePosixPath(root.path.as_posix())
            and getattr(binding, "device", None) == root.device
            and getattr(binding, "inode", None) == root.inode
        )

    def reconcile_active(self) -> int:
        """Remove effect reservations owned by the previous process."""

        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    rows = connection.execute(
                        """
                        DELETE FROM run_operations
                        WHERE operation_kind IN
                            ('manual_apply', 'automatic_apply', 'recover')
                        RETURNING operation_id
                        """
                    ).fetchall()
                    return len(rows)
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def _settle(self, result: ApplyResult) -> None:
        layout = None
        if result.status is ApplyStatus.COMPLETED:
            content = self._executor.plans.load(result.plan_hash)
            manifest = ExecutionManifest.from_canonical_bytes(
                content,
                plan_hash=result.plan_hash,
            )
            layout = capture_completed_layout(
                manifest,
                transaction_id=result.transaction_id,
            )
        self._completed_layouts.settle_and_append(
            result=result,
            layout=layout,
        )

    def _release_operation(
        self,
        *,
        run_id: str,
        operation_id: str,
    ) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        DELETE FROM run_operations
                        WHERE run_id = %s AND operation_id = %s
                        """,
                        (run_id, operation_id),
                    )
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None
