from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reeloom.adapters.plan_store import FilesystemPlanStore
from reeloom.kernel.tmdb import TmdbWorkType
from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.runtime.events import RunFailed, RunStarted
from reeloom.runtime.state import RunStatus
from reeloom.server.agent_definition import AgentDefinitionRevision
from reeloom.server.agent_repository import (
    PostgresAgentDefinitionRepository,
)
from reeloom.server.composition import _retire_unplanned_folder_runs
from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
    ServerWorkType,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.database import PostgresControlPlane
from reeloom.server.organizer_definition import (
    EPISODE_ORGANIZER_TOOL_NAMES,
    LEGACY_EPISODE_ORGANIZER_TOOL_NAMES,
    LEGACY_ORGANIZER_SCHEMA_VERSION,
    ORGANIZER_NAME,
    ORGANIZER_SCHEMA_VERSION,
    PREVIOUS_ORGANIZER_SCHEMA_VERSION,
    V2_ORGANIZER_SCHEMA_VERSION,
    organizer_definition,
)
from reeloom.server.runtime_store import PostgresEventStore
from reeloom.server.scheduler import Discovery, RunRegistration
from reeloom.server.scheduler_repository import (
    PostgresSchedulerRepository,
)
from reeloom.server.watcher import NoFollowWatcher


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


@dataclass(frozen=True, slots=True)
class _FolderRun:
    control: PostgresControlPlane
    scheduler: PostgresSchedulerRepository
    config: ConfigRevision
    watch_id: str
    watch_root: Path
    source: Path
    plans: FilesystemPlanStore
    watcher: NoFollowWatcher
    discovery: Discovery
    registration: RunRegistration
    store: PostgresEventStore


def _folder_run(
    tmp_path: Path,
    *,
    schema_version: str = PREVIOUS_ORGANIZER_SCHEMA_VERSION,
    tools: tuple[str, ...] = EPISODE_ORGANIZER_TOOL_NAMES,
) -> _FolderRun:
    control = PostgresControlPlane(_dsn())
    control.open()
    control.migrate()
    configs = PostgresConfigRepository(control.pool)
    head = configs.head()
    expected = 0 if head is None else head.revision
    config = ConfigRevision.create(
        revision_id=f"cfg-{uuid.uuid4().hex}",
        revision=expected + 1,
        created_at=datetime.now(UTC),
        draft=ConfigDraft(
            watches=(),
            provider=ProviderConfig(
                base_url="https://api.openai.com/v1",
                model="test",
                secret_ref="secret-test",
            ),
            apply_policy=ApplyPolicy.MANUAL,
        ),
    )
    configs.compare_and_append(
        expected_revision=expected,
        revision=config,
    )
    scheduler = PostgresSchedulerRepository(control.pool)
    watch_id = f"watch-{uuid.uuid4().hex}"
    scheduler.configure_watch(
        watch_id=watch_id,
        config_revision=config.revision,
        fence=config.revision,
        work_type=ServerWorkType.ANIME,
        settle_interval_seconds=1,
    )
    watch_root = tmp_path / "watch"
    source = watch_root / "Incoming" / "Episode S01E01.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    watcher = NoFollowWatcher()
    scan = watcher.scan_folders(AuthorizedRoot.create(watch_root))
    started = datetime.now(UTC)
    scheduler.reconcile_folders(
        watch_id=watch_id,
        config_revision=config.revision,
        fence=config.revision,
        observed_at=started,
        scan=scan,
    )
    discovery = scheduler.reconcile_folders(
        watch_id=watch_id,
        config_revision=config.revision,
        fence=config.revision,
        observed_at=started + timedelta(seconds=1),
        scan=scan,
    ).discoveries[0]
    registration = scheduler.register_run(
        discovery_id=discovery.discovery_id
    )
    PostgresAgentDefinitionRepository(control.pool).register_and_bind(
        run_id=registration.run_id,
        definition=AgentDefinitionRevision.create(
            name=ORGANIZER_NAME,
            instructions="Historical organizer.",
            tools=tools,
            schema_version=schema_version,
        ),
        session_id=registration.run_id,
    )
    plan_root = tmp_path / "plans"
    plan_root.mkdir()
    plans = FilesystemPlanStore(AuthorizedRoot.create(plan_root))
    store = PostgresEventStore(
        control.pool,
        run_id=registration.run_id,
        plans=plans,
    )
    store.append(RunStarted(registration.run_id, TmdbWorkType.ANIME))
    return _FolderRun(
        control=control,
        scheduler=scheduler,
        config=config,
        watch_id=watch_id,
        watch_root=watch_root,
        source=source,
        plans=plans,
        watcher=watcher,
        discovery=discovery,
        registration=registration,
        store=store,
    )


