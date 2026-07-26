from __future__ import annotations

import json
import threading

from psycopg_pool import ConnectionPool

from reeloom.runtime.errors import RuntimeDomainError, RuntimeErrorCode
from reeloom.runtime.event_codec import encode_event
from reeloom.runtime.events import PlanBuilt, RunStarted, RuntimeEvent
from reeloom.runtime.reducer import reduce_event
from reeloom.runtime.state_codec import (
    STATE_PROJECTION_SCHEMA,
    canonical_state,
    decode_state,
)
from reeloom.runtime.state import Phase, RunState
from reeloom.runtime.store import StoredEvent
from reeloom.ports.plans import PlanStore
from reeloom.kernel.rename_plan import RenamePlan

_MAX_PROJECTION_BYTES = 10 * 1024 * 1024


def _error(code: RuntimeErrorCode) -> RuntimeDomainError:
    return RuntimeDomainError(code)


def _run_status(state: RunState) -> str:
    if state.phase is Phase.AWAITING_APPROVAL:
        return "awaiting_approval"
    if state.phase is Phase.APPLYING:
        return "applying"
    if state.phase is Phase.COMPLETED:
        return "completed"
    if state.phase is Phase.ROLLED_BACK:
        return "rolled_back"
    if state.phase is Phase.FAILED:
        return "failed"
    return "running"


