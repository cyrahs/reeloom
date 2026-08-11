from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from reeloom.executor.forward import (
    ForwardExecutionItemResult,
    ForwardExecutionResult,
)
from reeloom.executor.subtitle_publication import (
    SubtitlePublicationResult,
    SubtitlePublicationState,
)
from reeloom.kernel.candidates import CandidateId, CandidateKind
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.forward_execution import (
    ExecutionItemOutcome,
    ExecutionOperation,
    ExecutionOperationStatus,
)
from reeloom.runtime.budget import RunBudget
from reeloom.server.approval_repository import PostgresApprovalStore
from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.database import PostgresControlPlane
from reeloom.server.forward_operation_repository import (
    ForwardOperationError,
    ForwardOperationErrorCode,
    PostgresForwardOperationRepository,
    execution_operation_id,
    _subtitle_failure_diagnostic,
)


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


def test_execution_operation_id_is_deterministic_and_plan_bound() -> None:
    first = execution_operation_id(
        run_id="run:1", plan_hash="sha256:" + "a" * 64
    )

    assert first == execution_operation_id(
        run_id="run:1", plan_hash="sha256:" + "a" * 64
    )
    assert first != execution_operation_id(
        run_id="run:1", plan_hash="sha256:" + "b" * 64
    )
    assert first.startswith("execution-operation-v2-")


def test_execution_operation_id_rejects_untyped_binding() -> None:
    with pytest.raises(ForwardOperationError) as raised:
        execution_operation_id(
            run_id=1,  # type: ignore[arg-type]
            plan_hash="sha256:" + "a" * 64,
        )

    assert (
        raised.value.code
        is ForwardOperationErrorCode.INVALID_OPERATION
    )


def test_subtitle_failure_diagnostic_preserves_bounded_publication_reason() -> None:
    assert _subtitle_failure_diagnostic(
        SubtitlePublicationResult(
            state=SubtitlePublicationState.COLLISION,
            publication_directory="reeloom-acquired-test",
            published_count=0,
            reason="casefold_collision",
        )
    ) == {
        "schema_version": 2,
        "stage": "publication",
        "reason": "casefold_collision",
    }


def test_subtitle_failure_diagnostic_maps_untrusted_reason_to_state() -> None:
    assert _subtitle_failure_diagnostic(
        SubtitlePublicationResult(
            state=SubtitlePublicationState.UNSAFE,
            publication_directory="reeloom-acquired-test",
            published_count=0,
            reason="unexpected adapter detail",
        )
    ) == {
        "schema_version": 2,
        "stage": "publication",
        "reason": "unsafe",
    }


