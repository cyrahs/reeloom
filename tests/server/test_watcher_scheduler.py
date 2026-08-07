from __future__ import annotations

import os
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reeloom.policy.path_policy import AuthorizedRoot
from reeloom.adapters.filesystem import FilesystemScanner
from reeloom.server.config import ServerWorkType
from reeloom.server.agent_worker import InitialAgentWorker
from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.scheduler import (
    AgentJobContext,
    Discovery,
    InMemorySchedulerRepository,
    RunRegistration,
)
from reeloom.server.scheduler_repository import (
    _inventory_json,
    _missing_folder_action,
    _snapshot_from_json,
    _snapshot_json,
)
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


def test_folder_semantic_identity_ignores_stat_metadata(
    tmp_path: Path,
) -> None:
    work = tmp_path / "Work"
    work.mkdir()
    video = work / "episode.mkv"
    video.write_bytes(b"first")
    watcher = NoFollowWatcher()
    root = AuthorizedRoot.create(tmp_path)

    before = watcher.scan_folders(root).folders[0]
    os.utime(video, ns=(1_000_000_000, 1_000_000_000))
    after = watcher.scan_folders(root).folders[0]

    assert before.inventory_id != after.inventory_id
    assert before.candidates.snapshot_id != after.candidates.snapshot_id
    assert before.semantic_inventory_id == after.semantic_inventory_id
    assert (
        before.candidates.semantic_snapshot_id
        == after.candidates.semantic_snapshot_id
    )
    assert set(before.entries[0].semantic_payload) == {
        "kind",
        "relative_path",
        "size_bytes",
    }


def test_same_path_and_size_video_replacement_is_semantically_unchanged(
    tmp_path: Path,
) -> None:
    work = tmp_path / "Work"
    work.mkdir()
    video = work / "episode.mkv"
    video.write_bytes(b"first")
    watcher = NoFollowWatcher()
    root = AuthorizedRoot.create(tmp_path)
    before = watcher.scan_folders(root).folders[0]

    replacement = work / "replacement.tmp"
    replacement.write_bytes(b"other")
    replacement.replace(video)
    after = watcher.scan_folders(root).folders[0]

    assert before.candidates.snapshot_id != after.candidates.snapshot_id
    assert before.inventory_id != after.inventory_id
    assert (
        before.candidates.semantic_snapshot_id
        == after.candidates.semantic_snapshot_id
    )
    assert before.semantic_inventory_id == after.semantic_inventory_id


def test_subtitle_semantic_identity_uses_the_full_file_hash(
    tmp_path: Path,
) -> None:
    work = tmp_path / "Work"
    work.mkdir()
    subtitle = work / "episode.ass"
    prefix = b"x" * (64 * 1024)
    subtitle.write_bytes(prefix + b"first")
    watcher = NoFollowWatcher()
    root = AuthorizedRoot.create(tmp_path)
    before = watcher.scan_folders(root).folders[0]

    subtitle.write_bytes(prefix + b"other")
    after = watcher.scan_folders(root).folders[0]

    assert (
        before.candidates.files[0].sample_digest
        == after.candidates.files[0].sample_digest
    )
    assert before.candidates.files[0].sha256 != after.candidates.files[0].sha256
    assert (
        before.candidates.semantic_snapshot_id
        != after.candidates.semantic_snapshot_id
    )


def test_semantic_watch_payload_omits_persistent_stat_identity(
    tmp_path: Path,
) -> None:
    work = tmp_path / "Work"
    work.mkdir()
    (work / "episode.mkv").write_bytes(b"video")
    (work / "episode.ass").write_bytes(b"subtitle")
    folder = NoFollowWatcher().scan_folders(
        AuthorizedRoot.create(tmp_path)
    ).folders[0]

    snapshot_payload = _snapshot_json(
        folder.candidates, semantic_v2=True
    )
    inventory_payload = _inventory_json(folder, semantic_v2=True)
    restored = _snapshot_from_json(json.loads(snapshot_payload))

    forbidden = ("device", "inode", "mtime_ns", "ctime_ns")
    assert not any(field in snapshot_payload for field in forbidden)
    assert not any(field in inventory_payload for field in forbidden)
    assert restored.semantic_snapshot_id == (
        folder.candidates.semantic_snapshot_id
    )
    assert restored.snapshot_id.startswith("candidate-snapshot-v2:")


def test_agent_rescans_semantic_discovery_from_current_paths(
    tmp_path: Path,
) -> None:
    work = tmp_path / "Work"
    work.mkdir()
    video = work / "episode.mkv"
    video.write_bytes(b"first")
    watcher = NoFollowWatcher()
    root = AuthorizedRoot.create(tmp_path)
    folder = watcher.scan_folders(root).folders[0]
    semantic_id = folder.candidates.semantic_snapshot_id
    persisted = _snapshot_from_json(
        json.loads(_snapshot_json(folder.candidates, semantic_v2=True))
    )
    os.utime(video, ns=(1_000_000_000, 1_000_000_000))
    job = AgentJobContext(
        registration=RunRegistration(
            run_id="run-v2",
            job_id="job-v2",
            discovery_id="discovery-v2",
            config_revision=1,
            work_type=ServerWorkType.ANIME,
            source_capability="capability-v2",
        ),
        discovery=Discovery(
            discovery_id="discovery-v2",
            watch_id="watch-v2",
            config_revision=1,
            snapshot_id=semantic_id,
            work_type=ServerWorkType.ANIME,
            discovered_at=datetime.now(UTC),
            snapshot=persisted,
            source_folder="Work",
            folder_generation_id="folder-v2",
        ),
    )

    transient, semantic = InitialAgentWorker._resolve_snapshots(job, root)

    assert semantic is not None
    assert semantic.snapshot_id == semantic_id
    assert transient.snapshot_id != folder.candidates.snapshot_id
    assert transient.candidates == semantic.candidates


