from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from reeloom.executor.errors import ApprovalError
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.server.approval_repository import PostgresApprovalStore
from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
    ServerWorkType,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.database import PostgresControlPlane
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.idempotency import PostgresIdempotencyService
from reeloom.server.interaction_repository import (
    PostgresInteractionRepository,
)
from reeloom.server.interactions import InteractionKind
from reeloom.server.scheduler_repository import (
    PostgresSchedulerRepository,
)
from reeloom.server.watcher import WatchSnapshot


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


def _append_config(
    repository: PostgresConfigRepository,
) -> ConfigRevision:
    head = repository.head()
    expected = 0 if head is None else head.revision
    revision = ConfigRevision.create(
        revision_id=f"cfg-{uuid.uuid4().hex}",
        revision=expected + 1,
        created_at=datetime.now(UTC),
        draft=ConfigDraft(
            watches=(),
            archive_routes=(),
            provider=ProviderConfig(
                base_url="https://api.openai.com/v1",
                model="gpt-5",
                secret_ref="secret-test",
            ),
            apply_policy=ApplyPolicy.MANUAL,
        ),
    )
    return repository.compare_and_append(
        expected_revision=expected,
        revision=revision,
    )


@pytest.mark.postgres
def test_concurrent_run_registration_and_approval_claim_have_one_winner() -> None:
    control = PostgresControlPlane(_dsn())
    try:
        control.open()
        control.migrate()
        config = PostgresConfigRepository(control.pool).head()
        assert config is not None
        watch_id = f"watch-{uuid.uuid4().hex}"
        discovery_id = f"discovery-{uuid.uuid4().hex}"
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO watch_states
                        (watch_id, config_revision, fence, work_type,
                         settle_interval_seconds)
                    VALUES (%s, %s, %s, 'anime', 1)
                    """,
                    (watch_id, config.revision, config.revision),
                )
                connection.execute(
                    """
                    INSERT INTO discoveries
                        (discovery_id, watch_id, config_revision,
                         snapshot_id, snapshot_payload, work_type,
                         discovered_at)
                    VALUES (
                        %s, %s, %s, %s,
                        '{"files":[],"snapshot_id":"empty"}'::jsonb,
                        'anime', %s
                    )
                    """,
                    (
                        discovery_id,
                        watch_id,
                        config.revision,
                        f"snapshot-{uuid.uuid4().hex}",
                        datetime.now(UTC),
                    ),
                )
        scheduler = PostgresSchedulerRepository(control.pool)
        with ThreadPoolExecutor(max_workers=8) as executor:
            registrations = tuple(
                executor.map(
                    lambda _: scheduler.register_run(
                        discovery_id=discovery_id
                    ),
                    range(8),
                )
            )
        assert len({item.run_id for item in registrations}) == 1
        run_id = registrations[0].run_id
        with control.pool.connection() as connection:
            counts = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM runs WHERE discovery_id = %s),
                    (SELECT count(*) FROM jobs WHERE run_id = %s)
                """,
                (discovery_id, run_id),
            ).fetchone()
        assert tuple(counts) == (1, 1)

        plan_hash = "sha256:" + uuid.uuid4().hex * 2
        other_hash = "sha256:" + uuid.uuid4().hex * 2
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO plan_lineage
                        (run_id, version, plan_hash, plan_kind)
                    VALUES
                        (%s, 1, %s, 'initial'),
                        (%s, 2, %s, 'amendment')
                    """,
                    (run_id, plan_hash, run_id, other_hash),
                )
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO plan_heads
                            (run_id, version, plan_hash)
                        VALUES (%s, 1, %s)
                        """,
                        (run_id, other_hash),
                    )
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO plan_heads (run_id, version, plan_hash)
                    VALUES (%s, 1, %s)
                    """,
                    (run_id, plan_hash),
                )

        approval = ApprovalRecord.create(
            run_id=run_id,
            plan_hash=plan_hash,
            scope=ApprovalScope.APPLY,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            nonce=uuid.uuid4().hex,
        )
        approvals = PostgresApprovalStore(control.pool)
        approvals.issue(approval)

        def claim(_: int) -> bool:
            try:
                approvals.claim(
                    approval_id=approval.approval_id,
                    run_id=run_id,
                    plan_hash=approval.plan_hash,
                    scope=ApprovalScope.APPLY,
                )
                return True
            except ApprovalError:
                return False

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = tuple(executor.map(claim, range(8)))
        assert results.count(True) == 1

        current = [datetime.now(UTC)]
        reusable = PostgresApprovalStore(
            control.pool,
            clock=lambda: current[0],
        )
        first = reusable.issue_or_reuse(
            ApprovalRecord.create(
                run_id=run_id,
                plan_hash=other_hash,
                scope=ApprovalScope.APPLY,
                expires_at=current[0] + timedelta(seconds=1),
                nonce=uuid.uuid4().hex,
            )
        )
        current[0] += timedelta(seconds=2)
        replacement = reusable.issue_or_reuse(
            ApprovalRecord.create(
                run_id=run_id,
                plan_hash=other_hash,
                scope=ApprovalScope.APPLY,
                expires_at=current[0] + timedelta(minutes=5),
                nonce=uuid.uuid4().hex,
            )
        )
        assert replacement.approval_id != first.approval_id
        assert (
            reusable.claim(
                approval_id=replacement.approval_id,
                run_id=run_id,
                plan_hash=other_hash,
                scope=ApprovalScope.APPLY,
            )
            == replacement
        )
        with pytest.raises(ApprovalError):
            reusable.issue_or_reuse(
                ApprovalRecord.create(
                    run_id=run_id,
                    plan_hash=other_hash,
                    scope=ApprovalScope.APPLY,
                    expires_at=current[0] + timedelta(minutes=5),
                    nonce=uuid.uuid4().hex,
                )
            )
    finally:
        control.close()


@pytest.mark.postgres
def test_durable_http_idempotency_replays_without_reexecuting() -> None:
    control = PostgresControlPlane(_dsn())
    try:
        control.open()
        control.migrate()
        service = PostgresIdempotencyService(control.pool)
        key = f"idem-{uuid.uuid4().hex}"
        calls = 0

        def execute() -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"status": "done", "value": 7}

        first = service.run(
            scope="test",
            subject_id="subject",
            idempotency_key=key,
            request={"expected": 1},
            execute=execute,
        )
        second = service.run(
            scope="test",
            subject_id="subject",
            idempotency_key=key,
            request={"expected": 1},
            execute=execute,
        )

        assert first == second == {"status": "done", "value": 7}
        assert calls == 1
    finally:
        control.close()


@pytest.mark.postgres
def test_failed_idempotency_record_uses_typed_resolver() -> None:
    control = PostgresControlPlane(_dsn())
    try:
        control.open()
        control.migrate()
        service = PostgresIdempotencyService(control.pool)
        key = f"idem-{uuid.uuid4().hex}"
        calls = 0

        def execute() -> dict[str, object]:
            nonlocal calls
            calls += 1
            raise RuntimeError("simulated finalize uncertainty")

        with pytest.raises(RuntimeError):
            service.run(
                scope="test",
                subject_id="subject",
                idempotency_key=key,
                request={"expected": 1},
                execute=execute,
            )
        resolved = service.run(
            scope="test",
            subject_id="subject",
            idempotency_key=key,
            request={"expected": 1},
            execute=execute,
            resolve=lambda: {"status": "durably_settled"},
        )

        assert resolved == {"status": "durably_settled"}
        assert calls == 1
    finally:
        control.close()


@pytest.mark.postgres
def test_concurrent_idempotency_reservation_reports_busy_not_database_error(
) -> None:
    control = PostgresControlPlane(_dsn())
    try:
        control.open()
        control.migrate()
        service = PostgresIdempotencyService(control.pool)
        key = f"idem-{uuid.uuid4().hex}"
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def execute() -> dict[str, object]:
            nonlocal calls
            calls += 1
            started.set()
            assert release.wait(timeout=5)
            return {"status": "done"}

        def run() -> dict[str, object]:
            return service.run(
                scope="test",
                subject_id="subject",
                idempotency_key=key,
                request={"expected": 1},
                execute=execute,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(run)
            assert started.wait(timeout=5)
            second = executor.submit(run)
            try:
                with pytest.raises(ServerError) as raised:
                    second.result(timeout=5)
                assert raised.value.code is ServerErrorCode.RUN_BUSY
            finally:
                release.set()
            assert first.result(timeout=5) == {"status": "done"}
        assert calls == 1
    finally:
        control.close()


@pytest.mark.postgres
def test_same_run_interaction_reservation_has_one_winner() -> None:
    control = PostgresControlPlane(_dsn())
    try:
        control.open()
        control.migrate()
        config = _append_config(
            PostgresConfigRepository(control.pool)
        )
        watch_id = f"watch-{uuid.uuid4().hex}"
        discovery_id = f"discovery-{uuid.uuid4().hex}"
        run_id = f"run-{uuid.uuid4().hex}"
        plan_hash = "sha256:" + uuid.uuid4().hex * 2
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO watch_states
                        (watch_id, config_revision, fence, work_type,
                         settle_interval_seconds)
                    VALUES (%s, %s, %s, 'anime', 1)
                    """,
                    (watch_id, config.revision, config.revision),
                )
                connection.execute(
                    """
                    INSERT INTO discoveries
                        (discovery_id, watch_id, config_revision,
                         snapshot_id, snapshot_payload, work_type,
                         discovered_at)
                    VALUES (
                        %s, %s, %s, %s, '{}'::jsonb, 'anime', %s
                    )
                    """,
                    (
                        discovery_id,
                        watch_id,
                        config.revision,
                        f"snapshot-{uuid.uuid4().hex}",
                        datetime.now(UTC),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO runs
                        (run_id, discovery_id, config_revision, work_type,
                         source_capability, status)
                    VALUES (%s, %s, %s, 'anime', %s, 'awaiting_approval')
                    """,
                    (
                        run_id,
                        discovery_id,
                        config.revision,
                        f"cap-{uuid.uuid4().hex}",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO plan_lineage
                        (run_id, version, plan_hash, plan_kind)
                    VALUES (%s, 1, %s, 'initial')
                    """,
                    (run_id, plan_hash),
                )
                connection.execute(
                    """
                    INSERT INTO plan_heads (run_id, version, plan_hash)
                    VALUES (%s, 1, %s)
                    """,
                    (run_id, plan_hash),
                )
                connection.execute(
                    """
                    INSERT INTO agent_sessions
                        (session_id, run_id, revision, items)
                    VALUES (%s, %s, 0, '[]'::jsonb)
                    """,
                    (run_id, run_id),
                )
                connection.execute(
                    """
                    INSERT INTO run_states
                        (run_id, event_sequence, phase, runtime_status,
                         model_turns, model_tokens, tool_calls, failures,
                         plan_hash, deadline_at)
                    VALUES (
                        %s, 1, 'awaiting_approval', 'stopped',
                        0, 0, 0, 0, %s, %s
                    )
                    """,
                    (
                        run_id,
                        plan_hash,
                        datetime.now(UTC) + timedelta(minutes=5),
                    ),
                )
        repository = PostgresInteractionRepository(control.pool)

        def reserve(index: int) -> object:
            try:
                return repository.reserve(
                    run_id=run_id,
                    kind=InteractionKind.QUESTION,
                    idempotency_key=f"question-{index}",
                    expected_plan_hash=plan_hash,
                    message="Explain this plan.",
                )
            except ServerError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(reserve, range(2)))
        winners = tuple(
            item
            for item in results
            if not isinstance(item, ServerErrorCode)
        )

        assert len(winners) == 1
        assert results.count(ServerErrorCode.RUN_BUSY) == 1
        repository.fail(
            interaction_id=winners[0].interaction_id
        )
    finally:
        control.close()


