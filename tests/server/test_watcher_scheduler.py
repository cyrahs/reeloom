from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.adapters.filesystem import FilesystemScanner
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


def test_folder_watcher_scans_direct_children_independently(
    tmp_path: Path,
) -> None:
    first = tmp_path / "First"
    second = tmp_path / "Second"
    first.mkdir()
    second.mkdir()
    (first / "episode.mkv").write_bytes(b"video")
    (first / "notes.nfo").write_text("metadata")
    (second / "episode.mkv").write_bytes(b"video")
    (tmp_path / "loose.mkv").write_bytes(b"ignored")
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "old.mkv").write_bytes(b"ignored")
    (tmp_path / "FAIL").mkdir()
    (tmp_path / "FAIL" / "bad.mkv").write_bytes(b"ignored")
    (tmp_path / "ａｒｃｈｉｖｅ").mkdir()
    (tmp_path / "ａｒｃｈｉｖｅ" / "wide.mkv").write_bytes(b"ignored")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "hidden.mkv").write_bytes(b"ignored")

    scan = NoFollowWatcher().scan_folders(AuthorizedRoot.create(tmp_path))

    assert tuple(item.name for item in scan.folders) == ("First", "Second")
    assert scan.blocked == ()
    assert tuple(
        item.relative_path.as_posix()
        for item in scan.folders[0].candidates.files
    ) == ("First/episode.mkv",)
    assert tuple(
        item.relative_path.as_posix()
        for item in scan.folders[0].entries
    ) == ("episode.mkv", "notes.nfo")


def test_folder_watcher_tracks_nested_symlink_without_following(
    tmp_path: Path,
) -> None:
    work = tmp_path / "Work"
    work.mkdir()
    (work / "episode.mkv").write_bytes(b"video")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mkv").write_bytes(b"secret")
    (work / "link").symlink_to(outside, target_is_directory=True)

    scan = NoFollowWatcher().scan_folders(AuthorizedRoot.create(tmp_path))

    work_snapshot = next(item for item in scan.folders if item.name == "Work")
    assert tuple(
        item.relative_path.as_posix() for item in work_snapshot.entries
    ) == ("episode.mkv", "link")
    assert tuple(
        item.relative_path.as_posix()
        for item in work_snapshot.candidates.files
    ) == ("Work/episode.mkv",)


def test_folder_watcher_blocks_env_without_scanning_candidates(
    tmp_path: Path,
) -> None:
    work = tmp_path / "Work"
    work.mkdir()
    (work / ".env-secret").write_text("forbidden")
    (work / "episode.mkv").write_bytes(b"video")

    scan = NoFollowWatcher().scan_folders(AuthorizedRoot.create(tmp_path))

    assert scan.folders == ()
    assert tuple((item.name, item.reason) for item in scan.blocked) == (
        ("Work", "env_path_forbidden"),
    )


def test_folder_watcher_finds_nested_env_before_reading_subtitle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "Work"
    later = work / "z-later"
    later.mkdir(parents=True)
    (work / "a-first.srt").write_text("subtitle")
    (later / ".env").write_text("forbidden")
    reads = 0

    def unexpected_read(**_: object) -> str:
        nonlocal reads
        reads += 1
        return "unused"

    monkeypatch.setattr(
        FilesystemScanner,
        "_subtitle_sample_digest",
        unexpected_read,
    )

    scan = NoFollowWatcher().scan_folders(AuthorizedRoot.create(tmp_path))

    assert scan.folders == ()
    assert scan.blocked[0].reason == "env_path_forbidden"
    assert reads == 0


def test_folder_discoveries_settle_independently(tmp_path: Path) -> None:
    first = tmp_path / "First"
    second = tmp_path / "Second"
    first.mkdir()
    second.mkdir()
    (first / "episode.mkv").write_bytes(b"same")
    (second / "episode.mkv").write_bytes(b"same")
    watcher = NoFollowWatcher()
    repository = InMemorySchedulerRepository()
    repository.configure_watch(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        work_type=ServerWorkType.ANIME,
        settle_interval_seconds=10,
    )
    started = datetime(2026, 7, 27, tzinfo=UTC)

    first_poll = repository.reconcile_folders(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        observed_at=started,
        scan=watcher.scan_folders(AuthorizedRoot.create(tmp_path)),
    )
    stable = repository.reconcile_folders(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        observed_at=started + timedelta(seconds=10),
        scan=watcher.scan_folders(AuthorizedRoot.create(tmp_path)),
    )

    assert first_poll.discoveries == ()
    assert {item.source_folder for item in stable.discoveries} == {
        "First",
        "Second",
    }
    assert len({item.discovery_id for item in stable.discoveries}) == 2
    assert all(
        tuple(
            path.relative_path.parts[0]
            for path in item.snapshot.files  # type: ignore[union-attr]
        )
        in {("First",), ("Second",)}
        for item in stable.discoveries
    )


def test_folder_change_creates_a_new_generation_after_settling(
    tmp_path: Path,
) -> None:
    work = tmp_path / "Work"
    work.mkdir()
    video = work / "episode.mkv"
    video.write_bytes(b"first")
    watcher = NoFollowWatcher()
    repository = InMemorySchedulerRepository()
    repository.configure_watch(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        work_type=ServerWorkType.ANIME,
        settle_interval_seconds=1,
    )
    started = datetime(2026, 7, 27, tzinfo=UTC)
    repository.reconcile_folders(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        observed_at=started,
        scan=watcher.scan_folders(AuthorizedRoot.create(tmp_path)),
    )
    first = repository.reconcile_folders(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        observed_at=started + timedelta(seconds=1),
        scan=watcher.scan_folders(AuthorizedRoot.create(tmp_path)),
    ).discoveries[0]
    video.write_bytes(b"second")
    repository.reconcile_folders(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        observed_at=started + timedelta(seconds=2),
        scan=watcher.scan_folders(AuthorizedRoot.create(tmp_path)),
    )
    second = repository.reconcile_folders(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        observed_at=started + timedelta(seconds=3),
        scan=watcher.scan_folders(AuthorizedRoot.create(tmp_path)),
    ).discoveries[0]

    assert second.folder_generation_id != first.folder_generation_id


def test_empty_folder_settles_for_no_video_disposition(
    tmp_path: Path,
) -> None:
    (tmp_path / "Empty").mkdir()
    repository = InMemorySchedulerRepository()
    repository.configure_watch(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        work_type=ServerWorkType.MOVIE,
        settle_interval_seconds=1,
    )
    started = datetime(2026, 7, 27, tzinfo=UTC)
    scan = NoFollowWatcher().scan_folders(AuthorizedRoot.create(tmp_path))
    repository.reconcile_folders(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        observed_at=started,
        scan=scan,
    )

    stable = repository.reconcile_folders(
        watch_id="watch-1",
        config_revision=1,
        fence=1,
        observed_at=started + timedelta(seconds=1),
        scan=scan,
    )

    assert stable.discoveries[0].source_folder == "Empty"
    assert stable.discoveries[0].snapshot.files == ()  # type: ignore[union-attr]


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
