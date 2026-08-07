from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reeloom.adapters.filesystem import (
    FilesystemPlanCompiler,
    FilesystemScanner,
)
from reeloom.adapters.journal import FilesystemJournalStore
from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.executor.apply import FilesystemExecutor
from reeloom.executor.apply import ApplyResult, ApplyStatus
from reeloom.executor.manifest import ExecutionManifest
from reeloom.executor.transaction import TransactionRecord
from reeloom.kernel.approval import ApprovalRecord, ApprovalScope
from reeloom.kernel.mapping import EpisodeCatalog, MappingDraft
from reeloom.kernel.naming import SeriesIdentity
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.runtime.budget import RunBudget
from reeloom.runtime.state import (
    Phase,
    RunState,
    RunStatus,
    StopReason,
)
from reeloom.runtime.state_codec import (
    STATE_PROJECTION_SCHEMA,
    canonical_state,
)
from reeloom.server.completed_layout import (
    PostgresCompletedLayoutRepository,
)
from reeloom.server.apply_service import ApplyCoordinator
from reeloom.server.approval_repository import PostgresApprovalStore
from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
    ServerWorkType,
    WatchConfig,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.database import PostgresControlPlane


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


def _append_config(
    control: PostgresControlPlane,
    *,
    watches: tuple[WatchConfig, ...] = (),
) -> ConfigRevision:
    repository = PostgresConfigRepository(control.pool)
    head = repository.head()
    expected = 0 if head is None else head.revision
    return repository.compare_and_append(
        expected_revision=expected,
        revision=ConfigRevision.create(
            revision_id=f"cfg-{uuid.uuid4().hex}",
            revision=expected + 1,
            created_at=datetime.now(UTC),
            draft=ConfigDraft(
                watches=watches,
                provider=ProviderConfig(
                    base_url="https://api.openai.com/v1",
                    model="test",
                    secret_ref="secret-test",
                ),
                apply_policy=ApplyPolicy.MANUAL,
            ),
        ),
    )


def _insert_claimed_run(
    control: PostgresControlPlane,
    *,
    config: ConfigRevision,
    run_id: str,
    plan_hash: str,
    approval: ApprovalRecord,
    status: str,
) -> None:
    watch_id = config.watches[0].watch_id if config.watches else (
        f"watch-{uuid.uuid4().hex}"
    )
    discovery_id = f"discovery-{uuid.uuid4().hex}"
    state = RunState(
        run_id=run_id,
        phase=Phase.AWAITING_APPROVAL,
        status=RunStatus.STOPPED,
        event_count=1,
        tool_calls=0,
        failures=0,
        pending_tool_calls=frozenset(),
        observed_tool_calls=frozenset(),
        work_type=TmdbWorkType.ANIME,
        budget=RunBudget(),
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        plan_hash=plan_hash,
        stop_reason=StopReason.AWAITING_APPROVAL,
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
                    f"snapshot-{uuid.uuid4().hex}",
                    datetime.now(UTC),
                ),
            )
            connection.execute(
                """
                INSERT INTO runs
                    (run_id, discovery_id, config_revision, work_type,
                     source_capability, status)
                VALUES (%s, %s, %s, 'anime', %s, %s)
                """,
                (
                    run_id,
                    discovery_id,
                    config.revision,
                    f"cap-{uuid.uuid4().hex}",
                    status,
                ),
            )
            connection.execute(
                """
                INSERT INTO jobs (job_id, run_id, status)
                VALUES (%s, %s, 'completed')
                """,
                (f"job-{uuid.uuid4().hex}", run_id),
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
                INSERT INTO run_states
                    (run_id, event_sequence, phase, runtime_status,
                     model_turns, model_tokens, tool_calls, failures,
                     plan_hash, deadline_at, projection_schema,
                     projection_payload)
                VALUES (
                    %s, 1, 'awaiting_approval', 'stopped',
                    0, 0, 0, 0, %s, %s, %s, %s::jsonb
                )
                """,
                (
                    run_id,
                    plan_hash,
                    state.deadline_at,
                    STATE_PROJECTION_SCHEMA,
                    canonical_state(state),
                ),
            )
            connection.execute(
                """
                INSERT INTO approvals
                    (approval_id, run_id, plan_hash, scope,
                     expires_at, canonical_record)
                VALUES (%s, %s, %s, 'apply', %s, %s)
                """,
                (
                    approval.approval_id,
                    run_id,
                    plan_hash,
                    approval.expires_at,
                    approval.canonical_bytes(),
                ),
            )
            connection.execute(
                """
                INSERT INTO approval_claims
                    (approval_id, run_id, plan_hash)
                VALUES (%s, %s, %s)
                """,
                (approval.approval_id, run_id, plan_hash),
            )


@pytest.mark.postgres
def test_completed_zero_move_settlement_has_no_layout_head() -> None:
    control = PostgresControlPlane(_dsn())
    run_id = f"run-{uuid.uuid4().hex}"
    plan_hash = "sha256:" + uuid.uuid4().hex * 2
    transaction_id = "txn-v1-" + uuid.uuid4().hex * 2
    try:
        control.open()
        control.migrate()
        config = _append_config(control)
        approval = ApprovalRecord.create(
            run_id=run_id,
            plan_hash=plan_hash,
            scope=ApprovalScope.APPLY,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            nonce=uuid.uuid4().hex,
        )
        _insert_claimed_run(
            control,
            config=config,
            run_id=run_id,
            plan_hash=plan_hash,
            approval=approval,
            status="awaiting_approval",
        )

        result = ApplyResult(
            transaction_id=transaction_id,
            plan_hash=plan_hash,
            approval_id=approval.approval_id,
            status=ApplyStatus.COMPLETED,
            applied_count=0,
            rolled_back_count=0,
            failure_code=None,
        )
        repository = PostgresCompletedLayoutRepository(control.pool)

        assert (
            repository.settle_and_append(result=result, layout=None)
            is None
        )
        assert repository.head(run_id) is None
        assert repository.settlement(
            run_id=run_id,
            plan_hash=plan_hash,
            approval_id=approval.approval_id,
        ) == result
        assert (
            repository.settle_and_append(result=result, layout=None)
            is None
        )

        with control.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT r.status, s.phase, s.runtime_status,
                       (SELECT count(*) FROM completed_layouts
                        WHERE run_id = %s),
                       (SELECT count(*) FROM completed_layout_heads
                        WHERE run_id = %s)
                FROM runs AS r
                JOIN run_states AS s USING (run_id)
                WHERE r.run_id = %s
                """,
                (run_id, run_id, run_id),
            ).fetchone()
        assert row is not None
        assert tuple(row) == ("completed", "completed", "stopped", 0, 0)
    finally:
        control.close()