@pytest.mark.postgres
def test_poll_rejects_revision_that_is_no_longer_config_head() -> None:
    control = PostgresControlPlane(_dsn())
    try:
        control.open()
        control.migrate()
        configs = PostgresConfigRepository(control.pool)
        old = _append_config(configs)
        scheduler = PostgresSchedulerRepository(control.pool)
        watch_id = f"watch-{uuid.uuid4().hex}"
        scheduler.configure_watch(
            watch_id=watch_id,
            config_revision=old.revision,
            fence=old.revision,
            work_type=ServerWorkType.ANIME,
            settle_interval_seconds=1,
        )
        _append_config(configs)

        with pytest.raises(ServerError) as raised:
            scheduler.reconcile_poll(
                watch_id=watch_id,
                config_revision=old.revision,
                fence=old.revision,
                observed_at=datetime.now(UTC),
                snapshot=WatchSnapshot(
                    snapshot_id=f"snapshot-{uuid.uuid4().hex}",
                    files=(),
                ),
            )

        assert raised.value.code is ServerErrorCode.STALE_WATCH_SCAN
        with control.pool.connection() as connection:
            count = connection.execute(
                """
                SELECT count(*) FROM watch_observations
                WHERE watch_id = %s
                """,
                (watch_id,),
            ).fetchone()
        assert int(count[0]) == 0
    finally:
        control.close()


