from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from reeloom.executor.errors import ApprovalError, ApprovalErrorCode
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.forward_execution import ExecutionOperation
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.events import RunStarted
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
from reeloom.server.forward_operation_repository import (
    ForwardOperationError,
    ForwardOperationErrorCode,
    PostgresForwardOperationRepository,
    execution_operation_id,
)
from reeloom.server.interaction_repository import (
    PostgresInteractionRepository,
)
from reeloom.server.interactions import InteractionKind
from reeloom.server.scheduler_repository import (
    PostgresSchedulerRepository,
)
from reeloom.server.run_control_repository import (
    PostgresRunControlRepository,
)
from reeloom.server.run_lifecycle import RunEffectKind
from reeloom.server.runtime_store import PostgresEventStore
from reeloom.server.watcher import NoFollowWatcher, WatchSnapshot


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
def test_legacy_claim_and_v2_authorize_share_one_run_fence() -> None:
    control = PostgresControlPlane(_dsn())
    suffix = uuid.uuid4().hex
    now = datetime.now(UTC)
    run_id = f"run-approval-fence-{suffix}"
    plan_hash = "sha256:" + uuid.uuid4().hex * 2
    try:
        control.open()
        control.migrate()
        config = _append_config(PostgresConfigRepository(control.pool))
        watch_id = f"watch-approval-fence-{suffix}"
        discovery_id = f"discovery-approval-fence-{suffix}"
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO watch_states
                        (watch_id, config_revision, fence, work_type,
                         settle_interval_seconds, semantic_v2)
                    VALUES (%s, %s, %s, 'anime', 1, true)
                    """,
                    (watch_id, config.revision, config.revision),
                )
                connection.execute(
                    """
                    INSERT INTO discoveries
                        (discovery_id, watch_id, config_revision,
                         snapshot_id, snapshot_payload, work_type,
                         discovered_at)
                    VALUES (%s, %s, %s, %s, '{}'::jsonb, 'anime', %s)
                    """,
                    (
                        discovery_id,
                        watch_id,
                        config.revision,
                        "candidate-snapshot-v2:" + uuid.uuid4().hex * 2,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO runs
                        (run_id, discovery_id, config_revision, work_type,
                         source_capability, status)
                    VALUES (%s, %s, %s, 'anime', %s, 'awaiting_approval')
                    """,
                    (run_id, discovery_id, config.revision, f"cap-{suffix}"),
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
                    INSERT INTO run_lifecycle_controls_v2
                        (run_id, mode, classification_reason, revision,
                         effect_kind, effect_plan_hash, effect_policy,
                         handoff_event_sequence)
                    VALUES (%s, 'forward_v2', 'approval_fence_test', 1,
                            'media_move', %s, 'manual', 1)
                    """,
                    (run_id, plan_hash),
                )
        approval = ApprovalRecord.create(
            run_id=run_id,
            plan_hash=plan_hash,
            scope=ApprovalScope.APPLY,
            expires_at=now + timedelta(minutes=5),
            nonce=uuid.uuid4().hex,
        )
        approvals = PostgresApprovalStore(control.pool)
        approvals.issue(approval)
        operations = PostgresForwardOperationRepository(control.pool)
        operation = ExecutionOperation.authorized(
            operation_id=execution_operation_id(
                run_id=run_id,
                plan_hash=plan_hash,
            ),
            run_id=run_id,
            plan_hash=plan_hash,
        )
        barrier = threading.Barrier(2)

        def legacy_claim() -> str:
            barrier.wait()
            try:
                approvals.claim(
                    approval_id=approval.approval_id,
                    run_id=run_id,
                    plan_hash=plan_hash,
                    scope=ApprovalScope.APPLY,
                )
                return "legacy"
            except ApprovalError as error:
                assert error.code is ApprovalErrorCode.ALREADY_CLAIMED
                return "blocked"

        def v2_authorize() -> str:
            barrier.wait()
            try:
                operations.authorize(
                    operation,
                    approval_id=approval.approval_id,
                    now=now,
                )
                return "v2"
            except ForwardOperationError as error:
                assert error.code is ForwardOperationErrorCode.OPERATION_CONFLICT
                return "blocked"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(legacy_claim),
                executor.submit(v2_authorize),
            )
            outcomes = tuple(future.result() for future in futures)
        assert outcomes.count("blocked") == 1
        assert len({*outcomes} & {"legacy", "v2"}) == 1
        with control.pool.connection() as connection:
            consumed = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM approval_claims
                     WHERE run_id = %s AND plan_hash = %s),
                    (SELECT count(*) FROM execution_operations_v2
                     WHERE run_id = %s AND plan_hash = %s)
                """,
                (run_id, plan_hash, run_id, plan_hash),
            ).fetchone()
        assert consumed is not None
        assert int(consumed[0]) + int(consumed[1]) == 1
    finally:
        control.close()


