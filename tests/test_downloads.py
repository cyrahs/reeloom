"""DownloadService: submit, track, conclude — against fakes only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from reeloom.adapters.clouddrive import CloudDriveError, OfflineStatus
from reeloom.db import SCHEMA_SQL
from reeloom.models import DownloadState
from reeloom.server.downloads import DownloadError, DownloadService
from tests.fakes import (
    FakeCloudDrive,
    FakeDatabase,
    RecordingDownloadNotifier,
    StubDownloadClients,
)

HASH = "C9E15763F722F23E98A29DECDFAE341B98D53056"
MAGNET = f"magnet:?xt=urn:btih:{HASH.lower()}"
DIR = "/dl"


def make_service(tmp_path: Path, *, stall_hours: int = 24, now=None):
    (tmp_path / "dl").mkdir(exist_ok=True)
    database = FakeDatabase()
    cloud = FakeCloudDrive(tmp_path)
    clients = StubDownloadClients(cloud, stall_hours=stall_hours)
    notifier = RecordingDownloadNotifier()
    kwargs = {"notifier": notifier}
    if now is not None:
        kwargs["now"] = now
    service = DownloadService(database, clients, **kwargs)
    return service, database, cloud, notifier


def later(**kwargs):
    """A clock that reads the given amount into the future."""

    return lambda: datetime.now(timezone.utc) + timedelta(**kwargs)


def test_live_hash_index_matches_the_enum() -> None:
    """The partial index's exclusion list is a hand-written copy of the
    enum's terminal states; this pins them together."""

    terminal = {state.value for state in DownloadState if state.is_terminal}
    assert terminal == {"completed", "lost", "removed"}
    assert "where state not in ('completed', 'lost', 'removed')" in SCHEMA_SQL


# ---- submit -----------------------------------------------------------


async def test_submit_records_then_adds_under_in_progress(tmp_path: Path) -> None:
    service, database, cloud, _ = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)

    assert download.state is DownloadState.SUBMITTED
    assert download.info_hash == HASH
    assert cloud.ensured == [(DIR, "in_progress")]
    assert cloud.added == [([MAGNET], f"{DIR}/in_progress")]
    assert (tmp_path / "dl/in_progress").is_dir()
    assert database.downloads[download.id].state is DownloadState.SUBMITTED


async def test_submit_rejects_untrackable_magnets(tmp_path: Path) -> None:
    service, database, cloud, _ = make_service(tmp_path)
    with pytest.raises(DownloadError) as info:
        await service.submit("magnet:?xt=urn:btmh:1220" + "a" * 64, DIR)
    assert info.value.code == "invalid_magnet"
    assert not database.downloads
    assert not cloud.added


async def test_submit_refuses_a_second_live_row_for_the_hash(
    tmp_path: Path,
) -> None:
    service, _, _, _ = make_service(tmp_path)
    await service.submit(MAGNET, DIR)
    with pytest.raises(DownloadError) as info:
        await service.submit(MAGNET, DIR)
    assert info.value.code == "duplicate_download"


async def test_submit_duplicate_at_clouddrive_counts_as_success(
    tmp_path: Path,
) -> None:
    service, _, cloud, _ = make_service(tmp_path)
    cloud.add_result = {"success": True, "duplicate": True, "error_message": ""}
    download = await service.submit(MAGNET, DIR)
    assert download.state is DownloadState.SUBMITTED


async def test_submit_rollback_on_outright_rejection(tmp_path: Path) -> None:
    service, database, cloud, _ = make_service(tmp_path)
    cloud.fail_add = CloudDriveError("clouddrive_unauthorized")
    with pytest.raises(CloudDriveError):
        await service.submit(MAGNET, DIR)
    assert not database.downloads


async def test_submit_keeps_the_row_on_ambiguous_failure(tmp_path: Path) -> None:
    """A timeout may have reached CloudDrive: the row stays as failed so the
    poll can resurrect it if the task shows up, and the user can retry."""

    service, database, cloud, _ = make_service(tmp_path)
    cloud.fail_add = CloudDriveError("clouddrive_timeout")
    with pytest.raises(CloudDriveError):
        await service.submit(MAGNET, DIR)
    (download,) = database.downloads.values()
    assert download.state is DownloadState.FAILED
    assert download.error == "clouddrive_timeout"


async def test_submit_rolls_back_when_the_server_says_no(tmp_path: Path) -> None:
    service, database, cloud, _ = make_service(tmp_path)
    cloud.add_result = {"success": False, "duplicate": False, "error_message": "quota"}
    with pytest.raises(CloudDriveError) as info:
        await service.submit(MAGNET, DIR)
    assert info.value.code == "clouddrive_rejected"
    assert not database.downloads


# ---- poll: progress and stalls ----------------------------------------


async def test_poll_advances_submitted_to_downloading(tmp_path: Path) -> None:
    service, database, cloud, _ = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)
    cloud.script_task(
        HASH, name="Show S01", status=OfflineStatus.DOWNLOADING, progress=12.5,
        size=2048,
    )
    await service.poll()
    current = database.downloads[download.id]
    assert current.state is DownloadState.DOWNLOADING
    assert current.progress == pytest.approx(12.5)
    assert current.name == "Show S01"
    assert current.size_bytes == 2048