def test_folder_poll_performs_one_namespace_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "Work"
    work.mkdir()
    (work / "episode.mkv").write_bytes(b"video")
    calls = 0
    original = NoFollowWatcher._scan_folder_once

    def counted(self: NoFollowWatcher, *args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(NoFollowWatcher, "_scan_folder_once", counted)

    scan = NoFollowWatcher().scan_folders(AuthorizedRoot.create(tmp_path))

    assert len(scan.folders) == 1
    assert calls == 1


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


def test_folder_watcher_ignores_acquisition_staging_but_reads_published_subtitles(
    tmp_path: Path,
) -> None:
    work = tmp_path / "Work"
    staging = work / (".reeloom-acquiring-" + "a" * 64)
    published = work / ("reeloom-acquired-" + "b" * 64)
    staging.mkdir(parents=True)
    published.mkdir()
    (work / "episode.mkv").write_bytes(b"video")
    (staging / "partial.ass").write_bytes(b"partial")
    (published / "episode.ass").write_bytes(b"subtitle")

    scan = NoFollowWatcher().scan_folders(AuthorizedRoot.create(tmp_path))

    assert len(scan.folders) == 1
    assert tuple(
        item.relative_path.as_posix()
        for item in scan.folders[0].candidates.files
    ) == (
        "Work/episode.mkv",
        "Work/reeloom-acquired-" + "b" * 64 + "/episode.ass",
    )
    assert all(
        not item.relative_path.as_posix().startswith(".reeloom-acquiring-")
        for item in scan.folders[0].entries
    )


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


def test_semantic_scheduler_does_not_restart_for_metadata_or_equal_size_replacement(
    tmp_path: Path,
) -> None:
    work = tmp_path / "Work"
    work.mkdir()
    video = work / "episode.mkv"
    video.write_bytes(b"first")
    watcher = NoFollowWatcher()
    root = AuthorizedRoot.create(tmp_path)
    repository = InMemorySchedulerRepository()
    repository.configure_watch(
        watch_id="watch-v2",
        config_revision=1,
        fence=1,
        work_type=ServerWorkType.ANIME,
        settle_interval_seconds=1,
        semantic_v2=True,
    )
    started = datetime(2026, 8, 7, tzinfo=UTC)
    repository.reconcile_folders(
        watch_id="watch-v2",
        config_revision=1,
        fence=1,
        observed_at=started,
        scan=watcher.scan_folders(root),
    )
    first = repository.reconcile_folders(
        watch_id="watch-v2",
        config_revision=1,
        fence=1,
        observed_at=started + timedelta(seconds=1),
        scan=watcher.scan_folders(root),
    ).discoveries[0]

    os.utime(video, ns=(2_000_000_000, 2_000_000_000))
    metadata_only = repository.reconcile_folders(
        watch_id="watch-v2",
        config_revision=1,
        fence=1,
        observed_at=started + timedelta(seconds=2),
        scan=watcher.scan_folders(root),
    )
    replacement = work / "replacement.tmp"
    replacement.write_bytes(b"other")
    replacement.replace(video)
    replaced = repository.reconcile_folders(
        watch_id="watch-v2",
        config_revision=1,
        fence=1,
        observed_at=started + timedelta(seconds=3),
        scan=watcher.scan_folders(root),
    )

    assert metadata_only.discoveries == ()
    assert replaced.discoveries == ()
    assert first.snapshot_id.startswith("candidate-snapshot-v2:")
    assert first.inventory_id is not None
    assert first.inventory_id.startswith("folder-inventory-v2:")

    video.write_bytes(b"different-size")
    repository.reconcile_folders(
        watch_id="watch-v2",
        config_revision=1,
        fence=1,
        observed_at=started + timedelta(seconds=4),
        scan=watcher.scan_folders(root),
    )
    second = repository.reconcile_folders(
        watch_id="watch-v2",
        config_revision=1,
        fence=1,
        observed_at=started + timedelta(seconds=5),
        scan=watcher.scan_folders(root),
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


@pytest.mark.parametrize(
    ("status", "has_discovery", "terminal_ready", "elapsed", "expected"),
    (
        ("active", True, True, 0, "start_missing"),
        ("active", True, False, 100, "keep"),
        ("settling", True, True, 9, "keep"),
        ("settling", True, True, 10, "confirm_missing"),
        ("blocked", True, True, 100, "keep"),
        ("settling", False, True, 100, "remove"),
        ("settled", True, True, 100, "remove"),
    ),
)
def test_missing_folder_transition_is_bounded(
    status: str,
    has_discovery: bool,
    terminal_ready: bool,
    elapsed: int,
    expected: str,
) -> None:
    assert (
        _missing_folder_action(
            status=status,
            has_discovery=has_discovery,
            terminal_ready=terminal_ready,
            elapsed_seconds=elapsed,
            settle_interval_seconds=10,
        )
        == expected
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