@pytest.mark.postgres
def test_boot_reconcile_repairs_discovery_committed_without_run(tmp_path) -> None:
    control = PostgresControlPlane(_dsn())
    incoming = tmp_path / "orphan-discovery"
    release = incoming / "Release"
    release.mkdir(parents=True)
    (release / "episode.mkv").write_bytes(b"video")
    try:
        control.open()
        control.migrate()
        revision = _append_config(PostgresConfigRepository(control.pool))
        scheduler = PostgresSchedulerRepository(control.pool)
        watch_id = f"watch-{uuid.uuid4().hex}"
        scheduler.configure_watch(
            watch_id=watch_id,
            config_revision=revision.revision,
            fence=revision.revision,
            work_type=ServerWorkType.ANIME,
            settle_interval_seconds=1,
            semantic_v2=True,
        )
        scan = NoFollowWatcher().scan_folders(
            AuthorizedRoot.create(incoming)
        )
        observed = datetime.now(UTC)
        scheduler.reconcile_folders(
            watch_id=watch_id,
            config_revision=revision.revision,
            fence=revision.revision,
            observed_at=observed,
            scan=scan,
        )
        discovery_id = f"discovery-{uuid.uuid4().hex}"
        with control.pool.connection() as connection:
            with connection.transaction():
                observation = connection.execute(
                    """
                    SELECT snapshot_id, snapshot_payload::text,
                           inventory_id, inventory_payload::text
                    FROM watch_folder_observations
                    WHERE watch_id = %s AND folder_name = 'Release'
                    """,
                    (watch_id,),
                ).fetchone()
                assert observation is not None
                connection.execute(
                    """
                    INSERT INTO discoveries
                        (discovery_id, watch_id, config_revision,
                         snapshot_id, snapshot_payload, work_type,
                         discovered_at, source_folder,
                         folder_generation_id, inventory_id)
                    VALUES (%s, %s, %s, %s, %s::jsonb, 'anime', %s,
                            'Release', %s, %s)
                    """,
                    (
                        discovery_id,
                        watch_id,
                        revision.revision,
                        str(observation[0]),
                        observation[1],
                        observed,
                        f"folder-{uuid.uuid4().hex}",
                        str(observation[2]),
                    ),
                )
                connection.execute(
                    """
                    UPDATE watch_folder_observations
                    SET discovery_id = %s, status = 'active', stable_at = %s
                    WHERE watch_id = %s AND folder_name = 'Release'
                    """,
                    (discovery_id, observed, watch_id),
                )

        assert scheduler.reconcile_boot(current_boot_id="boot-repair") == 1
        result = scheduler.reconcile_folders(
            watch_id=watch_id,
            config_revision=revision.revision,
            fence=revision.revision,
            observed_at=observed + timedelta(seconds=1),
            scan=scan,
        )

        assert result.discoveries == ()
        with control.pool.connection() as connection:
            repaired = connection.execute(
                """
                SELECT run.run_id, job.status, control.mode
                FROM runs AS run
                JOIN jobs AS job USING (run_id)
                JOIN run_lifecycle_controls_v2 AS control USING (run_id)
                WHERE run.discovery_id = %s
                """,
                (discovery_id,),
            ).fetchone()
        assert repaired is not None
        assert repaired[1:] == ("pending", "forward_v2")

        drift_now = observed + timedelta(seconds=2)
        lease_expires_at = drift_now + timedelta(minutes=1)
        request_id = f"generation-{uuid.uuid4().hex}"
        with control.pool.connection() as connection:
            observation = connection.execute(
                """
                SELECT discovery_id, inventory_id
                FROM watch_folder_observations
                WHERE watch_id = %s AND folder_name = 'Release'
                """,
                (watch_id,),
            ).fetchone()
            assert observation is not None
            connection.execute(
                """
                INSERT INTO generation_requests_v2
                    (request_id, request_kind, origin_run_id,
                     watch_id, source_folder, expected_inventory_id,
                     generation_nonce, state, attempt_count,
                     lease_owner, lease_expires_at)
                VALUES (%s, 'operation_rescan', %s, %s, 'Release', %s,
                        %s, 'leased', 1, 'worker-drift', %s)
                """,
                (
                    request_id,
                    str(repaired[0]),
                    watch_id,
                    str(observation[1]),
                    f"nonce-{uuid.uuid4().hex}",
                    lease_expires_at,
                ),
            )
        changed_revision = _append_config(
            PostgresConfigRepository(control.pool)
        )
        scheduler.configure_watch(
            watch_id=watch_id,
            config_revision=changed_revision.revision,
            fence=changed_revision.revision,
            work_type=ServerWorkType.ANIME,
            settle_interval_seconds=1,
            semantic_v2=True,
        )

        with pytest.raises(ServerError) as drifted:
            scheduler.accept_generation_request(
                request_id=request_id,
                worker_id="worker-drift",
                attempt_count=1,
                lease_expires_at=lease_expires_at,
                now=drift_now,
            )
        assert drifted.value.code is ServerErrorCode.STALE_WATCH_SCAN

        scheduler.reconcile_folders(
            watch_id=watch_id,
            config_revision=changed_revision.revision,
            fence=changed_revision.revision,
            observed_at=drift_now,
            scan=scan,
        )
        scheduler.accept_generation_request(
            request_id=request_id,
            worker_id="worker-drift",
            attempt_count=1,
            lease_expires_at=lease_expires_at,
            now=drift_now,
        )
        with control.pool.connection() as connection:
            assert connection.execute(
                """
                SELECT state FROM generation_requests_v2
                WHERE request_id = %s
                """,
                (request_id,),
            ).fetchone() == ("accepted",)
            assert connection.execute(
                """
                SELECT discovery_id, config_revision
                FROM watch_folder_observations
                WHERE watch_id = %s AND folder_name = 'Release'
                """,
                (watch_id,),
            ).fetchone() == (None, changed_revision.revision)
    finally:
        control.close()