async def test_unchanged_progress_does_not_touch_updated_at(
    tmp_path: Path,
) -> None:
    service, database, cloud, _ = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)
    cloud.script_task(
        HASH, name="Show", status=OfflineStatus.DOWNLOADING, progress=50.0
    )
    await service.poll()
    stamp = database.downloads[download.id].updated_at
    await service.poll()
    assert database.downloads[download.id].updated_at == stamp


async def test_stalled_download_notifies_once(tmp_path: Path) -> None:
    service, database, cloud, notifier = make_service(
        tmp_path, now=later(hours=25)
    )
    download = await service.submit(MAGNET, DIR)
    cloud.script_task(
        HASH, name="Show", status=OfflineStatus.DOWNLOADING, progress=50.0
    )
    await service.poll()  # records progress; updated_at = real now
    await service.poll()  # no change, and the clock reads 25h later
    current = database.downloads[download.id]
    assert current.state is DownloadState.STALLED
    assert [alert.id for alert in notifier.alerts] == [download.id]
    await service.poll()  # still stalled: no second alert
    assert len(notifier.alerts) == 1


async def test_progress_resurrects_a_stalled_row(tmp_path: Path) -> None:
    service, database, cloud, _ = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)
    await database.transition_download(
        download.id,
        expected=[DownloadState.SUBMITTED],
        target=DownloadState.STALLED,
    )
    cloud.script_task(
        HASH, name="Show", status=OfflineStatus.DOWNLOADING, progress=61.0
    )
    await service.poll()
    assert database.downloads[download.id].state is DownloadState.DOWNLOADING


async def test_clouddrive_error_fails_the_row_and_keeps_the_task(
    tmp_path: Path,
) -> None:
    service, database, cloud, notifier = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)
    cloud.script_task(HASH, name="Show", status=OfflineStatus.ERROR)
    await service.poll()
    assert database.downloads[download.id].state is DownloadState.FAILED
    assert len(notifier.alerts) == 1
    assert not cloud.removed  # the task is left for the user to inspect


# ---- poll: lost sweep -------------------------------------------------


async def test_lost_sweep_waits_out_the_submit_grace(tmp_path: Path) -> None:
    service, database, _, notifier = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)
    await service.poll()  # absent, but freshly submitted
    assert database.downloads[download.id].state is DownloadState.SUBMITTED
    assert not notifier.alerts


async def test_lost_sweep_concludes_after_the_grace(tmp_path: Path) -> None:
    service, database, _, notifier = make_service(tmp_path, now=later(seconds=301))
    download = await service.submit(MAGNET, DIR)
    await service.poll()
    assert database.downloads[download.id].state is DownloadState.LOST
    assert len(notifier.alerts) == 1


async def test_listing_failure_suspends_judgement(tmp_path: Path) -> None:
    """No listing is not the same as every task having vanished."""

    service, database, cloud, notifier = make_service(
        tmp_path, now=later(seconds=301)
    )
    download = await service.submit(MAGNET, DIR)
    cloud.fail_list_offline = CloudDriveError("clouddrive_unreachable")
    await service.poll()
    assert database.downloads[download.id].state is DownloadState.SUBMITTED
    assert not notifier.alerts


# ---- poll: finishing and moving ---------------------------------------


def build_finished_folder(tmp_path: Path, name: str = "Show S01") -> None:
    folder = tmp_path / "dl/in_progress" / name
    folder.mkdir(parents=True)
    (folder / "ep01.mkv").write_bytes(b"video")


async def test_finished_folder_is_moved_out_and_completed(
    tmp_path: Path,
) -> None:
    service, database, cloud, notifier = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)
    build_finished_folder(tmp_path)
    cloud.script_task(
        HASH, name="Show S01", status=OfflineStatus.FINISHED, progress=100.0
    )
    await service.poll()
    current = database.downloads[download.id]
    assert current.state is DownloadState.COMPLETED
    assert current.final_path == "/dl/Show S01"
    # Finished on the first poll: the progress writer never ran, so the
    # move is what records the task name for the history row.
    assert current.name == "Show S01"
    assert (tmp_path / "dl/Show S01/ep01.mkv").is_file()
    assert not (tmp_path / "dl/in_progress/Show S01").exists()
    assert not notifier.alerts  # completion is the archive flow's story


async def test_single_file_torrent_is_wrapped_in_a_folder(
    tmp_path: Path,
) -> None:
    service, database, cloud, _ = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)
    (tmp_path / "dl/in_progress").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dl/in_progress/Feature.mkv").write_bytes(b"video")
    cloud.script_task(
        HASH, name="Feature.mkv", status=OfflineStatus.FINISHED, progress=100.0
    )
    await service.poll()
    current = database.downloads[download.id]
    assert current.state is DownloadState.COMPLETED
    assert current.final_path == "/dl/Feature"
    assert (tmp_path / "dl/Feature/Feature.mkv").is_file()


