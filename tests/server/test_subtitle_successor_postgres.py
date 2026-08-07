from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
    ServerWorkType,
)
from reeloom.server.config_repository import PostgresConfigRepository
from reeloom.server.database import PostgresControlPlane
from reeloom.server.scheduler_repository import PostgresSchedulerRepository
from reeloom.server.subtitle_successor import (
    SubtitleAcquisitionSettlement,
    SubtitleSuccessorMember,
)
from reeloom.server.subtitle_successor_repository import (
    PostgresSubtitleSuccessorOutbox,
)
from reeloom.server.watcher import NoFollowWatcher


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


@pytest.mark.postgres
def test_settle_and_fresh_successor_registration_are_transactional(
    tmp_path: Path,
) -> None:
    control = PostgresControlPlane(_dsn())
    try:
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
        source = watch_root / "Incoming"
        source.mkdir(parents=True)
        (source / "episode.mkv").write_bytes(b"video")
        watcher = NoFollowWatcher()
        started = datetime.now(UTC)
        scan = watcher.scan_folders(AuthorizedRoot.create(watch_root))
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
        destination_name = "reeloom-acquired-" + "a" * 64
        destination = source / destination_name
        destination.mkdir()
        member_name = "subtitle.ass"
        content = b"subtitle"
        (destination / member_name).write_bytes(content)
        destination_stat = destination.stat()
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "UPDATE runs SET status = 'running' WHERE run_id = %s",
                    (registration.run_id,),
                )
        settlement = SubtitleAcquisitionSettlement(
            origin_run_id=registration.run_id,
            origin_discovery_id=discovery.discovery_id,
            plan_hash="sha256:" + "a" * 64,
            approval_id=f"approval-{uuid.uuid4().hex}",
            transaction_id=f"transaction-{uuid.uuid4().hex}",
            source_folder="Incoming",
            source_folder_device=destination.parent.stat().st_dev,
            source_folder_inode=destination.parent.stat().st_ino,
            original_snapshot_id=discovery.snapshot_id,
            destination_name=destination_name,
            destination_device=destination_stat.st_dev,
            destination_inode=destination_stat.st_ino,
            members=(SubtitleSuccessorMember(member_name, len(content)),),
        )
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO subtitle_acquisition_requests
                        (run_id, plan_hash, config_revision, policy,
                         status, approval_id)
                    VALUES (%s, %s, %s, 'automatic', 'approved', %s)
                    """,
                    (
                        registration.run_id,
                        settlement.plan_hash,
                        config.revision,
                        settlement.approval_id,
                    ),
                )
        repository = PostgresSubtitleSuccessorOutbox(control.pool)

        settled = repository.settle(settlement)

        assert settled.created
        claim = repository.claim(
            worker_id="worker-postgres",
            now=started + timedelta(seconds=2),
            lease_for=timedelta(seconds=30),
        )
        assert claim is not None
        fresh = watcher.scan_folders(
            AuthorizedRoot.create(watch_root)
        ).folders[0]
        assert not repository.stabilize(
            claim,
            snapshot=fresh,
            now=started + timedelta(seconds=2),
            delay=timedelta(seconds=1),
        )
        claim = repository.claim(
            worker_id="worker-postgres",
            now=started + timedelta(seconds=3),
            lease_for=timedelta(seconds=30),
        )
        assert claim is not None
        assert repository.stabilize(
            claim,
            snapshot=fresh,
            now=started + timedelta(seconds=3),
            delay=timedelta(seconds=1),
        )
        successor = repository.complete(
            claim,
            snapshot=fresh,
            now=started + timedelta(seconds=3),
        )

        with control.pool.connection() as connection:
            origin = connection.execute(
                "SELECT status FROM runs WHERE run_id = %s",
                (registration.run_id,),
            ).fetchone()
            created = connection.execute(
                """
                SELECT r.status, j.status, o.state,
                       r.subtitle_acquisition_lineage_key
                FROM runs AS r
                JOIN jobs AS j USING (run_id)
                JOIN subtitle_successor_outbox AS o
                  ON o.successor_run_id = r.run_id
                WHERE r.run_id = %s
                """,
                (successor.registration.run_id,),
            ).fetchone()
        assert origin == ("superseded",)
        assert created is not None
        assert tuple(created[:3]) == ("registered", "pending", "completed")
        assert str(created[3]) == settled.lineage_key
        assert not repository.lineage_allows_automatic_acquisition(
            successor.registration.run_id
        )
    finally:
        control.close()