def _fail_unplanned_run(
    case: _FolderRun,
    *,
    runtime_failure: str | None = None,
) -> None:
    if runtime_failure is not None:
        case.store.append(RunFailed(code=runtime_failure))
    with case.control.pool.connection() as connection:
        with connection.transaction():
            connection.execute(
                """
                UPDATE jobs
                SET status = 'completed', boot_id = NULL
                WHERE job_id = %s
                """,
                (case.registration.job_id,),
            )
            if runtime_failure is None:
                connection.execute(
                    "UPDATE runs SET status = 'failed' WHERE run_id = %s",
                    (case.registration.run_id,),
                )


def _add_retirement_blocker(case: _FolderRun, blocker: str) -> None:
    values = {
        "run_id": case.registration.run_id,
        "plan_hash": f"plan-{uuid.uuid4().hex}",
        "token": uuid.uuid4().hex,
        "generation_id": case.discovery.folder_generation_id,
        "inventory_id": case.discovery.inventory_id,
    }
    statements = {
        "operation": """
            INSERT INTO run_operations
                (run_id, operation_id, operation_kind)
            VALUES (%(run_id)s, %(token)s, 'question')
        """,
        "plan": """
            INSERT INTO plan_heads (run_id, version, plan_hash)
            VALUES (%(run_id)s, 1, %(plan_hash)s)
        """,
        "layout": """
            WITH layout AS (
                INSERT INTO completed_layouts
                    (run_id, version, plan_hash,
                     transaction_id, layout_payload)
                VALUES (
                    %(run_id)s, 1, %(plan_hash)s,
                    %(token)s, '{}'::jsonb
                )
                RETURNING run_id, version, plan_hash
            )
            INSERT INTO completed_layout_heads
                (run_id, version, plan_hash)
            SELECT run_id, version, plan_hash FROM layout
        """,
        "disposition": """
            INSERT INTO folder_disposition_plans
                (plan_hash, run_id, folder_generation_id,
                 action, source_root_device, source_root_inode,
                 inventory_id, file_count, reason_code,
                 canonical_record)
            VALUES (
                %(plan_hash)s, %(run_id)s, %(generation_id)s,
                'remove_empty', 0, 0, %(inventory_id)s, 0,
                'test', '{}'::bytea
            )
        """,
        "approval": """
            WITH approval AS (
                INSERT INTO approvals
                    (approval_id, run_id, plan_hash, scope,
                     expires_at, canonical_record)
                VALUES (
                    %(token)s, %(run_id)s, %(plan_hash)s, 'apply',
                    clock_timestamp() + interval '1 hour',
                    '{}'::bytea
                )
                RETURNING approval_id, run_id, plan_hash
            )
            INSERT INTO approval_claims
                (approval_id, run_id, plan_hash)
            SELECT approval_id, run_id, plan_hash FROM approval
        """,
    }
    with case.control.pool.connection() as connection:
        with connection.transaction():
            if blocker in {"plan", "layout", "approval"}:
                connection.execute(
                    """
                    INSERT INTO plan_lineage
                        (run_id, version, plan_hash, plan_kind)
                    VALUES (%(run_id)s, 1, %(plan_hash)s, 'initial')
                    """,
                    values,
                )
            connection.execute(statements[blocker], values)


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("schema_version", "tools", "failure_code"),
    (
        (
            LEGACY_ORGANIZER_SCHEMA_VERSION,
            LEGACY_EPISODE_ORGANIZER_TOOL_NAMES,
            "retired_tool_call",
        ),
        (
            V2_ORGANIZER_SCHEMA_VERSION,
            EPISODE_ORGANIZER_TOOL_NAMES,
            "retired_agent_definition",
        ),
        (
            PREVIOUS_ORGANIZER_SCHEMA_VERSION,
            EPISODE_ORGANIZER_TOOL_NAMES,
            "retired_invalid_tool_schema",
        ),
    ),
)
def test_retired_folder_run_restarts_without_source_side_effect(
    tmp_path: Path,
    schema_version: str,
    tools: tuple[str, ...],
    failure_code: str,
) -> None:
    case = _folder_run(
        tmp_path,
        schema_version=schema_version,
        tools=tools,
    )
    control = case.control
    scheduler = case.scheduler
    config = case.config
    watch_id = case.watch_id
    watch_root = case.watch_root
    source = case.source
    plans = case.plans
    watcher = case.watcher
    first = case.discovery
    registration = case.registration
    try:
        prior_failure = failure_code == "retired_invalid_tool_schema"
        _fail_unplanned_run(
            case,
            runtime_failure="agent_run_failed" if prior_failure else None,
        )

        assert scheduler.retired_unplanned_folder_runs(
            schema_versions=(schema_version,)
        ) == (registration.run_id,)
        _retire_unplanned_folder_runs(
            database=control,
            plans=plans,
            scheduler=scheduler,
        )
        _retire_unplanned_folder_runs(
            database=control,
            plans=plans,
            scheduler=scheduler,
        )

        retired = PostgresEventStore(
            control.pool,
            run_id=registration.run_id,
            plans=plans,
        ).state
        assert retired is not None
        assert retired.status is RunStatus.FAILED
        assert retired.failure_code == (
            "agent_run_failed" if prior_failure else failure_code
        )
        assert source.read_bytes() == b"video"
        with control.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT job.status, observed.status,
                       observed.discovery_id
                FROM jobs AS job
                JOIN runs AS run ON run.run_id = job.run_id
                JOIN discoveries AS discovery
                  ON discovery.discovery_id = run.discovery_id
                JOIN watch_folder_observations AS observed
                  ON observed.watch_id = discovery.watch_id
                 AND observed.folder_name = discovery.source_folder
                WHERE run.run_id = %s
                """,
                (registration.run_id,),
            ).fetchone()
            audit_count = connection.execute(
                """
                SELECT count(*)
                FROM scheduler_audit
                WHERE event_type = %s AND subject_id = %s
                """,
                (failure_code, registration.run_id),
            ).fetchone()[0]
        assert tuple(row) == ("failed", "settling", None)
        assert audit_count == 1

        restarted_at = datetime.now(UTC) + timedelta(seconds=2)
        restarted = scheduler.reconcile_folders(
            watch_id=watch_id,
            config_revision=config.revision,
            fence=config.revision,
            observed_at=restarted_at,
            scan=watcher.scan_folders(
                AuthorizedRoot.create(watch_root)
            ),
        )
        assert len(restarted.discoveries) == 1
        assert (
            restarted.discoveries[0].folder_generation_id
            != first.folder_generation_id
        )
        new_registration = scheduler.register_run(
            discovery_id=restarted.discoveries[0].discovery_id
        )
        assert (
            scheduler.register_run(
                discovery_id=restarted.discoveries[0].discovery_id
            ).run_id
            == new_registration.run_id
        )
        definitions = PostgresAgentDefinitionRepository(control.pool)
        current = organizer_definition(TmdbWorkType.ANIME)
        definitions.register_and_bind(
            run_id=new_registration.run_id,
            definition=current,
            session_id=new_registration.run_id,
        )
        bound, _ = definitions.load_bound(run_id=new_registration.run_id)
        assert bound.schema_version == ORGANIZER_SCHEMA_VERSION
        assert (
            scheduler.reconcile_folders(
                watch_id=watch_id,
                config_revision=config.revision,
                fence=config.revision,
                observed_at=restarted_at + timedelta(seconds=1),
                scan=watcher.scan_folders(
                    AuthorizedRoot.create(watch_root)
                ),
            ).discoveries
            == ()
        )
        assert source.read_bytes() == b"video"
    finally:
        control.close()


@pytest.mark.postgres
@pytest.mark.parametrize(
    "blocker",
    ("operation", "plan", "layout", "disposition", "approval"),
)
def test_retired_folder_run_preserves_durable_work(
    tmp_path: Path,
    blocker: str,
) -> None:
    case = _folder_run(tmp_path)
    _fail_unplanned_run(case)
    try:
        _add_retirement_blocker(case, blocker)
        assert case.scheduler.retired_unplanned_folder_runs(
            schema_versions=(PREVIOUS_ORGANIZER_SCHEMA_VERSION,)
        ) == ()
    finally:
        case.control.close()
