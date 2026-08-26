"""Magnet downloads through CloudDrive2.

Tasks are submitted into ``<download_dir>/in_progress`` — a reserved name the
scanner never picks up — and tracked by info hash against CloudDrive's
offline-task list. A finished item is moved cloud-side out of ``in_progress``
into ``download_dir``; the mounted watch root then sees it appear and the
normal archive flow takes over. An incomplete download can therefore never
become a run.

Coordination is one ``state`` column with compare-and-set transitions; the
``moving`` state is re-entered idempotently every poll (re-list first), which
is the whole crash-recovery story. No journal.
"""

from __future__ import annotations

import logging
import posixpath
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Protocol

from reeloom.adapters.clouddrive import (
    AsyncCloudDrive,
    CloudDriveError,
    OfflineStatus,
    validate_api_path,
    validate_path_segment,
)
from reeloom.adapters.clouddrive.aio import OfflineTask
from reeloom.db import Database
from reeloom.magnet import extract_info_hash
from reeloom.models import DownloadState, MagnetDownload, ReeloomError
from reeloom.scanner import IN_PROGRESS_BUCKET
from reeloom.server.composition import Clients, NotConfigured

_LOGGER = logging.getLogger(__name__)

#: A magnet submitted during this very poll cannot appear in the listing the
#: poll already read; only past this grace does "absent everywhere" mean lost.
SUBMIT_GRACE_SECONDS = 300

#: Submit failures that prove CloudDrive rejected the magnet outright, so the
#: just-created row can be dropped. Anything else (timeout, unreachable) is
#: ambiguous — the task may have been accepted — so the row is kept as
#: ``failed``: the poll resurrects it if the task shows up, and the user can
#: retry or delete it otherwise.
_SUBMIT_REJECT_CODES = frozenset(
    {"clouddrive_unauthorized", "clouddrive_path_not_found", "clouddrive_rejected"}
)

_PRE_MOVE_STATES = (
    DownloadState.SUBMITTED,
    DownloadState.DOWNLOADING,
    DownloadState.FAILED,
    DownloadState.STALLED,
)


class DownloadError(ReeloomError):
    pass