@pytest.mark.postgres
def test_postgres_operation_ledger_authorizes_leases_and_settles() -> None:
    control = PostgresControlPlane(_dsn())
    suffix = uuid.uuid4().hex
    now = datetime.now(UTC)
    try:
        control.open()
        control.migrate()
        configs = PostgresConfigRepository(control.pool)
        config = configs.head()
        if config is None:
            config = configs.compare_and_append(
                expected_revision=0,
                revision=ConfigRevision.create(
                    revision_id=f"cfg-{suffix}",
                    revision=1,
                    created_at=now,
                    draft=ConfigDraft(
                        watches=(),
                        provider=ProviderConfig(
                            base_url="https://api.openai.com/v1",
                            model="gpt-test",
                            secret_ref="secret-test",
                        ),
                        apply_policy=ApplyPolicy.MANUAL,
                        agent_budget=RunBudget(),
                    ),
                ),
            )
        watch_id = f"watch-{suffix}"
        discovery_id = f"discovery-{suffix}"
        run_id = f"run-{suffix}"
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
                    VALUES (%s, %s, %s, %s, '{}'::jsonb, 'anime', %s)
                    """,
                    (
                        discovery_id,
                        watch_id,
                        config.revision,
                        f"snapshot-{suffix}",
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
                    (
                        run_id,
                        discovery_id,
                        config.revision,
                        f"capability-{suffix}",
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
                    INSERT INTO run_lifecycle_controls_v2
                        (run_id, mode, classification_reason,
                         revision, effect_kind, effect_plan_hash,
                         effect_policy, handoff_event_sequence)
                    VALUES (%s, 'forward_v2', 'test_fixture', 1,
                            'media_move', %s, 'automatic', 1)
                    """,
                    (run_id, plan_hash),
                )
        approval = ApprovalRecord.create(
            run_id=run_id,
            plan_hash=plan_hash,
            scope=ApprovalScope.APPLY,
            expires_at=now + timedelta(minutes=10),
            nonce=uuid.uuid4().hex,
        )
        PostgresApprovalStore(control.pool).issue(approval)
        operation = ExecutionOperation.authorized(
            operation_id=execution_operation_id(
                run_id=run_id, plan_hash=plan_hash
            ),
            run_id=run_id,
            plan_hash=plan_hash,
        )
        repository = PostgresForwardOperationRepository(control.pool)

        assert repository.find_unstarted_automatic(
            operation_kind="media_move"
        ) == (run_id, plan_hash)

        assert repository.authorize(
            operation, approval_id=approval.approval_id, now=now
        ) == operation
        assert repository.find_unstarted_automatic(
            operation_kind="media_move"
        ) is None
        lease = repository.claim(
            operation.operation_id,
            worker_id=f"worker-{suffix}",
            now=now,
            lease_for=timedelta(seconds=30),
        )
        assert lease is not None
        original_lease = lease
        lease = repository.renew_lease(
            lease,
            now=now + timedelta(seconds=10),
            lease_for=timedelta(seconds=30),
        )
        assert lease.expires_at == now + timedelta(seconds=40)
        with pytest.raises(ForwardOperationError) as stale_renewal:
            repository.renew_lease(
                original_lease,
                now=now + timedelta(seconds=11),
                lease_for=timedelta(seconds=30),
            )
        assert stale_renewal.value.code is (
            ForwardOperationErrorCode.LEASE_CONFLICT
        )
        assert repository.claim(
            operation.operation_id,
            worker_id=f"competing-worker-{suffix}",
            now=now + timedelta(seconds=31),
            lease_for=timedelta(seconds=30),
        ) is None
        terminal = lease.settle(
            (
                ExecutionItemOutcome.SATISFIED,
                ExecutionItemOutcome.COLLISION,
            ),
            now=now + timedelta(seconds=11),
        )
        result = ForwardExecutionResult(
            operation=terminal,
            items=(
                ForwardExecutionItemResult(
                    CandidateId(CandidateKind.VIDEO, 1),
                    ExecutionItemOutcome.SATISFIED,
                ),
                ForwardExecutionItemResult(
                    CandidateId(CandidateKind.VIDEO, 2),
                    ExecutionItemOutcome.COLLISION,
                ),
            ),
            warnings=("directory_fsync_unsupported",),
            fresh_scan_required=True,
        )
        settled = repository.settle_result(
            lease, result, now=now + timedelta(seconds=11)
        )

        assert settled.status is ExecutionOperationStatus.PARTIAL
        assert repository.get(operation.operation_id) == settled
        view = repository.get_view(operation.operation_id)
        assert view.operation == settled
        assert tuple(item["outcome"] for item in view.items) == (
            "satisfied",
            "collision",
        )
        assert view.warnings == ("directory_fsync_unsupported",)
        assert view.fresh_scan_required
        assert view.rescan_state is None
        rescan = repository.claim_rescan(
            worker_id=f"worker-{suffix}",
            now=now + timedelta(seconds=2),
            lease_for=timedelta(seconds=30),
            operation_id=operation.operation_id,
        )
        assert rescan is None
        # This component fixture has no semantic source folder.  M14.6 never
        # fabricates a generation from a flat/legacy discovery.
        with pytest.raises(ForwardOperationError) as unavailable_rescan:
            repository.requeue_rescan(
                run_id=run_id,
                plan_hash=plan_hash,
                now=now + timedelta(seconds=4),
            )
        assert unavailable_rescan.value.code is (
            ForwardOperationErrorCode.OPERATION_CONFLICT
        )
        assert repository.get_view(operation.operation_id).rescan_state is None
        assert repository.claim(
            operation.operation_id,
            worker_id=f"worker-{suffix}",
            now=now + timedelta(seconds=2),
            lease_for=timedelta(seconds=30),
        ) is None

        legacy_watch_id = f"legacy-watch-{suffix}"
        legacy_discovery_id = f"legacy-discovery-{suffix}"
        legacy_run_id = f"legacy-run-{suffix}"
        legacy_plan_hash = "sha256:" + uuid.uuid4().hex * 2
        legacy_approval = ApprovalRecord.create(
            run_id=legacy_run_id,
            plan_hash=legacy_plan_hash,
            scope=ApprovalScope.APPLY,
            expires_at=now + timedelta(minutes=10),
            nonce=uuid.uuid4().hex,
        )
        legacy_operation_id = execution_operation_id(
            run_id=legacy_run_id,
            plan_hash=legacy_plan_hash,
        )
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO watch_states
                        (watch_id, config_revision, fence, work_type,
                         settle_interval_seconds)
                    VALUES (%s, %s, %s, 'anime', 1)
                    """,
                    (
                        legacy_watch_id,
                        config.revision,
                        config.revision,
                    ),
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
                        legacy_discovery_id,
                        legacy_watch_id,
                        config.revision,
                        f"legacy-snapshot-{suffix}",
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO runs
                        (run_id, discovery_id, config_revision, work_type,
                         source_capability, status)
                    VALUES (%s, %s, %s, 'anime', %s, 'superseded')
                    """,
                    (
                        legacy_run_id,
                        legacy_discovery_id,
                        config.revision,
                        f"legacy-capability-{suffix}",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO plan_lineage
                        (run_id, version, plan_hash, plan_kind)
                    VALUES (%s, 1, %s, 'initial')
                    """,
                    (legacy_run_id, legacy_plan_hash),
                )
                connection.execute(
                    """
                    INSERT INTO run_lifecycle_controls_v2
                        (run_id, mode, classification_reason)
                    VALUES (%s, 'legacy_read_only', 'test_quarantine')
                    """,
                    (legacy_run_id,),
                )
        PostgresApprovalStore(control.pool).issue(legacy_approval)
        with control.pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO execution_operations_v2
                    (operation_id, schema_version, run_id, plan_hash,
                     approval_id, operation_kind, status)
                VALUES (%s, 2, %s, %s, %s, 'media_move', 'authorized')
                """,
                (
                    legacy_operation_id,
                    legacy_run_id,
                    legacy_plan_hash,
                    legacy_approval.approval_id,
                ),
            )

        assert repository.claim(
            legacy_operation_id,
            worker_id=f"worker-{suffix}",
            now=now + timedelta(seconds=5),
            lease_for=timedelta(seconds=30),
        ) is None
    finally:
        control.close()


