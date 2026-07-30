from __future__ import annotations

import pytest

from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.interaction_executor import _question_timeout_seconds
from reeloom.server.interaction_repository import (
    PostgresInteractionRepository,
    _fresh_interaction_budget,
)
from reeloom.server.interactions import InteractionKind


class _Cursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _Connection:
    def __init__(self) -> None:
        self.used_original_deadline = False

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def transaction(self) -> _Connection:
        return self

    def execute(
        self,
        query: object,
        parameters: object,
    ) -> _Cursor:
        del parameters
        statement = str(query)
        if "FROM interactions" in statement:
            return _Cursor(None)
        if "FROM plan_heads" in statement:
            return _Cursor(("sha256:" + "a" * 64,))
        if "FROM agent_sessions" in statement:
            return _Cursor((3,))
        if "FROM run_states AS state" in statement:
            self.used_original_deadline = "deadline_at" in statement
            return _Cursor(
                (
                    500,
                    "awaiting_approval",
                    4,
                    8,
                    1,
                    64,
                    64,
                    16,
                    100_000,
                    120.0,
                )
            )
        if "INSERT INTO run_operations" in statement:
            return _Cursor(("reserved",))
        if "INSERT INTO interactions" in statement:
            return _Cursor(None)
        raise AssertionError(statement)


class _Pool:
    def __init__(self) -> None:
        self.connection_value = _Connection()

    def connection(self) -> _Connection:
        return self.connection_value


def test_interaction_budget_reports_exhausted_model_turns() -> None:
    with pytest.raises(ServerError) as raised:
        _fresh_interaction_budget(
            model_tokens=500,
            model_turns=64,
            tool_calls=8,
            failures=0,
            max_model_turns=64,
            max_tool_calls=64,
            max_failures=16,
            max_total_tokens=100_000,
            max_elapsed_seconds=30.0,
        )

    assert (
        raised.value.code
        is ServerErrorCode.INTERACTION_BUDGET_EXHAUSTED
    )


def test_interaction_budget_refreshes_time_for_each_operation() -> None:
    budget = _fresh_interaction_budget(
        model_tokens=500,
        model_turns=4,
        tool_calls=8,
        failures=1,
        max_model_turns=64,
        max_tool_calls=64,
        max_failures=16,
        max_total_tokens=100_000,
        max_elapsed_seconds=120.0,
    )

    assert budget.max_model_turns == 60
    assert budget.max_tool_calls == 56
    assert budget.max_failures == 15
    assert budget.max_total_tokens == 99_500
    assert budget.max_elapsed_seconds == 120.0


def test_question_timeout_uses_the_fresh_operation_limit() -> None:
    budget = _fresh_interaction_budget(
        model_tokens=500,
        model_turns=4,
        tool_calls=8,
        failures=1,
        max_model_turns=64,
        max_tool_calls=64,
        max_failures=16,
        max_total_tokens=100_000,
        max_elapsed_seconds=30.0,
    )

    assert _question_timeout_seconds(budget) == 30.0


def test_question_reservation_ignores_expired_original_run_deadline() -> None:
    pool = _Pool()
    repository = PostgresInteractionRepository(pool)  # type: ignore[arg-type]

    reservation = repository.reserve(
        run_id="run-1",
        kind=InteractionKind.QUESTION,
        idempotency_key="question-after-unattended-delay",
        expected_plan_hash="sha256:" + "a" * 64,
        message="Explain the mapping.",
    )

    assert reservation.budget.max_elapsed_seconds == 120.0
    assert pool.connection_value.used_original_deadline is False
