from __future__ import annotations

from contextlib import nullcontext

from reeloom.server.notification_intents import (
    PostgresNotificationIntentWorker,
)


class _Cursor:
    def __init__(self, row: object = None) -> None:
        self._row = row

    def fetchone(self) -> object:
        return self._row


class _Connection:
    def __init__(self, intent: tuple[object, ...], control: tuple[object, ...]) -> None:
        self.intent = intent
        self.control = control
        self.state: str | None = None

    def transaction(self) -> object:
        return nullcontext()

    def execute(self, query: str, params: object = None) -> _Cursor:
        if "FROM notification_intents_v2 AS intent" in query:
            return _Cursor(self.intent)
        if "SELECT revision, operation_id" in query:
            return _Cursor(self.control)
        if "UPDATE notification_intents_v2" in query:
            assert isinstance(params, tuple)
            self.state = str(params[0])
            return _Cursor()
        raise AssertionError(query)


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def connection(self) -> object:
        return nullcontext(self._connection)


class _Projector:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def plan_ready_from_projection(self, connection: object, **value: object) -> None:
        del connection
        if self.fail:
            raise ValueError("missing plan")
        self.calls.append(("plan_ready", str(value["run_id"])))

    def operation_completed_from_projection(
        self, connection: object, **value: object
    ) -> None:
        del connection
        self.calls.append(("completed", str(value["run_id"])))

    def attention_from_projection(self, connection: object, **value: object) -> None:
        del connection
        self.calls.append(("attention", str(value["run_id"])))


def _worker(
    *,
    kind: str,
    policy: str,
    operation_id: str | None = None,
    operation_status: str | None = None,
    intent_revision: int = 2,
    control_revision: int = 2,
) -> tuple[PostgresNotificationIntentWorker, _Connection, _Projector]:
    connection = _Connection(
        (
            "intent:1",
            "run:1",
            intent_revision,
            operation_id,
            kind,
            "sha256:" + "a" * 64,
            "media_move",
            policy,
            operation_status,
            1,
        ),
        (control_revision, operation_id),
    )
    projector = _Projector()
    return (
        PostgresNotificationIntentWorker(
            pool=_Pool(connection),  # type: ignore[arg-type]
            projector=projector,  # type: ignore[arg-type]
        ),
        connection,
        projector,
    )


def test_automatic_plan_ready_intent_is_cancelled() -> None:
    worker, connection, projector = _worker(
        kind="plan_ready", policy="automatic"
    )

    assert worker.process_one()
    assert connection.state == "cancelled"
    assert projector.calls == []


def test_manual_plan_ready_is_projected_once() -> None:
    worker, connection, projector = _worker(
        kind="plan_ready", policy="manual"
    )

    assert worker.process_one()
    assert connection.state == "projected"
    assert projector.calls == [("plan_ready", "run:1")]


def test_completed_operation_intent_uses_terminal_operation_truth() -> None:
    worker, connection, projector = _worker(
        kind="operation_completed",
        policy="automatic",
        operation_id="operation:1",
        operation_status="completed",
    )

    assert worker.process_one()
    assert connection.state == "projected"
    assert projector.calls == [("completed", "run:1")]


def test_stale_control_revision_cancels_intent() -> None:
    worker, connection, projector = _worker(
        kind="attention_required",
        policy="automatic",
        operation_id="operation:1",
        operation_status="collision",
        control_revision=3,
    )

    assert worker.process_one()
    assert connection.state == "cancelled"
    assert projector.calls == []


def test_poison_projection_is_dead_lettered_without_blocking_queue() -> None:
    worker, connection, projector = _worker(
        kind="plan_ready", policy="manual"
    )
    projector.fail = True

    assert worker.process_one()
    assert connection.state == "dead"
    assert projector.calls == []