@pytest.mark.postgres
def test_generation_conflict_does_not_roll_back_media_settlement() -> None:
    control = PostgresControlPlane(_dsn())
    suffix = uuid.uuid4().hex
    now = datetime.now(UTC)
    try:
        control.open()
        control.migrate()
        configs = PostgresConfigRepository(control.pool)
        config = configs.head()
        assert config is not None
        watch_id = f"watch-generation-{suffix}"
        discovery_id = f"discovery-generation-{suffix}"
        run_id = f"run-generation-{suffix}"
        plan_hash = "sha256:" + uuid.uuid4().hex * 2
        operation_id = execution_operation_id(
            run_id=run_id, plan_hash=plan_hash
        )
        source_folder = f"Generation{suffix[:12]}"
        inventory_id = "folder-inventory-v2:" + uuid.uuid4().hex * 2
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
                            %s, %s, %s)
                    """,
                    (
                        discovery_id,
                        watch_id,
                        config.revision,
                        "candidate-snapshot-v2:" + uuid.uuid4().hex * 2,
                        now,
                        source_folder,
                        "folder-generation-v2:" + uuid.uuid4().hex * 2,
                        inventory_id,
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
                    VALUES (%s, 'forward_v2', 'test_fixture', 1,
                            'media_move', %s, 'manual', 1)
                    """,
                    (run_id, plan_hash),
                )
                connection.execute(
                    """
                    INSERT INTO generation_requests_v2
                        (request_id, request_kind, origin_run_id, watch_id,
                         source_folder, expected_inventory_id,
                         generation_nonce)
                    VALUES (%s, 'legacy_handoff', %s, %s, %s, %s, %s)
                    """,
                    (
                        f"generation-active-{suffix}",
                        run_id,
                        watch_id,
                        source_folder,
                        inventory_id,
                        f"generation-active-nonce-{suffix}",
                    ),
                )
        approval = ApprovalRecord.create(
            run_id=run_id,
            plan_hash=plan_hash,
            scope=ApprovalScope.APPLY,
            expires_at=now + timedelta(minutes=10),
            nonce=uuid.uuid4().hex,
        )
        PostgresApprovalStore(control.pool).issue(approval)
        repository = PostgresForwardOperationRepository(control.pool)
        operation = repository.authorize(
            ExecutionOperation.authorized(
                operation_id=operation_id,
                run_id=run_id,
                plan_hash=plan_hash,
            ),
            approval_id=approval.approval_id,
            now=now,
        )
        lease = repository.claim(
            operation.operation_id,
            worker_id=f"worker-{suffix}",
            now=now,
            lease_for=timedelta(seconds=30),
        )
        assert lease is not None
        terminal = lease.settle(
            (ExecutionItemOutcome.STALE,),
            now=now + timedelta(seconds=1),
        )
        settled = repository.settle_result(
            lease,
            ForwardExecutionResult(
                operation=terminal,
                items=(
                    ForwardExecutionItemResult(
                        CandidateId(CandidateKind.VIDEO, 1),
                        ExecutionItemOutcome.STALE,
                    ),
                ),
                warnings=(),
                fresh_scan_required=True,
            ),
            now=now + timedelta(seconds=1),
        )

        assert settled.status is ExecutionOperationStatus.STALE
        assert repository.get_view(operation_id).rescan_state == "blocked"
        with control.pool.connection() as connection:
            assert connection.execute(
                """
                SELECT warning FROM generation_requests_v2
                WHERE operation_id = %s
                """,
                (operation_id,),
            ).fetchone() == ("active_generation_conflict",)
            assert connection.execute(
                """
                SELECT terminal_status FROM handled_folder_inventories_v2
                WHERE operation_id = %s
                """,
                (operation_id,),
            ).fetchone() == ("stale",)
            connection.execute(
                """
                UPDATE generation_requests_v2
                SET state = 'blocked', warning = 'test_owner_released'
                WHERE request_id = %s
                """,
                (f"generation-active-{suffix}",),
            )
        repository.requeue_rescan(
            run_id=run_id,
            plan_hash=plan_hash,
            now=now + timedelta(seconds=2),
        )
        assert repository.get_view(operation_id).rescan_state == "queued"
        with control.pool.connection() as connection:
            request_id = str(
                connection.execute(
                    """
                    SELECT request_id FROM generation_requests_v2
                    WHERE operation_id = %s
                    """,
                    (operation_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                UPDATE generation_requests_v2
                SET state = 'accepted', attempt_count = 1,
                    accepted_at = %s, available_at = %s
                WHERE operation_id = %s
                """,
                (now, now, operation_id),
            )
            connection.execute(
                """
                UPDATE generation_requests_v2
                SET available_at = %s,
                    accepted_at = CASE
                        WHEN state = 'accepted' THEN %s
                        ELSE accepted_at
                    END
                WHERE request_id <> %s
                  AND state IN ('queued', 'leased', 'accepted')
                """,
                (
                    now + timedelta(days=1),
                    now + timedelta(days=1),
                    request_id,
                ),
            )
        reclaimed = repository.claim_generation_request(
            worker_id=f"generation-worker-{suffix}",
            now=now + timedelta(minutes=11),
            lease_for=timedelta(minutes=1),
        )
        assert reclaimed is not None
        assert reclaimed.request_id == request_id
        assert reclaimed.attempt_count == 2
        with control.pool.connection() as connection:
            assert connection.execute(
                """
                SELECT state, accepted_at
                FROM generation_requests_v2
                WHERE operation_id = %s
                """,
                (operation_id,),
            ).fetchone() == ("leased", None)
            connection.execute(
                """
                UPDATE generation_requests_v2
                SET state = 'accepted', attempt_count = 5,
                    accepted_at = %s, warning = NULL,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE operation_id = %s
                """,
                (now + timedelta(minutes=11), operation_id),
            )
        repository.claim_generation_request(
            worker_id=f"generation-reaper-{suffix}",
            now=now + timedelta(minutes=22),
            lease_for=timedelta(minutes=1),
        )
        with control.pool.connection() as connection:
            assert connection.execute(
                """
                SELECT state, warning
                FROM generation_requests_v2
                WHERE operation_id = %s
                """,
                (operation_id,),
            ).fetchone() == ("blocked", "accepted_timeout")
    finally:
        control.close()


@pytest.mark.postgres
def test_media_lease_exhaustion_atomically_terminalizes_all_projections() -> None:
    control = PostgresControlPlane(_dsn())
    suffix = uuid.uuid4().hex
    now = datetime.now(UTC)
    run_id = f"run-exhausted-{suffix}"
    watch_id = f"watch-exhausted-{suffix}"
    discovery_id = f"discovery-exhausted-{suffix}"
    plan_hash = "sha256:" + uuid.uuid4().hex * 2
    operation_id = execution_operation_id(
        run_id=run_id, plan_hash=plan_hash
    )
    try:
        control.open()
        control.migrate()
        configs = PostgresConfigRepository(control.pool)
        config = configs.head()
        assert config is not None
        approval = ApprovalRecord.create(
            run_id=run_id,
            plan_hash=plan_hash,
            scope=ApprovalScope.APPLY,
            expires_at=now + timedelta(minutes=10),
            nonce=uuid.uuid4().hex,
        )
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
                            %s, %s, %s)
                    """,
                    (
                        discovery_id,
                        watch_id,
                        config.revision,
                        "candidate-snapshot-v2:" + uuid.uuid4().hex * 2,
                        now,
                        f"Exhausted{suffix[:12]}",
                        "folder-generation-v2:" + uuid.uuid4().hex * 2,
                        "folder-inventory-v2:" + uuid.uuid4().hex * 2,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO runs
                        (run_id, discovery_id, config_revision, work_type,
                         source_capability, status)
                    VALUES (%s, %s, %s, 'anime', %s, 'applying')
                    """,
                    (run_id, discovery_id, config.revision, f"source-{suffix}"),
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
                    VALUES (%s, 'forward_v2', 'test_exhaustion', 1,
                            'media_move', %s, 'automatic', 1)
                    """,
                    (run_id, plan_hash),
                )
        PostgresApprovalStore(control.pool).issue(approval)
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO execution_operations_v2
                        (operation_id, schema_version, run_id, plan_hash,
                         approval_id, operation_kind, status, attempt_count,
                         lease_owner, lease_expires_at)
                    VALUES (%s, 2, %s, %s, %s, 'media_move', 'running',
                            100, 'expired-worker', %s)
                    """,
                    (
                        operation_id,
                        run_id,
                        plan_hash,
                        approval.approval_id,
                        now - timedelta(seconds=1),
                    ),
                )
                connection.execute(
                    """
                    UPDATE run_lifecycle_controls_v2
                    SET operation_id = %s, revision = revision + 1,
                        updated_at = %s
                    WHERE run_id = %s
                    """,
                    (operation_id, now, run_id),
                )
        repository = PostgresForwardOperationRepository(control.pool)

        assert repository.claim(
            operation_id,
            worker_id=f"worker-{suffix}",
            now=now,
            lease_for=timedelta(seconds=30),
        ) is None
        view = repository.get_view(operation_id)
        assert view.operation.status is ExecutionOperationStatus.UNAVAILABLE
        assert view.operation.outcomes == (ExecutionItemOutcome.UNAVAILABLE,)
        assert view.warnings == ("retry_exhausted",)
        assert view.rescan_state == "queued"
        with control.pool.connection() as connection:
            assert connection.execute(
                "SELECT status FROM runs WHERE run_id = %s", (run_id,)
            ).fetchone() == ("failed",)
            assert connection.execute(
                """
                SELECT terminal_status
                FROM handled_folder_inventories_v2
                WHERE run_id = %s AND operation_id = %s
                """,
                (run_id, operation_id),
            ).fetchone() == ("unavailable",)
            assert connection.execute(
                """
                SELECT count(*) FROM execution_operation_results_v2
                WHERE operation_id = %s
                """,
                (operation_id,),
            ).fetchone() == (1,)
    finally:
        control.close()