@pytest.mark.postgres
def test_boot_reconcile_is_repeatable_and_settles_terminal_run() -> None:
    control = PostgresControlPlane(_dsn())
    try:
        control.open()
        control.migrate()
        config = _append_config(PostgresConfigRepository(control.pool))
        watch_id = f"watch-{uuid.uuid4().hex}"
        discovery_id = f"discovery-{uuid.uuid4().hex}"
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO watch_states
                        (watch_id, config_revision, fence, work_type,
                         settle_interval_seconds)
                    VALUES (%s, %s, %s, 'anime', 1)
                    """,
                    (watch_id, config.revision, config.revision),
                )
                connection.execute(
                    """
                    INSERT INTO discoveries
                        (discovery_id, watch_id, config_revision,
                         snapshot_id, snapshot_payload, work_type,
                         discovered_at)
                    VALUES (
                        %s, %s, %s, %s,
                        '{"files":[],"snapshot_id":"empty"}'::jsonb,
                        'anime', %s
                    )
                    """,
                    (
                        discovery_id,
                        watch_id,
                        config.revision,
                        f"snapshot-{uuid.uuid4().hex}",
                        datetime.now(UTC),
                    ),
                )
        scheduler = PostgresSchedulerRepository(control.pool)
        registration = scheduler.register_run(discovery_id=discovery_id)
        boots = tuple(f"boot-{uuid.uuid4().hex}" for _ in range(3))
        for boot_id in boots:
            control.register_boot(boot_id)

        def mark_running(boot_id: str) -> None:
            with control.pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'running', boot_id = %s
                        WHERE job_id = %s
                        """,
                        (boot_id, registration.job_id),
                    )

        mark_running(boots[0])
        scheduler.reconcile_boot(current_boot_id="boot-new-1")
        mark_running(boots[1])
        scheduler.reconcile_boot(current_boot_id="boot-new-2")
        mark_running(boots[2])
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE runs SET status = 'completed'
                    WHERE run_id = %s
                    """,
                    (registration.run_id,),
                )
        scheduler.reconcile_boot(current_boot_id="boot-new-3")

        with control.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT status FROM jobs WHERE job_id = %s
                """,
                (registration.job_id,),
            ).fetchone()
        assert str(row[0]) == "completed"
    finally:
        control.close()
