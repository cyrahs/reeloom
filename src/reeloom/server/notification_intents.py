from __future__ import annotations

import logging
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.notification_projector import (
    PostgresNotificationProjector,
)
from reeloom.server.notifications import AttentionKind

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NotificationIntent:
    intent_id: str
    run_id: str
    revision: int
    operation_id: str | None
    kind: str
    plan_hash: str | None
    effect_kind: str | None
    policy: str | None
    operation_status: str | None
    applied_count: int


class PostgresNotificationIntentWorker:
    """Project revision-bound v2 notification intents exactly once.

    The intent is revalidated against the current control head while locked.
    Superseded or automatic plan-ready intents are cancelled instead of being
    delivered from a stale Agent event.
    """

    def __init__(
        self,
        *,
        pool: ConnectionPool,
        projector: PostgresNotificationProjector,
    ) -> None:
        self._pool = pool
        self._projector = projector

    def process_one(self) -> bool:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT intent.intent_id, intent.run_id,
                               intent.control_revision,
                               intent.operation_id, intent.intent_kind,
                               control.effect_plan_hash,
                               control.effect_kind, control.effect_policy,
                               operation.status,
                               COALESCE((
                                   SELECT count(*)
                                   FROM jsonb_array_elements_text(
                                       operation.outcomes
                                   ) AS outcome(value)
                                   WHERE outcome.value IN (
                                       'satisfied', 'already_satisfied'
                                   )
                               ), 0)
                        FROM notification_intents_v2 AS intent
                        JOIN run_lifecycle_controls_v2 AS control
                          ON control.run_id = intent.run_id
                        LEFT JOIN execution_operations_v2 AS operation
                          ON operation.operation_id = intent.operation_id
                        WHERE intent.state = 'queued'
                        ORDER BY intent.created_at, intent.intent_id
                        FOR UPDATE OF intent SKIP LOCKED
                        LIMIT 1
                        """
                    ).fetchone()
                    if row is None:
                        return False
                    intent = NotificationIntent(
                        intent_id=str(row[0]),
                        run_id=str(row[1]),
                        revision=int(row[2]),
                        operation_id=(
                            None if row[3] is None else str(row[3])
                        ),
                        kind=str(row[4]),
                        plan_hash=(
                            None if row[5] is None else str(row[5])
                        ),
                        effect_kind=(
                            None if row[6] is None else str(row[6])
                        ),
                        policy=None if row[7] is None else str(row[7]),
                        operation_status=(
                            None if row[8] is None else str(row[8])
                        ),
                        applied_count=int(row[9]),
                    )
                    try:
                        state = self._project(connection, intent)
                    except ServerError:
                        raise
                    except Exception as error:
                        _LOG.warning(
                            "notification_intent_dead intent_id=%s "
                            "error_type=%s",
                            intent.intent_id,
                            type(error).__name__,
                        )
                        state = "dead"
                    connection.execute(
                        """
                        UPDATE notification_intents_v2
                        SET state = %s, updated_at = clock_timestamp()
                        WHERE intent_id = %s AND state = 'queued'
                        """,
                        (state, intent.intent_id),
                    )
                    return True
        except Exception:
            _LOG.exception("notification_intent_projection_failed")
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def _project(self, connection: object, intent: NotificationIntent) -> str:
        current = connection.execute(  # type: ignore[attr-defined]
            """
            SELECT revision, operation_id
            FROM run_lifecycle_controls_v2
            WHERE run_id = %s
            """,
            (intent.run_id,),
        ).fetchone()
        if (
            current is None
            or int(current[0]) != intent.revision
            or (
                intent.operation_id is not None
                and str(current[1]) != intent.operation_id
            )
        ):
            return "cancelled"
        if intent.kind == "plan_ready":
            if intent.policy != "manual" or intent.plan_hash is None:
                return "cancelled"
            self._projector.plan_ready_from_projection(
                connection,
                run_id=intent.run_id,
                plan_hash=intent.plan_hash,
                scope_label=(
                    "字幕自动获取"
                    if intent.effect_kind == "subtitle_acquire"
                    else "媒体整理"
                ),
                effect_kind=intent.effect_kind or "",
            )
            return "projected"
        if intent.kind == "plan_generated":
            # plan-only is terminal but deliberately has no approval notice.
            return "cancelled"
        if intent.kind == "operation_completed":
            if intent.operation_id is None or intent.operation_status != "completed":
                return "cancelled"
            self._projector.operation_completed_from_projection(
                connection,
                run_id=intent.run_id,
                operation_id=intent.operation_id,
                applied_count=intent.applied_count,
                effect_kind=intent.effect_kind or "",
                plan_hash=intent.plan_hash,
            )
            return "projected"
        if intent.kind in {"attention_required", "housekeeping_warning"}:
            event_id = intent.operation_id or intent.intent_id
            kind = {
                "collision": AttentionKind.TARGET_EXISTS,
                "stale": AttentionKind.SOURCE_CHANGED,
            }.get(
                intent.operation_status,
                (
                    AttentionKind.FOLDER_DISPOSITION_FAILED
                    if intent.kind == "housekeeping_warning"
                    else AttentionKind.EXECUTION_INTERRUPTED
                ),
            )
            self._projector.attention_from_projection(
                connection,
                run_id=intent.run_id,
                operation_id=event_id,
                kind=kind,
            )
            return "projected"
        return "dead"
