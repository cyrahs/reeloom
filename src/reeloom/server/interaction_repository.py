from __future__ import annotations

import json
import uuid

from psycopg_pool import ConnectionPool

from reeloom.kernel.plan_review import PLAN_REVIEW_SCHEMA, PlanReview
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.event_codec import encode_event
from reeloom.runtime.errors import RuntimeDomainError
from reeloom.runtime.events import InteractionCompleted
from reeloom.runtime.reducer import reduce_interaction_head
from reeloom.runtime.state_codec import (
    STATE_PROJECTION_SCHEMA,
    is_supported_projection_schema,
    patch_state,
)
from reeloom.runtime.state import Phase
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.interactions import (
    InteractionExecution,
    InteractionKind,
    InteractionReservation,
    InteractionResult,
    _request_hash,
)


def _result(value: object) -> InteractionResult:
    raw = value if isinstance(value, dict) else json.loads(str(value))
    return InteractionResult(
        interaction_id=raw["interaction_id"],
        kind=InteractionKind(raw["kind"]),
        assistant_reply=raw["assistant_reply"],
        plan_hash=raw["plan_hash"],
        model_tokens=raw["model_tokens"],
    )


class PostgresInteractionRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def reserve(
        self,
        *,
        run_id: str,
        kind: InteractionKind,
        idempotency_key: str,
        expected_plan_hash: str,
        message: str,
    ) -> InteractionReservation:
        if (
            not isinstance(kind, InteractionKind)
            or not isinstance(idempotency_key, str)
            or not idempotency_key
            or len(idempotency_key.encode("utf-8")) > 256
            or not isinstance(expected_plan_hash, str)
            or not expected_plan_hash
            or not isinstance(message, str)
            or not message
            or len(message.encode("utf-8")) > 16 * 1024
        ):
            raise ServerError(ServerErrorCode.INTERACTION_CONFLICT)
        digest = _request_hash(
            kind=kind,
            expected_plan_hash=expected_plan_hash,
            message=message,
        )
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    existing = connection.execute(
                        """
                        SELECT interaction_id, kind, request_hash,
                               expected_plan_hash, session_revision,
                               status, result
                        FROM interactions
                        WHERE run_id = %s AND idempotency_key = %s
                        FOR UPDATE
                        """,
                        (run_id, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        if str(existing[2]) != digest:
                            raise ServerError(
                                ServerErrorCode.INTERACTION_CONFLICT
                            )
                        status = str(existing[5])
                        if status == "active":
                            raise ServerError(ServerErrorCode.RUN_BUSY)
                        if status != "completed":
                            raise ServerError(
                                ServerErrorCode.INTERACTION_CONFLICT
                            )
                        terminal = _result(existing[6])
                        return InteractionReservation(
                            interaction_id=str(existing[0]),
                            run_id=run_id,
                            kind=InteractionKind(str(existing[1])),
                            request_hash=digest,
                            session_revision=int(existing[4]),
                            plan_hash=str(existing[3]),
                            terminal_result=terminal,
                        )
                    head = connection.execute(
                        """
                        SELECT plan_hash FROM plan_heads
                        WHERE run_id = %s
                        """,
                        (run_id,),
                    ).fetchone()
                    session = connection.execute(
                        """
                        SELECT revision FROM agent_sessions
                        WHERE run_id = %s
                        """,
                        (run_id,),
                    ).fetchone()
                    if (
                        head is None
                        or session is None
                        or str(head[0]) != expected_plan_hash
                    ):
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    state = connection.execute(
                        """
                        SELECT model_tokens, phase, model_turns,
                               tool_calls, failures, max_model_turns,
                               max_tool_calls, max_failures,
                               max_total_tokens,
                               EXTRACT(
                                   EPOCH FROM
                                   deadline_at - clock_timestamp()
                               )
                        FROM run_states
                        WHERE run_id = %s
                        """,
                        (run_id,),
                    ).fetchone()
                    if state is None:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    remaining = (
                        int(state[5]) - int(state[2]),
                        int(state[6]) - int(state[3]),
                        int(state[7]) - int(state[4]),
                        int(state[8]) - int(state[0]),
                    )
                    elapsed = float(state[9])
                    if min(*remaining) < 1 or elapsed <= 0:
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    budget = RunBudget(
                        max_model_turns=remaining[0],
                        max_tool_calls=remaining[1],
                        max_failures=remaining[2],
                        max_total_tokens=remaining[3],
                        max_elapsed_seconds=min(elapsed, 3_600.0),
                    )
                    if kind is InteractionKind.REVISION:
                        forbidden = connection.execute(
                            """
                            SELECT
                                EXISTS (
                                    SELECT 1 FROM approvals
                                    WHERE run_id = %s AND plan_hash = %s
                                ),
                                EXISTS (
                                    SELECT 1 FROM completed_layout_heads
                                    WHERE run_id = %s
                                )
                            """,
                            (run_id, expected_plan_hash, run_id),
                        ).fetchone()
                        if (
                            str(state[1]) != "awaiting_approval"
                            or bool(forbidden[0])
                            or bool(forbidden[1])
                        ):
                            raise ServerError(
                                ServerErrorCode.INTERACTION_CONFLICT
                            )
                    elif kind is InteractionKind.REAPPLY:
                        completed = connection.execute(
                            """
                            SELECT plan_hash
                            FROM completed_layout_heads
                            WHERE run_id = %s
                            """,
                            (run_id,),
                        ).fetchone()
                        if completed is None:
                            raise ServerError(
                                ServerErrorCode.INTERACTION_CONFLICT
                            )
                        if str(completed[0]) != expected_plan_hash:
                            proposal = connection.execute(
                                """
                                SELECT l.plan_kind,
                                       EXISTS (
                                           SELECT 1 FROM approvals
                                           WHERE run_id = %s
                                             AND plan_hash = %s
                                       )
                                FROM plan_heads AS h
                                JOIN plan_lineage AS l
                                  ON l.run_id = h.run_id
                                 AND l.version = h.version
                                WHERE h.run_id = %s
                                  AND h.plan_hash = %s
                                """,
                                (
                                    run_id,
                                    expected_plan_hash,
                                    run_id,
                                    expected_plan_hash,
                                ),
                            ).fetchone()
                            if (
                                proposal is None
                                or str(proposal[0]) != "amendment"
                                or bool(proposal[1])
                            ):
                                raise ServerError(
                                    ServerErrorCode.INTERACTION_CONFLICT
                                )
                    interaction_id = (
                        f"interaction-{uuid.uuid4().hex}"
                    )
                    inserted = connection.execute(
                        """
                        INSERT INTO run_operations
                            (run_id, operation_id, operation_kind)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (run_id) DO NOTHING
                        RETURNING operation_id
                        """,
                        (run_id, interaction_id, kind.value),
                    ).fetchone()
                    if inserted is None:
                        raise ServerError(ServerErrorCode.RUN_BUSY)
                    connection.execute(
                        """
                        INSERT INTO interactions
                            (interaction_id, run_id, kind, idempotency_key,
                             request_hash, expected_plan_hash,
                             session_revision, status, request_message)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s)
                        """,
                        (
                            interaction_id,
                            run_id,
                            kind.value,
                            idempotency_key,
                            digest,
                            expected_plan_hash,
                            int(session[0]),
                            message,
                        ),
                    )
                    return InteractionReservation(
                        interaction_id=interaction_id,
                        run_id=run_id,
                        kind=kind,
                        request_hash=digest,
                        session_revision=int(session[0]),
                        plan_hash=expected_plan_hash,
                        budget=budget,
                    )
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def finalize(
        self,
        *,
        reservation: InteractionReservation,
        execution: InteractionExecution,
    ) -> InteractionResult:
        self._validate(reservation, execution)
        result = InteractionResult(
            interaction_id=reservation.interaction_id,
            kind=reservation.kind,
            assistant_reply=execution.assistant_reply,
            plan_hash=execution.plan_hash,
            model_tokens=execution.model_tokens,
        )
        encoded = json.dumps(
            {
                "assistant_reply": result.assistant_reply,
                "interaction_id": result.interaction_id,
                "kind": result.kind.value,
                "model_tokens": result.model_tokens,
                "plan_hash": result.plan_hash,
                "archive_report": execution.archive_report,
                "execution_schema_version": (
                    execution.execution_schema_version
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        batch_json = json.dumps(
            list(execution.session_batch),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        items_json = json.dumps(
            list(execution.session_items),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        SELECT status FROM interactions
                        WHERE interaction_id = %s
                        FOR UPDATE
                        """,
                        (reservation.interaction_id,),
                    ).fetchone()
                    if row is None or str(row[0]) != "active":
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    session = connection.execute(
                        """
                        SELECT session_id, revision
                        FROM agent_sessions
                        WHERE run_id = %s
                        FOR UPDATE
                        """,
                        (reservation.run_id,),
                    ).fetchone()
                    if (
                        session is None
                        or int(session[1])
                        != reservation.session_revision
                        or execution.session_revision
                        != reservation.session_revision + 1
                    ):
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    runtime = connection.execute(
                        """
                        SELECT event_sequence, model_tokens, phase,
                               runtime_status, plan_hash, model_turns,
                               tool_calls, failures, max_model_turns,
                               max_tool_calls, max_failures,
                               max_total_tokens,
                               deadline_at >= clock_timestamp(),
                               projection_schema, projection_payload
                        FROM run_states
                        WHERE run_id = %s
                        FOR UPDATE
                        """,
                        (reservation.run_id,),
                    ).fetchone()
                    if (
                        runtime is None
                        or str(runtime[3]) != "stopped"
                        or not bool(runtime[12])
                        or not is_supported_projection_schema(
                            str(runtime[13])
                        )
                        or int(runtime[1]) + execution.model_tokens
                        > int(runtime[11])
                        or int(runtime[5]) + execution.model_turns
                        > int(runtime[8])
                        or int(runtime[6]) + execution.tool_calls
                        > int(runtime[9])
                        or int(runtime[7]) + execution.failures
                        > int(runtime[10])
                        or execution.model_tokens
                        > reservation.budget.max_total_tokens
                        or execution.model_turns
                        > reservation.budget.max_model_turns
                        or execution.tool_calls
                        > reservation.budget.max_tool_calls
                        or execution.failures
                        > reservation.budget.max_failures
                    ):
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        )
                    final_plan_hash = (
                        execution.plan_hash or reservation.plan_hash
                    )
                    if (
                        reservation.kind is InteractionKind.REAPPLY
                        and execution.plan_hash is None
                    ):
                        head = connection.execute(
                            """
                            SELECT version, plan_hash
                            FROM plan_heads
                            WHERE run_id = %s
                            FOR UPDATE
                            """,
                            (reservation.run_id,),
                        ).fetchone()
                        completed = connection.execute(
                            """
                            SELECT plan_hash
                            FROM completed_layout_heads
                            WHERE run_id = %s
                            """,
                            (reservation.run_id,),
                        ).fetchone()
                        if (
                            head is None
                            or completed is None
                            or str(head[1]) != reservation.plan_hash
                        ):
                            raise ServerError(
                                ServerErrorCode.INTERACTION_CONFLICT
                            )
                        final_plan_hash = str(completed[0])
                        if final_plan_hash != reservation.plan_hash:
                            completed_lineage = connection.execute(
                                """
                                SELECT version
                                FROM plan_lineage
                                WHERE run_id = %s AND plan_hash = %s
                                """,
                                (
                                    reservation.run_id,
                                    final_plan_hash,
                                ),
                            ).fetchone()
                            if completed_lineage is None:
                                raise ServerError(
                                    ServerErrorCode.INTERACTION_CONFLICT
                                )
                            connection.execute(
                                """
                                UPDATE plan_heads
                                SET version = %s, plan_hash = %s
                                WHERE run_id = %s
                                """,
                                (
                                    int(completed_lineage[0]),
                                    final_plan_hash,
                                    reservation.run_id,
                                ),
                            )
                    event = InteractionCompleted(
                        interaction_id=reservation.interaction_id,
                        kind=reservation.kind.value,
                        model_turns=execution.model_turns,
                        model_tokens=execution.model_tokens,
                        tool_calls=execution.tool_calls,
                        failures=execution.failures,
                        fresh_mapping_submitted=(
                            execution.fresh_mapping_submitted
                        ),
                        final_plan_hash=final_plan_hash,
                        plan_hash=execution.plan_hash,
                    )
                    try:
                        final_phase, reduced_plan_hash = (
                            reduce_interaction_head(
                                phase=Phase(str(runtime[2])),
                                plan_hash=(
                                    None
                                    if runtime[4] is None
                                    else str(runtime[4])
                                ),
                                event=event,
                            )
                        )
                    except (RuntimeDomainError, ValueError):
                        raise ServerError(
                            ServerErrorCode.INTERACTION_CONFLICT
                        ) from None
                    connection.execute(
                        """
                        INSERT INTO agent_session_batches
                            (session_id, revision, operation, items)
                        VALUES (%s, %s, 'add', %s::jsonb)
                        """,
                        (
                            str(session[0]),
                            execution.session_revision,
                            batch_json,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE agent_sessions
                        SET revision = %s, items = %s::jsonb,
                            updated_at = clock_timestamp()
                        WHERE session_id = %s
                        """,
                        (
                            execution.session_revision,
                            items_json,
                            str(session[0]),
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE run_states
                        SET event_sequence = event_sequence + 1,
                            model_turns = model_turns + %s,
                            model_tokens = model_tokens + %s,
                            tool_calls = tool_calls + %s,
                            failures = failures + %s,
                            phase = %s,
                            runtime_status = 'stopped',
                            plan_hash = %s,
                            projection_schema = %s,
                            projection_payload = %s::jsonb,
                            updated_at = clock_timestamp()
                        WHERE run_id = %s
                        """,
                        (
                            execution.model_turns,
                            execution.model_tokens,
                            execution.tool_calls,
                            execution.failures,
                            final_phase.value,
                            reduced_plan_hash,
                            STATE_PROJECTION_SCHEMA,
                            patch_state(
                                runtime[14],
                                schema_version=str(runtime[13]),
                                event_count=int(runtime[0]) + 1,
                                failures=(
                                    int(runtime[7])
                                    + execution.failures
                                ),
                                model_tokens=(
                                    int(runtime[1])
                                    + execution.model_tokens
                                ),
                                model_turns=(
                                    int(runtime[5])
                                    + execution.model_turns
                                ),
                                phase=final_phase.value,
                                plan_hash=reduced_plan_hash,
                                status="stopped",
                                stop_reason=(
                                    (
                                        "awaiting_approval"
                                        if final_phase
                                        is Phase.AWAITING_APPROVAL
                                        else None
                                    )
                                    if reservation.kind
                                    is not InteractionKind.QUESTION
                                    else runtime[14]["stop_reason"]
                                ),
                                tool_calls=(
                                    int(runtime[6])
                                    + execution.tool_calls
                                ),
                            ),
                            reservation.run_id,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO run_events
                            (run_id, sequence, event_type, payload)
                        VALUES (%s, %s, 'interaction_completed', %s)
                        """,
                        (
                            reservation.run_id,
                            int(runtime[0]) + 1,
                            encode_event(event),
                        ),
                    )
                    if execution.plan_hash is not None:
                        connection.execute(
                            """
                            UPDATE runs
                            SET status = 'awaiting_approval'
                            WHERE run_id = %s
                            """,
                            (reservation.run_id,),
                        )
                    elif reservation.kind is InteractionKind.REAPPLY:
                        connection.execute(
                            """
                            UPDATE runs
                            SET status = 'completed'
                            WHERE run_id = %s
                            """,
                            (reservation.run_id,),
                        )
                    if execution.plan_hash is not None:
                        head = connection.execute(
                            """
                            SELECT version, plan_hash FROM plan_heads
                            WHERE run_id = %s
                            FOR UPDATE
                            """,
                            (reservation.run_id,),
                        ).fetchone()
                        if (
                            head is None
                            or str(head[1]) != reservation.plan_hash
                        ):
                            raise ServerError(
                                ServerErrorCode.INTERACTION_CONFLICT
                            )
                        latest = connection.execute(
                            """
                            SELECT COALESCE(max(version), 0)
                            FROM plan_lineage
                            WHERE run_id = %s
                            """,
                            (reservation.run_id,),
                        ).fetchone()
                        if latest is None:
                            raise ServerError(
                                ServerErrorCode.INTERACTION_CONFLICT
                            )
                        version = int(latest[0]) + 1
                        connection.execute(
                            """
                            INSERT INTO plan_lineage
                                (run_id, version, plan_hash,
                                parent_plan_hash, plan_kind)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (
                                reservation.run_id,
                                version,
                                execution.plan_hash,
                                (
                                    execution.lineage_parent_hash
                                    or reservation.plan_hash
                                ),
                                (
                                    "amendment"
                                    if reservation.kind
                                    is InteractionKind.REAPPLY
                                    else "initial"
                                ),
                            ),
                        )
                        review = (
                            execution.plan_review
                            or PlanReview.system_only()
                        )
                        connection.execute(
                            """
                            INSERT INTO plan_reviews
                                (run_id, version, plan_hash,
                                 schema_version, payload)
                            VALUES (%s, %s, %s, %s, %s::jsonb)
                            """,
                            (
                                reservation.run_id,
                                version,
                                execution.plan_hash,
                                PLAN_REVIEW_SCHEMA,
                                review.canonical_bytes().decode("ascii"),
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE plan_heads
                            SET version = %s, plan_hash = %s
                            WHERE run_id = %s
                            """,
                            (
                                version,
                                execution.plan_hash,
                                reservation.run_id,
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE interactions
                        SET status = 'completed', result = %s::jsonb,
                            finished_at = clock_timestamp()
                        WHERE interaction_id = %s
                        """,
                        (encoded, reservation.interaction_id),
                    )
                    connection.execute(
                        """
                        DELETE FROM run_operations
                        WHERE run_id = %s AND operation_id = %s
                        """,
                        (
                            reservation.run_id,
                            reservation.interaction_id,
                        ),
                    )
            return result
        except ServerError:
            raise
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def fail(self, *, interaction_id: str) -> None:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE interactions
                        SET status = 'failed',
                            finished_at = clock_timestamp()
                        WHERE interaction_id = %s AND status = 'active'
                        RETURNING run_id
                        """,
                        (interaction_id,),
                    ).fetchone()
                    if row is not None:
                        connection.execute(
                            """
                            DELETE FROM run_operations
                            WHERE run_id = %s AND operation_id = %s
                            """,
                            (str(row[0]), interaction_id),
                        )
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    def reconcile_active(self) -> int:
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    rows = connection.execute(
                        """
                        UPDATE interactions
                        SET status = 'failed',
                            finished_at = clock_timestamp()
                        WHERE status = 'active'
                        RETURNING run_id
                        """
                    ).fetchall()
                    for row in rows:
                        connection.execute(
                            "DELETE FROM run_operations WHERE run_id = %s",
                            (str(row[0]),),
                        )
                    return len(rows)
        except Exception:
            raise ServerError(
                ServerErrorCode.DATABASE_UNAVAILABLE
            ) from None

    @staticmethod
    def _validate(
        reservation: InteractionReservation,
        execution: InteractionExecution,
    ) -> None:
        if reservation.kind is InteractionKind.QUESTION:
            if (
                execution.domain_events
                or execution.plan_hash is not None
                or execution.fresh_mapping_submitted
                or execution.lineage_parent_hash is not None
            ):
                raise ServerError(
                    ServerErrorCode.INTERACTION_INVALID_RESULT
                )
            return
        if reservation.kind is InteractionKind.REAPPLY:
            if (
                not execution.fresh_mapping_submitted
                or "mapping_submitted" not in execution.domain_events
                or (
                    execution.plan_hash is None
                    and "plan_built" in execution.domain_events
                )
                or (
                    execution.plan_hash is not None
                    and (
                        "plan_built" not in execution.domain_events
                        or execution.plan_hash == reservation.plan_hash
                        or execution.lineage_parent_hash is None
                    )
                )
            ):
                raise ServerError(ServerErrorCode.FRESH_MAPPING_REQUIRED)
            return
        if (
            not execution.fresh_mapping_submitted
            or "mapping_submitted" not in execution.domain_events
            or "plan_built" not in execution.domain_events
            or execution.plan_hash is None
            or execution.plan_hash == reservation.plan_hash
            or execution.lineage_parent_hash != reservation.plan_hash
        ):
            raise ServerError(ServerErrorCode.FRESH_MAPPING_REQUIRED)
