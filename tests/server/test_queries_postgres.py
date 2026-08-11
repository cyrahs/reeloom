from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest

from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.api_models import RunResponse
from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
)
from reeloom.server.database import PostgresControlPlane
from reeloom.server.queries import PostgresQueries
from reeloom.runtime.budget import RunBudget


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


def _ensure_config(
    control: PostgresControlPlane,
    *,
    suffix: str,
    now: datetime,
) -> ConfigRevision:
    configs = PostgresConfigRepository(control.pool)
    config = configs.head()
    if config is not None:
        return config
    return configs.compare_and_append(
        expected_revision=0,
        revision=ConfigRevision.create(
            revision_id=f"config-query-{suffix}",
            revision=1,
            created_at=now,
            draft=ConfigDraft(
                watches=(),
                provider=ProviderConfig(
                    base_url="https://api.openai.com/v1",
                    model="test",
                    secret_ref="secret-test",
                ),
                apply_policy=ApplyPolicy.MANUAL,
                agent_budget=RunBudget(),
            ),
        ),
    )


@pytest.mark.postgres
def test_run_and_discovery_pages_order_by_creation_time() -> None:
    control = PostgresControlPlane(_dsn())
    suffix = uuid.uuid4().hex
    watch_id = f"watch-order-{suffix}"
    discovery_ids = (
        f"discovery-z-old-{suffix}",
        f"discovery-m-middle-{suffix}",
        f"discovery-a-new-{suffix}",
    )
    run_ids = (
        f"run-z-old-{suffix}",
        f"run-m-middle-{suffix}",
        f"run-a-new-{suffix}",
    )
    moments = (
        datetime(2099, 1, 1, tzinfo=UTC),
        datetime(2099, 1, 2, tzinfo=UTC),
        datetime(2099, 1, 3, tzinfo=UTC),
    )
    try:
        control.open()
        control.migrate()
        config = _ensure_config(
            control, suffix=suffix, now=moments[0]
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
                for index, (discovery_id, run_id, moment) in enumerate(
                    zip(discovery_ids, run_ids, moments, strict=True)
                ):
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
                            f"snapshot-{index}-{suffix}",
                            moment,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO runs
                            (run_id, discovery_id, config_revision,
                             work_type, source_capability, status,
                             created_at)
                        VALUES (%s, %s, %s, 'anime', %s, 'registered', %s)
                        """,
                        (
                            run_id,
                            discovery_id,
                            config.revision,
                            f"source-{index}-{suffix}",
                            moment,
                        ),
                    )
        queries = PostgresQueries(control.pool)

        runs = queries.list_runs(before=None, limit=10_000)
        discoveries = queries.list_discoveries(before=None, limit=10_000)

        run_positions = {
            item["run_id"]: index for index, item in enumerate(runs)
        }
        discovery_positions = {
            item["discovery_id"]: index
            for index, item in enumerate(discoveries)
        }
        assert run_positions[run_ids[2]] < run_positions[run_ids[1]]
        assert run_positions[run_ids[1]] < run_positions[run_ids[0]]
        assert (
            discovery_positions[discovery_ids[2]]
            < discovery_positions[discovery_ids[1]]
            < discovery_positions[discovery_ids[0]]
        )
        older_runs = queries.list_runs(
            before=run_ids[1],
            limit=10_000,
        )
        older_discoveries = queries.list_discoveries(
            before=discovery_ids[1],
            limit=10_000,
        )
        assert run_ids[0] in {item["run_id"] for item in older_runs}
        assert run_ids[2] not in {item["run_id"] for item in older_runs}
        assert discovery_ids[0] in {
            item["discovery_id"] for item in older_discoveries
        }
        assert discovery_ids[2] not in {
            item["discovery_id"] for item in older_discoveries
        }
    finally:
        control.close()


@pytest.mark.postgres
def test_legacy_handoff_uses_canonical_generation_projection() -> None:
    control = PostgresControlPlane(_dsn())
    suffix = uuid.uuid4().hex
    now = datetime.now(UTC)
    watch_id = f"watch-handoff-{suffix}"
    discovery_id = f"discovery-handoff-{suffix}"
    run_id = f"run-handoff-{suffix}"
    plan_hash = "sha256:" + uuid.uuid4().hex * 2
    try:
        control.open()
        control.migrate()
        config = _ensure_config(control, suffix=suffix, now=now)
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
                        f"Handoff{suffix[:12]}",
                        "folder-generation-v2:" + uuid.uuid4().hex * 2,
                        "folder-inventory-v2:" + uuid.uuid4().hex * 2,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO runs
                        (run_id, discovery_id, config_revision, work_type,
                         source_capability, status)
                    VALUES (%s, %s, %s, 'anime', %s, 'completed')
                    """,
                    (run_id, discovery_id, config.revision, f"source-{suffix}"),
                )
                connection.execute(
                    """
                    INSERT INTO subtitle_acquisition_requests
                        (run_id, plan_hash, config_revision, policy, status)
                    VALUES (%s, %s, %s, 'automatic', 'planned')
                    """,
                    (run_id, plan_hash, config.revision),
                )
                connection.execute(
                    """
                    INSERT INTO generation_requests_v2
                        (request_id, request_kind, origin_run_id, watch_id,
                         source_folder, generation_nonce, lineage_key,
                         state, attempt_count, accepted_at)
                    VALUES (%s, 'legacy_handoff', %s, %s, %s, %s, %s,
                            'accepted', 1, %s)
                    """,
                    (
                        f"generation-request-{suffix}",
                        run_id,
                        watch_id,
                        f"Handoff{suffix[:12]}",
                        f"generation-nonce-{suffix}",
                        f"subtitle-lineage-v1-{uuid.uuid4().hex * 2}",
                        now,
                    ),
                )

        response = RunResponse.model_validate(
            PostgresQueries(control.pool).get_run(run_id)
        )

        assert response.subtitle_acquisition is not None
        assert response.subtitle_acquisition.successor_status == "accepted"
    finally:
        control.close()
