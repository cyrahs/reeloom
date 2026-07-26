from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.server.config import ServerWorkType
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.scheduler import InMemorySchedulerRepository
from reeloom.server.watcher import NoFollowWatcher


def test_watcher_ignores_symlink_and_env_files(tmp_path: Path) -> None:
    (tmp_path / "episode.mkv").write_bytes(b"video")
    (tmp_path / ".env-secret.srt").write_bytes(b"forbidden")
    outside = tmp_path.parent / "outside.mkv"
    outside.write_bytes(b"outside")
    (tmp_path / "escape.mkv").symlink_to(outside)

    snapshot = NoFollowWatcher().scan(AuthorizedRoot.create(tmp_path))

    assert tuple(item.relative_path.as_posix() for item in snapshot.files) == (
        "episode.mkv",
    )


def test_unchanged_poll_does_not_grow_history(tmp_path: Path) -> None:
    (tmp_path / "episode.mkv").write_bytes(b"video")
    snapshot = NoFollowWatcher().scan(AuthorizedRoot.create(tmp_path))
    repository = InMemorySchedulerRepository()
    repository.configure_watch(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        work_type=ServerWorkType.ANIME,
        settle_interval_seconds=60,
    )
    started = datetime(2026, 7, 25, tzinfo=UTC)

    first = repository.reconcile_poll(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        observed_at=started,
        snapshot=snapshot,
    )
    stable = repository.reconcile_poll(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        observed_at=started + timedelta(seconds=60),
        snapshot=snapshot,
    )
    for index in range(10_000):
        unchanged = repository.reconcile_poll(
            watch_id="watch-1",
            config_revision=1,
            fence=1,
            observed_at=started + timedelta(seconds=61 + index),
            snapshot=snapshot,
        )
        assert not unchanged.mutated

    assert first.mutated
    assert stable.discovery is not None
    assert repository.observation_mutations == 2
    assert repository.audit_count == 1


def test_scan_result_is_rejected_after_config_fence_changes(
    tmp_path: Path,
) -> None:
    (tmp_path / "episode.mkv").write_bytes(b"video")
    snapshot = NoFollowWatcher().scan(AuthorizedRoot.create(tmp_path))
    repository = InMemorySchedulerRepository()
    repository.configure_watch(
        watch_id="watch-1",
        config_revision=2,
        fence=8,
        work_type=ServerWorkType.TV,
        settle_interval_seconds=10,
    )

    with pytest.raises(ServerError) as raised:
        repository.reconcile_poll(
            watch_id="watch-1",
            config_revision=1,
            fence=7,
            observed_at=datetime.now(UTC),
            snapshot=snapshot,
        )

    assert raised.value.code is ServerErrorCode.STALE_WATCH_SCAN
    assert repository.observation_mutations == 0


def test_concurrent_discovery_registration_has_one_run_and_job(
    tmp_path: Path,
) -> None:
    (tmp_path / "episode.mkv").write_bytes(b"video")
    snapshot = NoFollowWatcher().scan(AuthorizedRoot.create(tmp_path))
    repository = InMemorySchedulerRepository()
    repository.configure_watch(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        work_type=ServerWorkType.ANIME,
        settle_interval_seconds=1,
    )
    now = datetime.now(UTC)
    repository.reconcile_poll(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        observed_at=now,
        snapshot=snapshot,
    )
    discovery = repository.reconcile_poll(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        observed_at=now + timedelta(seconds=1),
        snapshot=snapshot,
    ).discovery
    assert discovery is not None

    with ThreadPoolExecutor(max_workers=8) as executor:
        registrations = tuple(
            executor.map(
                lambda _: repository.register_run(
                    discovery_id=discovery.discovery_id
                ),
                range(8),
            )
        )

    assert len({item.run_id for item in registrations}) == 1
    assert repository.run_count == 1
    assert repository.job_count == 1


def test_old_boot_running_job_is_reconciled_and_claimed_by_new_boot(
    tmp_path: Path,
) -> None:
    (tmp_path / "episode.mkv").write_bytes(b"video")
    snapshot = NoFollowWatcher().scan(AuthorizedRoot.create(tmp_path))
    repository = InMemorySchedulerRepository()
    repository.configure_watch(
        watch_id="w",
        config_revision=1,
        fence=1,
        work_type=ServerWorkType.ANIME,
        settle_interval_seconds=1,
    )
    now = datetime.now(UTC)
    repository.reconcile_poll(
        watch_id="w",
        config_revision=1,
        fence=1,
        observed_at=now,
        snapshot=snapshot,
    )
    discovery = repository.reconcile_poll(
        watch_id="w",
        config_revision=1,
        fence=1,
        observed_at=now + timedelta(seconds=1),
        snapshot=snapshot,
    ).discovery
    assert discovery is not None
    registration = repository.register_run(
        discovery_id=discovery.discovery_id
    )
    old = repository.claim_job(boot_id="boot-old")
    assert old is not None

    assert repository.reconcile_boot(
        current_boot_id="boot-new"
    ) == 1
    claimed = repository.claim_job(boot_id="boot-new")

    assert claimed is not None
    assert claimed.run_id == registration.run_id
    assert claimed.boot_id == "boot-new"