async def test_suffixless_single_file_keeps_its_name(tmp_path: Path) -> None:
    service, database, cloud, _ = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)
    (tmp_path / "dl/in_progress").mkdir(parents=True, exist_ok=True)
    (tmp_path / "dl/in_progress/Feature").write_bytes(b"video")
    cloud.script_task(
        HASH, name="Feature", status=OfflineStatus.FINISHED, progress=100.0
    )
    await service.poll()
    current = database.downloads[download.id]
    assert current.state is DownloadState.COMPLETED
    assert (tmp_path / "dl/Feature/Feature").is_file()


async def test_crash_replay_concludes_from_the_filesystem(
    tmp_path: Path,
) -> None:
    """A row left in moving with the source already gone reads the landed
    destination as done — even when the task record has vanished."""

    service, database, cloud, _ = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)
    await database.record_download_progress(
        download.id,
        state=DownloadState.DOWNLOADING,
        progress=100.0,
        size_bytes=None,
        name="Show S01",
        expected=[DownloadState.SUBMITTED],
    )
    await database.transition_download(
        download.id,
        expected=[DownloadState.DOWNLOADING],
        target=DownloadState.MOVING,
        final_path="/dl/Show S01",
    )
    (tmp_path / "dl/Show S01").mkdir(parents=True)
    (tmp_path / "dl/in_progress").mkdir(parents=True, exist_ok=True)
    await service.poll()  # no task listed at all
    assert database.downloads[download.id].state is DownloadState.COMPLETED


async def test_name_conflict_leaves_moving_then_times_out(
    tmp_path: Path,
) -> None:
    """Conflict-skip never overwrites; the row stays in moving until the
    timeout turns it into failed for a human to resolve."""

    service, database, cloud, notifier = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)
    build_finished_folder(tmp_path)
    (tmp_path / "dl/Show S01").mkdir()  # occupied destination
    cloud.script_task(
        HASH, name="Show S01", status=OfflineStatus.FINISHED, progress=100.0
    )
    await service.poll()
    assert database.downloads[download.id].state is DownloadState.MOVING
    assert (tmp_path / "dl/in_progress/Show S01").is_dir()  # untouched

    slow_service = DownloadService(
        database,
        StubDownloadClients(cloud),
        notifier=notifier,
        now=later(hours=25),
    )
    await slow_service.poll()
    current = database.downloads[download.id]
    assert current.state is DownloadState.FAILED
    assert current.error == "move_did_not_settle"
    assert len(notifier.alerts) == 1


async def test_unsafe_task_name_fails_without_building_a_path(
    tmp_path: Path,
) -> None:
    service, database, cloud, notifier = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)
    cloud.script_task(
        HASH, name="evil/../name", status=OfflineStatus.FINISHED, progress=100.0
    )
    await service.poll()
    current = database.downloads[download.id]
    assert current.state is DownloadState.FAILED
    assert current.error == "unsafe_name"
    assert not cloud.moves
    assert len(notifier.alerts) == 1


# ---- retry and remove -------------------------------------------------


async def test_retry_resubmits_the_same_magnet(tmp_path: Path) -> None:
    service, database, cloud, _ = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)
    await database.transition_download(
        download.id,
        expected=[DownloadState.SUBMITTED],
        target=DownloadState.FAILED,
        error="clouddrive_reported_error",
    )
    result = await service.retry(download.id)
    assert result.state is DownloadState.SUBMITTED
    assert result.progress is None
    assert result.error is None
    assert cloud.removed == [([HASH], DIR, True)]
    assert cloud.added[-1] == ([MAGNET], f"{DIR}/in_progress")


async def test_retry_is_limited_to_failed_and_stalled(tmp_path: Path) -> None:
    service, _, _, _ = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)
    with pytest.raises(DownloadError) as info:
        await service.retry(download.id)
    assert info.value.code == "download_not_retryable"


async def test_remove_drops_the_task_with_its_data(tmp_path: Path) -> None:
    service, database, cloud, _ = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)
    result = await service.remove(download.id)
    assert result.state is DownloadState.REMOVED
    assert cloud.removed == [([HASH], DIR, True)]
    # The hash slot is free again.
    assert await service.submit(MAGNET, DIR)


async def test_remove_refuses_a_row_mid_move(tmp_path: Path) -> None:
    service, database, _, _ = make_service(tmp_path)
    download = await service.submit(MAGNET, DIR)
    await database.transition_download(
        download.id,
        expected=[DownloadState.SUBMITTED],
        target=DownloadState.MOVING,
    )
    with pytest.raises(DownloadError) as info:
        await service.remove(download.id)
    assert info.value.code == "download_is_moving"


async def test_poll_without_configuration_is_a_no_op(tmp_path: Path) -> None:
    database = FakeDatabase()
    await database.create_magnet_download(
        magnet=MAGNET, info_hash=HASH, download_dir=DIR
    )
    service = DownloadService(database, StubDownloadClients(None))
    await service.poll()  # must not raise
    (download,) = database.downloads.values()
    assert download.state is DownloadState.SUBMITTED