class DownloadNotifier(Protocol):
    async def download_trouble(self, download: MagnetDownload) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DownloadService:
    """Submit, track and conclude magnet downloads. Used by both the API and
    the worker poll; every state change is a CAS through the repository."""

    def __init__(
        self,
        database: Database,
        clients: Clients,
        *,
        notifier: DownloadNotifier | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._db = database
        self._clients = clients
        self._notifier = notifier
        self._now = now

    # ---- API entry points ---------------------------------------------

    async def submit(self, magnet: str, directory: str) -> MagnetDownload:
        info_hash = extract_info_hash(magnet)
        if info_hash is None:
            raise DownloadError("invalid_magnet")
        validate_api_path(directory, allow_root=False)
        cloud = await self._clients.clouddrive()

        # The row exists before any RPC: a crash mid-submit reconciles
        # against the task listing instead of double-submitting.
        download = await self._db.create_magnet_download(
            magnet=magnet, info_hash=info_hash, download_dir=directory
        )
        if download is None:
            raise DownloadError("duplicate_download")

        try:
            await cloud.ensure_directory(directory, IN_PROGRESS_BUCKET)
        except Exception:
            # No add was attempted, so no task can exist: the row is safe to drop.
            await self._db.delete_magnet_download(download.id)
            raise
        try:
            result = await cloud.add_offline_files(
                [magnet], _task_dir(directory)
            )
            if not result["success"]:
                raise CloudDriveError(
                    "clouddrive_rejected",
                    details=str(result.get("error_message", ""))[:200],
                )
        except CloudDriveError as error:
            if error.code in _SUBMIT_REJECT_CODES:
                await self._db.delete_magnet_download(download.id)
            else:
                await self._db.transition_download(
                    download.id,
                    expected=[DownloadState.SUBMITTED],
                    target=DownloadState.FAILED,
                    error=error.code,
                )
            raise
        return await self._db.get_magnet_download(download.id) or download

    async def retry(self, download_id: str) -> MagnetDownload:
        """Re-submit the same magnet: drop the old task (best effort, data
        included) and add it again. The row is reused, so the live-hash
        uniqueness holds throughout."""

        download = await self._require(download_id)
        if download.state not in (DownloadState.FAILED, DownloadState.STALLED):
            raise DownloadError("download_not_retryable", state=download.state.value)
        cloud = await self._clients.clouddrive()
        try:
            await cloud.remove_offline_files(
                [download.info_hash], download.download_dir, delete_files=True
            )
        except CloudDriveError as error:
            _LOGGER.info(
                "old task removal failed before retry download=%s: %s",
                download.id,
                error.code,
            )
        await cloud.ensure_directory(download.download_dir, IN_PROGRESS_BUCKET)
        result = await cloud.add_offline_files(
            [download.magnet], _task_dir(download.download_dir)
        )
        if not result["success"]:
            raise CloudDriveError(
                "clouddrive_rejected",
                details=str(result.get("error_message", ""))[:200],
            )
        await self._db.transition_download(
            download.id,
            expected=[DownloadState.FAILED, DownloadState.STALLED],
            target=DownloadState.SUBMITTED,
            mark_submitted=True,
        )
        return await self._require(download_id)

    async def remove(self, download_id: str) -> MagnetDownload:
        """Drop the task at CloudDrive, downloaded data included, and close
        the row. User-initiated only; never on the archive execution path."""

        download = await self._require(download_id)
        if download.state is DownloadState.MOVING:
            raise DownloadError("download_is_moving")
        if not download.state.is_live:
            raise DownloadError("download_not_live", state=download.state.value)
        cloud = await self._clients.clouddrive()
        try:
            await cloud.remove_offline_files(
                [download.info_hash], download.download_dir, delete_files=True
            )
        except CloudDriveError as error:
            if error.code != "clouddrive_path_not_found":
                raise
        expected = [state for state in _PRE_MOVE_STATES]
        await self._db.transition_download(
            download.id, expected=expected, target=DownloadState.REMOVED
        )
        return await self._require(download_id)

    async def _require(self, download_id: str) -> MagnetDownload:
        download = await self._db.get_magnet_download(download_id)
        if download is None:
            raise DownloadError("download_not_found")
        return download

    # ---- worker poll --------------------------------------------------

    async def poll(self) -> None:
        """One tracking pass over every live download."""

        live = await self._db.live_magnet_downloads()
        if not live:
            return
        try:
            cloud = await self._clients.clouddrive()
        except NotConfigured:
            return
        stall_after = timedelta(hours=await self._clients.download_stall_hours())

        by_hash: dict[str, OfflineTask] = {}
        failed_dirs: set[str] = set()
        for directory in sorted({item.download_dir for item in live}):
            try:
                tasks = await cloud.list_offline_files(_task_dir(directory))
            except CloudDriveError as error:
                # No listing is not the same as every task having vanished:
                # rows under this dir are neither advanced nor swept as lost.
                _LOGGER.warning(
                    "offline listing failed dir=%s: %s", directory, error.code
                )
                failed_dirs.add(directory)
                continue
            for task in tasks:
                by_hash.setdefault(str(task["info_hash"]), task)

        for download in live:
            if download.download_dir in failed_dirs:
                continue
            try:
                await self._advance(cloud, download, by_hash, stall_after)
            except CloudDriveError as error:
                _LOGGER.warning(
                    "download advance failed download=%s: %s",
                    download.id,
                    error.code,
                )

    async def _advance(
        self,
        cloud: AsyncCloudDrive,
        download: MagnetDownload,
        by_hash: dict[str, OfflineTask],
        stall_after: timedelta,
    ) -> None:
        task = by_hash.get(download.info_hash)

        if task is None:
            if download.state is DownloadState.MOVING:
                # The task record may be gone, but the move still needs to be
                # replayed to a conclusion from the filesystem.
                await self._move(cloud, download, task=None, stall_after=stall_after)
            elif download.state in (
                DownloadState.SUBMITTED,
                DownloadState.DOWNLOADING,
            ):
                await self._sweep_lost(download)
            # failed/stalled rows without a task stay put: the user decides.
            return

        status = task["status"]
        if download.state is DownloadState.MOVING or status is OfflineStatus.FINISHED:
            await self._move(cloud, download, task=task, stall_after=stall_after)
            return
        if status is OfflineStatus.ERROR:
            moved = await self._db.transition_download(
                download.id,
                expected=[
                    DownloadState.SUBMITTED,
                    DownloadState.DOWNLOADING,
                    DownloadState.STALLED,
                ],
                target=DownloadState.FAILED,
                error="clouddrive_reported_error",
            )
            if moved:
                await self._notify(download.id)
            return

        # INIT / DOWNLOADING / UNKNOWN: record progress. A write refreshes
        # updated_at and silently resurrects failed/stalled rows whose task
        # moved again; no write on a downloading row is the stall signal.
        wrote = await self._db.record_download_progress(
            download.id,
            state=DownloadState.DOWNLOADING,
            progress=float(task["progress"]),
            size_bytes=int(task["size"]) or None,
            name=str(task["name"]) or None,
            expected=list(_PRE_MOVE_STATES),
        )
        if wrote or download.state is not DownloadState.DOWNLOADING:
            return
        if download.updated_at is None:
            return
        stalled_for = self._now() - download.updated_at
        if stalled_for < stall_after:
            return
        moved = await self._db.transition_download(
            download.id,
            expected=[DownloadState.DOWNLOADING],
            target=DownloadState.STALLED,
            error="download_stalled",
        )
        if moved:
            await self._notify(download.id)

    async def _sweep_lost(self, download: MagnetDownload) -> None:
        if download.submitted_at is None:
            return
        age = (self._now() - download.submitted_at).total_seconds()
        if age < SUBMIT_GRACE_SECONDS:
            return
        moved = await self._db.transition_download(
            download.id,
            expected=[DownloadState.SUBMITTED, DownloadState.DOWNLOADING],
            target=DownloadState.LOST,
            error="task_missing_from_clouddrive",
        )
        if moved:
            await self._notify(download.id)

    async def _move(
        self,
        cloud: AsyncCloudDrive,
        download: MagnetDownload,
        *,
        task: OfflineTask | None,
        stall_after: timedelta,
    ) -> None:
        """Move the finished item out of in_progress, idempotently.

        Verification is by re-listing, never by trusting the MoveFile result:
        the RPC takes only paths, so the filesystem is the ground truth. Not
        concluded this pass means try again next poll, until the stall
        timeout turns the row into failed.
        """

        if download.state is not DownloadState.MOVING:
            if not await self._db.transition_download(
                download.id,
                expected=list(_PRE_MOVE_STATES),
                target=DownloadState.MOVING,
            ):
                return  # concluded concurrently by the API

        name = download.name or (str(task["name"]) if task else None)
        if not name:
            return  # no name to move by yet; the next poll will carry one
        try:
            validate_path_segment(name)
        except CloudDriveError:
            # Never build a path from an unvalidated name.
            moved = await self._db.transition_download(
                download.id,
                expected=[DownloadState.MOVING],
                target=DownloadState.FAILED,
                error="unsafe_name",
            )
            if moved:
                await self._notify(download.id)
            return

        # Force-refresh parent then child to bust CloudDrive's persistent
        # directory cache; this doubles as the refresh that lets the FUSE
        # mount see the folder.
        in_progress = _task_dir(download.download_dir)
        parent_entries = await cloud.list_directory(download.download_dir)
        try:
            progress_entries = await cloud.list_directory(in_progress)
        except CloudDriveError as error:
            if error.code != "clouddrive_path_not_found":
                raise
            progress_entries = ()

        source = next(
            (entry for entry in progress_entries if entry["name"] == name), None
        )
        if source is None:
            # Crash replay: the move may already have happened.
            final_name = (
                posixpath.basename(download.final_path)
                if download.final_path
                else name
            )
            landed = any(
                entry["name"] == final_name and entry["is_directory"]
                for entry in parent_entries
            )
            if landed:
                await self._db.transition_download(
                    download.id,
                    expected=[DownloadState.MOVING],
                    target=DownloadState.COMPLETED,
                    final_path=posixpath.join(download.download_dir, final_name),
                )
                return
            await self._maybe_fail_move(download.id, stall_after)
            return

        if source["is_directory"]:
            final_name = name
        else:
            # The scanner only discovers directories, so a single-file
            # torrent gets wrapped in a folder named after its stem.
            final_name = PurePosixPath(name).stem or name
        planned_final = posixpath.join(download.download_dir, final_name)
        if download.final_path != planned_final:
            # Recorded once, before the move, so a crash replay knows what
            # to look for; guarded so retries do not refresh updated_at and
            # defeat the move timeout.
            await self._db.transition_download(
                download.id,
                expected=[DownloadState.MOVING],
                target=DownloadState.MOVING,
                final_path=planned_final,
            )
        source_path = posixpath.join(in_progress, name)
        if source["is_directory"]:
            await cloud.move_file(source_path, download.download_dir)
        else:
            await cloud.ensure_directory(download.download_dir, final_name)
            await cloud.move_file(source_path, planned_final)

        # Verify: destination present and source gone, by fresh listings.
        parent_after = await cloud.list_directory(download.download_dir)
        progress_after = await cloud.list_directory(in_progress)
        landed = any(
            entry["name"] == final_name and entry["is_directory"]
            for entry in parent_after
        )
        source_gone = all(entry["name"] != name for entry in progress_after)
        if landed and source_gone:
            await self._db.transition_download(
                download.id,
                expected=[DownloadState.MOVING],
                target=DownloadState.COMPLETED,
                final_path=planned_final,
            )
            return
        # Conflict-skip fired or the move did not settle: retried next poll,
        # capped by the move timeout.
        await self._maybe_fail_move(download.id, stall_after)

    async def _maybe_fail_move(
        self, download_id: str, stall_after: timedelta
    ) -> None:
        current = await self._db.get_magnet_download(download_id)
        if (
            current is None
            or current.state is not DownloadState.MOVING
            or current.updated_at is None
        ):
            return
        if self._now() - current.updated_at < stall_after:
            return
        moved = await self._db.transition_download(
            download_id,
            expected=[DownloadState.MOVING],
            target=DownloadState.FAILED,
            error="move_did_not_settle",
        )
        if moved:
            await self._notify(download_id)

    async def _notify(self, download_id: str) -> None:
        """Fire-and-forget, like Worker._notify: a lost alert must never
        block or fail the poll."""

        if self._notifier is None:
            return
        download = await self._db.get_magnet_download(download_id)
        if download is None:
            return
        try:
            await self._notifier.download_trouble(download)
        except Exception:
            _LOGGER.exception("download notification failed id=%s", download_id)


def _task_dir(download_dir: str) -> str:
    return posixpath.join(download_dir, IN_PROGRESS_BUCKET)