@pytest.mark.postgres
def test_recover_failed_zero_move_run_settles_without_layout(
    tmp_path: Path,
) -> None:
    incoming = tmp_path / "incoming"
    library = tmp_path / "library"
    plan_root = tmp_path / "plans"
    journal_root = tmp_path / "journals"
    for root in (incoming, library, plan_root, journal_root):
        root.mkdir()
    source = incoming / "Folder" / "extra.mkv"
    source.parent.mkdir()
    source.write_bytes(b"unmapped")

    run_id = f"run-{uuid.uuid4().hex}"
    scan = FilesystemScanner().scan(AuthorizedRoot.create(incoming))
    plan = FilesystemPlanCompiler(
        scan=scan,
        output_root=AuthorizedRoot.create(library),
    ).compile(
        run_id=run_id,
        work_type=TmdbWorkType.ANIME,
        series=SeriesIdentity("Test", 2026, 1),
        mapping=MappingDraft.from_dict(
            {"videos": [], "subtitles": []},
            candidates=scan.snapshot.candidates,
            catalog=EpisodeCatalog.from_counts({1: 1}),
        ),
        subtitle_variants=(),
        created_at=datetime.now(UTC),
    )
    plans = FilesystemPlanStore(AuthorizedRoot.create(plan_root))
    plans.save(plan)
    manifest = ExecutionManifest.from_canonical_bytes(
        plan.canonical_bytes(),
        plan_hash=plan.plan_hash,
    )
    assert manifest.sources and not manifest.moves

    control = PostgresControlPlane(_dsn())
    try:
        control.open()
        control.migrate()
        watch_id = f"watch-{uuid.uuid4().hex}"
        config = _append_config(
            control,
            watches=(
                WatchConfig(
                    watch_id=watch_id,
                    root=incoming,
                    library_root=library,
                    work_type=ServerWorkType.ANIME,
                    poll_interval_seconds=1,
                    settle_interval_seconds=1,
                ),
            ),
        )
        approval = ApprovalRecord.create(
            run_id=run_id,
            plan_hash=plan.plan_hash,
            scope=ApprovalScope.APPLY,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            nonce=uuid.uuid4().hex,
        )
        _insert_claimed_run(
            control,
            config=config,
            run_id=run_id,
            plan_hash=plan.plan_hash,
            approval=approval,
            status="failed",
        )

        transaction = TransactionRecord.create(
            manifest,
            approval_id=approval.approval_id,
        )
        journals = FilesystemJournalStore(
            AuthorizedRoot.create(journal_root)
        )
        journals.begin(transaction)
        journals.record_completed(transaction)
        detached_incoming = tmp_path / "detached-incoming"
        detached_library = tmp_path / "detached-library"
        incoming.rename(detached_incoming)
        library.rename(detached_library)
        incoming.mkdir()
        library.mkdir()
        layouts = PostgresCompletedLayoutRepository(control.pool)
        coordinator = ApplyCoordinator(
            pool=control.pool,
            approvals=PostgresApprovalStore(control.pool),
            executor=FilesystemExecutor(
                plans=plans,
                approvals=PostgresApprovalStore(control.pool),
                journals=journals,
            ),
            completed_layouts=layouts,
        )

        recovered = coordinator.recover(
            run_id=run_id,
            plan_hash=plan.plan_hash,
            approval_id=approval.approval_id,
        )
        assert recovered == ApplyResult(
            transaction_id=transaction.transaction_id,
            plan_hash=plan.plan_hash,
            approval_id=approval.approval_id,
            status=ApplyStatus.COMPLETED,
            applied_count=0,
            rolled_back_count=0,
            failure_code=None,
        )
        assert coordinator.recover(
            run_id=run_id,
            plan_hash=plan.plan_hash,
            approval_id=approval.approval_id,
        ) == recovered
        assert layouts.head(run_id) is None
        assert (
            detached_incoming / "Folder" / "extra.mkv"
        ).read_bytes() == b"unmapped"
        assert not any(incoming.iterdir())
        assert not any(library.iterdir())

        with control.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT r.status, s.phase,
                       (SELECT count(*) FROM completed_layouts
                        WHERE run_id = %s),
                       (SELECT count(*) FROM run_operations
                        WHERE run_id = %s)
                FROM runs AS r
                JOIN run_states AS s USING (run_id)
                WHERE r.run_id = %s
                """,
                (run_id, run_id, run_id),
            ).fetchone()
        assert row is not None
        assert tuple(row) == ("completed", "completed", 0, 0)
    finally:
        control.close()