class PostgresEventStore:
    """Append event and update the indexed run projection atomically."""

    def __init__(
        self,
        pool: ConnectionPool,
        *,
        run_id: str,
        plans: PlanStore | None = None,
    ) -> None:
        self._pool = pool
        self.run_id = run_id
        self._plans = plans
        self._events: list[StoredEvent] = []
        self._state: RunState | None = None
        self._lock = threading.Lock()
        self._load()

    @property
    def state(self) -> RunState | None:
        return self._state

    @property
    def events(self) -> tuple[StoredEvent, ...]:
        return tuple(self._events)

    def append(self, event: RuntimeEvent) -> RunState:
        with self._lock:
            sequence = (
                1 if self._state is None else self._state.event_count + 1
            )
            if sequence == 1 and (
                not isinstance(event, RunStarted)
                or event.run_id != self.run_id
            ):
                raise _error(RuntimeErrorCode.RUN_ID_MISMATCH)
            next_state = reduce_event(self._state, event)
            encoded = (
                json.dumps(
                    {
                        "event_type": "plan_built",
                        "payload": {"plan_hash": event.plan.plan_hash},
                        "schema_version": "runtime-event-ref-v1",
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("ascii")
                if isinstance(event, PlanBuilt)
                else encode_event(event)
            )
            envelope = json.loads(encoded)
            projection = canonical_state(next_state)
            if len(projection.encode("utf-8")) > _MAX_PROJECTION_BYTES:
                raise _error(RuntimeErrorCode.EVENT_STORE_FAILURE)
            try:
                with self._pool.connection() as connection:
                    with connection.transaction():
                        connection.execute(
                            """
                            SELECT pg_advisory_xact_lock(
                                hashtextextended(%s, 0)
                            )
                            """,
                            (self.run_id,),
                        )
                        row = connection.execute(
                            """
                            SELECT event_sequence
                            FROM run_states
                            WHERE run_id = %s
                            FOR UPDATE
                            """,
                            (self.run_id,),
                        ).fetchone()
                        current = 0 if row is None else int(row[0])
                        if current != sequence - 1:
                            raise _error(
                                RuntimeErrorCode.EVENT_STORE_CONFLICT
                            )
                        connection.execute(
                            """
                            INSERT INTO run_events
                                (run_id, sequence, event_type, payload)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (
                                self.run_id,
                                sequence,
                                str(envelope["event_type"]),
                                encoded,
                            ),
                        )
                        connection.execute(
                            """
                            INSERT INTO run_states
                                (run_id, event_sequence, phase,
                                 runtime_status, model_turns, model_tokens,
                                 tool_calls, failures, plan_hash, deadline_at,
                                 max_model_turns, max_tool_calls,
                                 max_failures, max_total_tokens,
                                 projection_schema, projection_payload)
                            VALUES
                                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                 %s, %s, %s, %s, %s, %s::jsonb)
                            ON CONFLICT (run_id) DO UPDATE SET
                                event_sequence = EXCLUDED.event_sequence,
                                phase = EXCLUDED.phase,
                                runtime_status = EXCLUDED.runtime_status,
                                model_turns = EXCLUDED.model_turns,
                                model_tokens = EXCLUDED.model_tokens,
                                tool_calls = EXCLUDED.tool_calls,
                                failures = EXCLUDED.failures,
                                plan_hash = EXCLUDED.plan_hash,
                                deadline_at = EXCLUDED.deadline_at,
                                max_model_turns =
                                    EXCLUDED.max_model_turns,
                                max_tool_calls =
                                    EXCLUDED.max_tool_calls,
                                max_failures = EXCLUDED.max_failures,
                                max_total_tokens =
                                    EXCLUDED.max_total_tokens,
                                projection_schema =
                                    EXCLUDED.projection_schema,
                                projection_payload =
                                    EXCLUDED.projection_payload,
                                updated_at = clock_timestamp()
                            """,
                            (
                                self.run_id,
                                sequence,
                                next_state.phase.value,
                                next_state.status.value,
                                next_state.model_turns,
                                next_state.model_tokens,
                                next_state.tool_calls,
                                next_state.failures,
                                next_state.plan_hash,
                                next_state.deadline_at,
                                next_state.budget.max_model_turns,
                                next_state.budget.max_tool_calls,
                                next_state.budget.max_failures,
                                next_state.budget.max_total_tokens,
                                STATE_PROJECTION_SCHEMA,
                                projection,
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE runs
                            SET status = %s
                            WHERE run_id = %s
                            """,
                            (_run_status(next_state), self.run_id),
                        )
                        if isinstance(event, PlanBuilt):
                            self._append_plan_lineage(
                                connection,
                                plan_hash=event.plan.plan_hash,
                            )
            except RuntimeDomainError:
                raise
            except Exception:
                raise _error(RuntimeErrorCode.EVENT_STORE_FAILURE) from None
            self._events.append(StoredEvent(sequence, event))
            self._state = next_state
            return next_state

    def replay(self) -> RunState | None:
        return self._state

    def _load(self) -> None:
        try:
            with self._pool.connection() as connection:
                projection = connection.execute(
                    """
                    SELECT event_sequence, phase, runtime_status,
                           model_turns, model_tokens, tool_calls, failures,
                           plan_hash, deadline_at, max_model_turns,
                           max_tool_calls, max_failures, max_total_tokens,
                           projection_schema, projection_payload
                    FROM run_states
                    WHERE run_id = %s
                    """,
                    (self.run_id,),
                ).fetchone()
        except Exception:
            raise _error(RuntimeErrorCode.EVENT_STORE_FAILURE) from None
        if projection is None:
            self._state = None
            return
        try:
            if str(projection[13]) != STATE_PROJECTION_SCHEMA:
                raise ValueError
            state = decode_state(
                projection[14],
                load_plan=self._load_plan,
            )
            if (
                state.run_id != self.run_id
                or int(projection[0]) != state.event_count
                or str(projection[1]) != state.phase.value
                or str(projection[2]) != state.status.value
                or int(projection[3]) != state.model_turns
                or int(projection[4]) != state.model_tokens
                or int(projection[5]) != state.tool_calls
                or int(projection[6]) != state.failures
                or projection[7] != state.plan_hash
                or projection[8] != state.deadline_at
                or int(projection[9]) != state.budget.max_model_turns
                or int(projection[10]) != state.budget.max_tool_calls
                or int(projection[11]) != state.budget.max_failures
                or int(projection[12]) != state.budget.max_total_tokens
            ):
                raise ValueError
        except Exception:
            raise _error(RuntimeErrorCode.EVENT_STORE_CORRUPT) from None
        self._state = state

    def _load_plan(self, plan_hash: str) -> RenamePlan:
        if self._plans is None:
            raise ValueError
        return RenamePlan.from_canonical_bytes(
            self._plans.load(plan_hash),
            plan_hash=plan_hash,
        )

    def _append_plan_lineage(
        self,
        connection: object,
        *,
        plan_hash: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT version, plan_hash
            FROM plan_heads
            WHERE run_id = %s
            FOR UPDATE
            """,
            (self.run_id,),
        ).fetchone()
        version = 1 if row is None else int(row[0]) + 1
        parent = None if row is None else str(row[1])
        connection.execute(
            """
            INSERT INTO plan_lineage
                (run_id, version, plan_hash, parent_plan_hash, plan_kind)
            VALUES (%s, %s, %s, %s, 'initial')
            """,
            (self.run_id, version, plan_hash, parent),
        )
        connection.execute(
            """
            INSERT INTO plan_heads (run_id, version, plan_hash)
            VALUES (%s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                version = EXCLUDED.version,
                plan_hash = EXCLUDED.plan_hash
            """,
            (self.run_id, version, plan_hash),
        )
