from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from reeloom.executor.subtitle_publication import (
    SubtitlePublicationResult,
    SubtitlePublicationState,
)
from reeloom.kernel.semantic_identity import SemanticRootBinding
from reeloom.kernel.subtitle_acquisition import (
    InspectedSubtitleMember,
    SubtitleAcquisitionPlanV2,
    SubtitleArchiveFormat,
    SubtitleArchiveSetId,
    SubtitleArchiveSource,
    SubtitleArchiveVolume,
    SubtitleReleaseId,
)
from reeloom.kernel.subtitle_publication import (
    SUBTITLE_PUBLICATION_MARKER,
    SubtitlePublicationManifest,
)
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
from reeloom.server.subtitle_publication_repository import (
    PostgresSubtitlePublicationRepository,
)
from reeloom.server.subtitle_scan import SubtitleScanWorker
from reeloom.server.watcher import NoFollowWatcher


def _dsn() -> str:
    value = os.environ.get("REELOOM_TEST_POSTGRES_DSN")
    if not value:
        pytest.skip("REELOOM_TEST_POSTGRES_DSN is not set")
    return value


@pytest.mark.postgres
def test_active_registration_does_not_consume_legacy_subtitle_scan(
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
        root = AuthorizedRoot.create(watch_root)
        started = datetime.now(UTC)
        scan = watcher.scan_folders(root)
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
        registration = scheduler.register_run(discovery_id=discovery.discovery_id)
        folder = scan.folders[0]
        archive = b"PK\x03\x04archive"
        subtitle = b"subtitle"
        archive_id = SubtitleArchiveSetId(1)
        archive_source = SubtitleArchiveSource(
            SubtitleReleaseId(1),
            archive_id,
            SubtitleArchiveFormat.ZIP,
            (1,),
            10081,
            95257,
            "b" * 64,
            (
                SubtitleArchiveVolume(
                    1,
                    34768,
                    len(archive),
                    hashlib.sha256(archive).hexdigest(),
                ),
            ),
        )
        plan = SubtitleAcquisitionPlanV2.create(
            run_id=registration.run_id,
            config_revision=config.revision,
            config_revision_id=config.revision_id,
            watch_id=watch_id,
            created_at=started,
            source_root=SemanticRootBinding(
                PurePosixPath(watch_root.as_posix())
            ),
            source_folder=source.name,
            folder_generation_id=discovery.folder_generation_id or "missing",
            inventory_id=folder.semantic_inventory_id,
            candidate_snapshot=folder.candidates.semantic_snapshot,
            tmdb_id=123,
            archives=(archive_source,),
            inspected_members=(
                InspectedSubtitleMember(
                    archive_id,
                    PurePosixPath("Subs/E01.ass"),
                    len(subtitle),
                    hashlib.sha256(subtitle).hexdigest(),
                ),
            ),
        )
        manifest = SubtitlePublicationManifest.from_plan(plan)
        destination = source / manifest.publication_directory
        destination.mkdir()
        (destination / manifest.members[0].name).write_bytes(subtitle)
        (destination / SUBTITLE_PUBLICATION_MARKER).write_bytes(
            manifest.canonical_bytes()
        )
        approval_id = "approval-v1-" + uuid.uuid4().hex
        with control.pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "UPDATE runs SET status = 'running' WHERE run_id = %s",
                    (registration.run_id,),
                )
                connection.execute(
                    """
                    INSERT INTO subtitle_acquisition_requests
                        (run_id, plan_hash, config_revision, policy,
                         status, approval_id)
                    VALUES (%s, %s, %s, 'automatic', 'approved', %s)
                    """,
                    (
                        registration.run_id,
                        plan.plan_hash,
                        config.revision,
                        approval_id,
                    ),
                )
        publications = PostgresSubtitlePublicationRepository(control.pool)
        result = SubtitlePublicationResult(
            state=SubtitlePublicationState.COMPLETED,
            publication_directory=manifest.publication_directory,
            published_count=1,
        )

        publications.settle(
            plan=plan,
            approval_id=approval_id,
            result=result,
            origin_discovery_id=discovery.discovery_id,
        )
        assert SubtitleScanWorker(publications, scheduler).process_one(
            worker_id="subtitle-scan-test",
            now=started + timedelta(seconds=2),
        )

        fresh = watcher.scan_folders(root)
        scheduler.reconcile_folders(
            watch_id=watch_id,
            config_revision=config.revision,
            fence=config.revision,
            observed_at=started + timedelta(seconds=2),
            scan=fresh,
        )
        successor_discovery = scheduler.reconcile_folders(
            watch_id=watch_id,
            config_revision=config.revision,
            fence=config.revision,
            observed_at=started + timedelta(seconds=3),
            scan=fresh,
        ).discoveries[0]
        successor = scheduler.register_run(
            discovery_id=successor_discovery.discovery_id
        )

        assert publications.lineage_allows_automatic_acquisition(
            successor.run_id
        )
        with control.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT state, successor_run_id
                FROM subtitle_scan_requests_v2
                WHERE run_id = %s
                """,
                (registration.run_id,),
            ).fetchone()
        assert row is not None
        assert str(row[0]) == "dispatched"
        assert row[1] is None
    finally:
        control.close()
