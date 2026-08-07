from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

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

        assert repository.authorize(
            operation, approval_id=approval.approval_id, now=now
        ) == operation
        lease = repository.claim(
            operation.operation_id,
            worker_id=f"worker-{suffix}",
            now=now,
            lease_for=timedelta(seconds=30),
        )
        assert lease is not None
        settled = repository.settle(
            lease,
            (
                ExecutionItemOutcome.SATISFIED,
                ExecutionItemOutcome.COLLISION,
            ),
            now=now + timedelta(seconds=1),
        )

        assert settled.status is ExecutionOperationStatus.PARTIAL
        assert repository.get(operation.operation_id) == settled
        assert repository.claim(
            operation.operation_id,
            worker_id=f"worker-{suffix}",
            now=now + timedelta(seconds=2),
            lease_for=timedelta(seconds=30),
        ) is None
    finally:
        control.close()