@pytest.mark.postgres
def test_terminalization_and_operation_authorization_have_one_winner() -> None:
    control = PostgresControlPlane(_dsn())
    suffix = uuid.uuid4().hex
    now = datetime.now(UTC)
    watch_id = f"watch-terminal-fence-{suffix}"
    discovery_id = f"discovery-terminal-fence-{suffix}"
    plan_hash = "sha256:" + uuid.uuid4().hex * 2
    snapshot_id = "candidate-snapshot-v2:" + uuid.uuid4().hex * 2
    generation_id = "folder-generation-v2:" + uuid.uuid4().hex * 2
    inventory_id = "folder-inventory-v2:" + uuid.uuid4().hex * 2
    try:
        control.open()
        control.migrate()
        config = _append_config(PostgresConfigRepository(control.pool))
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO watch_states
                        (watch_id, config_revision, fence, work_type,
                         settle_interval_seconds, semantic_v2)
                    VALUES (%s, %s, %s, 'anime', 1, true)
                    """,
                    (watch_id, config.revision, config.revision),
                )
                connection.execute(
                    """
                    INSERT INTO discoveries
                        (discovery_id, watch_id, config_revision,
                         snapshot_id, snapshot_payload, work_type,
                         discovered_at, source_folder,
                         folder_generation_id, inventory_id)
                    VALUES (%s, %s, %s, %s, '{}'::jsonb, 'anime', %s,
                            'TerminalFence', %s, %s)
                    """,
                    (
                        discovery_id,
                        watch_id,
                        config.revision,
                        snapshot_id,
                        now,
                        generation_id,
                        inventory_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO watch_folder_observations
                        (watch_id, folder_name, config_revision,
                         inventory_id, inventory_payload, snapshot_id,
                         snapshot_payload, first_observed_at, stable_at,
                         discovery_id, status)
                    VALUES (%s, 'TerminalFence', %s, %s, '{}'::jsonb,
                            %s, '{}'::jsonb, %s, %s, %s, 'active')
                    """,
                    (
                        watch_id,
                        config.revision,
                        inventory_id,
                        snapshot_id,
                        now,
                        now,
                        discovery_id,
                    ),
                )
        scheduler = PostgresSchedulerRepository(control.pool)
        run_id = scheduler.register_run(discovery_id=discovery_id).run_id
        PostgresEventStore(control.pool, run_id=run_id).append(
            RunStarted(run_id, TmdbWorkType.ANIME, RunBudget())
        )
        with control.pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO plan_lineage
                    (run_id, version, plan_hash, plan_kind)
                VALUES (%s, 1, %s, 'initial')
                """,
                (run_id, plan_hash),
            )
        PostgresRunControlRepository(control.pool).handoff_effect(
            run_id=run_id,
            plan_hash=plan_hash,
            effect_kind=RunEffectKind.MEDIA_MOVE,
            policy=ApplyPolicy.MANUAL,
            event_sequence=1,
        )
        approval = ApprovalRecord.create(
            run_id=run_id,
            plan_hash=plan_hash,
            scope=ApprovalScope.APPLY,
            expires_at=now + timedelta(minutes=5),
            nonce=uuid.uuid4().hex,
        )
        PostgresApprovalStore(control.pool).issue(approval)
        repository = PostgresForwardOperationRepository(control.pool)
        operation = ExecutionOperation.authorized(
            operation_id=execution_operation_id(
                run_id=run_id, plan_hash=plan_hash
            ),
            run_id=run_id,
            plan_hash=plan_hash,
        )
        barrier = threading.Barrier(2)

        def terminalize() -> str:
            barrier.wait()
            try:
                scheduler.terminalize_run_failure(
                    run_id=run_id, failure_code="concurrent_failure"
                )
            except ServerError as error:
                assert error.code is ServerErrorCode.INTERACTION_CONFLICT
                return "lost"
            return "terminal"

        def authorize() -> str:
            barrier.wait()
            try:
                repository.authorize(
                    operation,
                    approval_id=approval.approval_id,
                    now=now,
                )
            except ForwardOperationError:
                return "lost"
            return "operation"

        with ThreadPoolExecutor(max_workers=2) as executor:
            terminal_future = executor.submit(terminalize)
            authorize_future = executor.submit(authorize)
            outcomes = {
                terminal_future.result(),
                authorize_future.result(),
            }
        assert outcomes == {"lost", "terminal"} or outcomes == {
            "lost",
            "operation",
        }
        with control.pool.connection() as connection:
            facts = connection.execute(
                """
                SELECT
                    EXISTS (
                        SELECT 1 FROM planning_terminal_results_v2
                        WHERE run_id = %s
                    ),
                    EXISTS (
                        SELECT 1 FROM execution_operations_v2
                        WHERE run_id = %s
                    )
                """,
                (run_id, run_id),
            ).fetchone()
        assert tuple(facts) in {(True, False), (False, True)}
        if bool(facts[0]):
            with pytest.raises(ServerError) as late_handoff:
                PostgresRunControlRepository(control.pool).handoff_effect(
                    run_id=run_id,
                    plan_hash=plan_hash,
                    effect_kind=RunEffectKind.MEDIA_MOVE,
                    policy=ApplyPolicy.MANUAL,
                    event_sequence=1,
                )
            assert late_handoff.value.code is (
                ServerErrorCode.INTERACTION_CONFLICT
            )
        # Do not leave a winning authorized operation claimable by later
        # repository tests sharing this PostgreSQL database.
        with control.pool.connection() as connection:
            connection.execute(
                """
                UPDATE execution_operations_v2
                SET status = 'superseded', outcomes = '[]'::jsonb,
                    lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = clock_timestamp()
                WHERE run_id = %s AND status IN ('authorized', 'running')
                """,
                (run_id,),
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
def test_expired_run_deadline_still_allows_one_interaction_winner() -> None:
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
                        datetime.now(UTC) - timedelta(minutes=5),
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
