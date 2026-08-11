from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from reeloom.server.config import ApplyPolicy
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.run_lifecycle import RunEffectKind, RunEffectMode

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PLAN_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class RunEffectControl:
    run_id: str
    mode: RunEffectMode
    revision: int
    effect_kind: RunEffectKind | None
    plan_hash: str | None
    policy: ApplyPolicy | None
    operation_id: str | None


class PostgresRunControlRepository:
    """The only active writer for a run's v2 effect head.

    Planning, effect execution and browser commands use the same advisory lock
    and monotonically fenced control row.  Filesystem work never happens while
    this lock is held.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def handoff_effect(
        self,
        *,
        run_id: str,
        plan_hash: str,
        effect_kind: RunEffectKind,
        policy: ApplyPolicy,
        event_sequence: int,
    ) -> RunEffectControl:
        self._validate_binding(
            run_id=run_id,
            plan_hash=plan_hash,
            effect_kind=effect_kind,
            policy=policy,
            event_sequence=event_sequence,
        )
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    return self.handoff_effect_in_transaction(
                        connection,
                        run_id=run_id,
                        plan_hash=plan_hash,
                        effect_kind=effect_kind,
                        policy=policy,
                        event_sequence=event_sequence,
                    )
        except ServerError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    def handoff_effect_in_transaction(
        self,
        connection: object,
        *,
        run_id: str,
        plan_hash: str,
        effect_kind: RunEffectKind,
        policy: ApplyPolicy,
        event_sequence: int,
    ) -> RunEffectControl:
        """Perform handoff on a caller-owned transaction and connection."""

        self._validate_binding(
            run_id=run_id,
            plan_hash=plan_hash,
            effect_kind=effect_kind,
            policy=policy,
            event_sequence=event_sequence,
        )
        self._lock(connection, run_id)
        row = connection.execute(  # type: ignore[attr-defined]
            """
            SELECT control.mode, control.revision, control.effect_kind,
                   control.effect_plan_hash, control.effect_policy,
                   control.operation_id, run.status, state.event_sequence,
                   terminal.run_id IS NOT NULL
            FROM run_lifecycle_controls_v2 AS control
            JOIN runs AS run USING (run_id)
            JOIN run_states AS state USING (run_id)
            LEFT JOIN planning_terminal_results_v2 AS terminal
              ON terminal.run_id = control.run_id
            WHERE control.run_id = %s
            FOR UPDATE OF control, run
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise ServerError(ServerErrorCode.RUN_NOT_FOUND)
        current = self._control(run_id, row)
        if (
            current.mode is not RunEffectMode.FORWARD_V2
            or current.operation_id is not None
            or str(row[6])
            in {"completed", "failed", "rolled_back", "superseded"}
            or int(row[7]) != event_sequence
            or bool(row[8])
            or (
                current.plan_hash is not None
                and (
                    current.plan_hash != plan_hash
                    or current.effect_kind is not effect_kind
                    or current.policy is not policy
                )
            )
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        binding = connection.execute(  # type: ignore[attr-defined]
            """
            SELECT plan_kind, approval_scope
            FROM effect_plan_bindings_v2
            WHERE run_id = %s AND plan_hash = %s
            """,
            (run_id, plan_hash),
        ).fetchone()
        expected_scope = (
            "apply"
            if effect_kind is RunEffectKind.MEDIA_MOVE
            else "subtitle_acquire"
        )
        if binding is None or tuple(map(str, binding)) != (
            effect_kind.value,
            expected_scope,
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        if current.plan_hash is None:
            updated = connection.execute(  # type: ignore[attr-defined]
                """
                UPDATE run_lifecycle_controls_v2
                SET effect_kind = %s, effect_plan_hash = %s,
                    effect_policy = %s, handoff_event_sequence = %s,
                    revision = revision + 1,
                    updated_at = clock_timestamp()
                WHERE run_id = %s AND revision = %s
                RETURNING mode, revision, effect_kind,
                          effect_plan_hash, effect_policy, operation_id
                """,
                (
                    effect_kind.value,
                    plan_hash,
                    policy.value,
                    event_sequence,
                    run_id,
                    current.revision,
                ),
            ).fetchone()
            if updated is None:
                raise ServerError(ServerErrorCode.RUN_BUSY)
            current = self._control(run_id, updated)
        self._settle_planning_handoff(
            connection,
            current=current,
            event_sequence=event_sequence,
        )
        return current

    def mark_failed(
        self,
        *,
        run_id: str,
        expected_event_sequence: int,
        reason_code: str,
        source_disposition: str = "preserve",
    ) -> RunEffectControl:
        if (
            _RUN_ID.fullmatch(run_id) is None
            or type(expected_event_sequence) is not int
            or expected_event_sequence <= 0
            or not isinstance(reason_code, str)
            or not 1 <= len(reason_code.encode("utf-8")) <= 128
            or source_disposition not in {"preserve", "fail"}
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    self._lock(connection, run_id)
                    row = connection.execute(
                        """
                        SELECT control.mode, control.revision,
                               control.effect_kind,
                               control.effect_plan_hash,
                               control.effect_policy,
                               control.operation_id,
                               state.event_sequence,
                               state.projection_payload->>'stop_reason'
                        FROM run_lifecycle_controls_v2 AS control
                        JOIN runs AS run USING (run_id)
                        JOIN run_states AS state USING (run_id)
                        WHERE control.run_id = %s
                          AND run.status IN ('running', 'failed')
                        FOR UPDATE OF control, run
                        """,
                        (run_id,),
                    ).fetchone()
                    if (
                        row is None
                        or str(row[0]) != RunEffectMode.FORWARD_V2.value
                        or row[5] is not None
                        or int(row[6]) != expected_event_sequence
                        or str(row[7]) != "needs_attention"
                    ):
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    current = self._control(run_id, row[:6])
                    connection.execute(
                        """
                        INSERT INTO planning_terminal_results_v2
                            (run_id, plan_hash, outcome, reason_code,
                             source_disposition)
                        VALUES (%s, %s, 'user_failed', %s, %s)
                        ON CONFLICT (run_id) DO NOTHING
                        """,
                        (
                            run_id,
                            current.plan_hash,
                            reason_code,
                            source_disposition,
                        ),
                    )
                    self._record_handled(
                        connection,
                        run_id=run_id,
                        operation_id=None,
                        terminal_status="agent_failed",
                    )
                    connection.execute(
                        """
                        UPDATE runs SET status = 'failed'
                        WHERE run_id = %s
                        """,
                        (run_id,),
                    )
                    self._project_terminal_state(
                        connection,
                        run_id=run_id,
                        completed=False,
                        failure_code=reason_code,
                    )
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'failed', boot_id = NULL,
                            updated_at = clock_timestamp()
                        WHERE run_id = %s
                          AND status IN ('pending', 'running', 'completed')
                        """,
                        (run_id,),
                    )
                    self._notification_intent(
                        connection,
                        run_id=run_id,
                        revision=current.revision,
                        operation_id=None,
                        intent_kind="attention_required",
                        semantic_suffix=reason_code,
                    )
                    return current
        except ServerError:
            raise
        except Exception:
            raise ServerError(ServerErrorCode.DATABASE_UNAVAILABLE) from None

    @staticmethod
    def _lock(connection: object, run_id: str) -> None:
        connection.execute(  # type: ignore[attr-defined]
            """
            SELECT pg_advisory_xact_lock(
                hashtextextended('reeloom-run:' || %s, 0)
            )
            """,
            (run_id,),
        )

    def _settle_planning_handoff(
        self,
        connection: object,
        *,
        current: RunEffectControl,
        event_sequence: int,
    ) -> None:
        if current.policy is ApplyPolicy.PLAN_ONLY:
            connection.execute(  # type: ignore[attr-defined]
                """
                INSERT INTO planning_terminal_results_v2
                    (run_id, plan_hash, outcome, reason_code,
                     source_disposition)
                VALUES (%s, %s, 'plan_only', 'plan_only', 'preserve')
                ON CONFLICT (run_id) DO NOTHING
                """,
                (current.run_id, current.plan_hash),
            )
            self._record_handled(
                connection,
                run_id=current.run_id,
                operation_id=None,
                terminal_status="completed",
            )
            connection.execute(  # type: ignore[attr-defined]
                "UPDATE runs SET status = 'completed' WHERE run_id = %s",
                (current.run_id,),
            )
            self._project_terminal_state(
                connection,
                run_id=current.run_id,
                completed=True,
                failure_code=None,
            )
            intent_kind = "plan_generated"
        elif current.policy is ApplyPolicy.MANUAL:
            connection.execute(  # type: ignore[attr-defined]
                """
                UPDATE runs SET status = 'awaiting_approval'
                WHERE run_id = %s
                  AND status IN ('registered', 'running',
                                 'awaiting_approval')
                """,
                (current.run_id,),
            )
            intent_kind = "plan_ready"
        else:
            intent_kind = None
        connection.execute(  # type: ignore[attr-defined]
            """
            UPDATE jobs
            SET status = 'completed', boot_id = NULL,
                updated_at = clock_timestamp()
            WHERE run_id = %s AND status IN ('pending', 'running')
            """,
            (current.run_id,),
        )
        if intent_kind is not None:
            self._notification_intent(
                connection,
                run_id=current.run_id,
                revision=current.revision,
                operation_id=None,
                intent_kind=intent_kind,
                semantic_suffix=current.plan_hash or str(event_sequence),
            )

    @staticmethod
    def _record_handled(
        connection: object,
        *,
        run_id: str,
        operation_id: str | None,
        terminal_status: str,
    ) -> None:
        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO handled_folder_inventories_v2
                (watch_id, source_folder, inventory_id, run_id,
                 operation_id, terminal_status)
            SELECT discovery.watch_id, discovery.source_folder,
                   observation.inventory_id, run.run_id, %s, %s
            FROM runs AS run
            JOIN discoveries AS discovery USING (discovery_id)
            JOIN watch_folder_observations AS observation
              ON observation.discovery_id = discovery.discovery_id
            WHERE run.run_id = %s
              AND discovery.source_folder IS NOT NULL
              AND observation.inventory_id IS NOT NULL
            ON CONFLICT DO NOTHING
            """,
            (operation_id, terminal_status, run_id),
        )

    @staticmethod
    def _project_terminal_state(
        connection: object,
        *,
        run_id: str,
        completed: bool,
        failure_code: str | None,
    ) -> None:
        phase = "completed" if completed else "failed"
        status = "stopped" if completed else "failed"
        connection.execute(  # type: ignore[attr-defined]
            """
            UPDATE run_states
            SET phase = %s, runtime_status = %s,
                projection_payload = projection_payload
                    || jsonb_build_object(
                        'phase', %s::text, 'status', %s::text,
                        'stop_reason', CASE WHEN %s::boolean
                            THEN NULL ELSE 'fatal_error' END,
                        'failure_code', %s::text
                    ),
                updated_at = clock_timestamp()
            WHERE run_id = %s
            """,
            (
                phase,
                status,
                phase,
                status,
                completed,
                failure_code,
                run_id,
            ),
        )

    @staticmethod
    def _notification_intent(
        connection: object,
        *,
        run_id: str,
        revision: int,
        operation_id: str | None,
        intent_kind: str,
        semantic_suffix: str,
    ) -> None:
        digest = hashlib.sha256(
            f"{run_id}\0{revision}\0{intent_kind}\0{semantic_suffix}".encode(
                "utf-8"
            )
        ).hexdigest()
        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO notification_intents_v2
                (intent_id, run_id, control_revision, operation_id,
                 intent_kind, semantic_key)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (semantic_key) DO NOTHING
            """,
            (
                f"notification-intent-v2-{digest}",
                run_id,
                revision,
                operation_id,
                intent_kind,
                f"{intent_kind}:{run_id}:{semantic_suffix}",
            ),
        )

    @staticmethod
    def _control(run_id: str, row: object) -> RunEffectControl:
        if not isinstance(row, (tuple, list)) or len(row) < 6:
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        return RunEffectControl(
            run_id=run_id,
            mode=RunEffectMode(str(row[0])),
            revision=int(row[1]),
            effect_kind=(
                None if row[2] is None else RunEffectKind(str(row[2]))
            ),
            plan_hash=None if row[3] is None else str(row[3]),
            policy=None if row[4] is None else ApplyPolicy(str(row[4])),
            operation_id=None if row[5] is None else str(row[5]),
        )

    @staticmethod
    def _validate_binding(
        *,
        run_id: str,
        plan_hash: str,
        effect_kind: RunEffectKind,
        policy: ApplyPolicy,
        event_sequence: int,
    ) -> None:
        if (
            _RUN_ID.fullmatch(run_id) is None
            or _PLAN_HASH.fullmatch(plan_hash) is None
            or not isinstance(effect_kind, RunEffectKind)
            or not isinstance(policy, ApplyPolicy)
            or type(event_sequence) is not int
            or event_sequence <= 0
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
