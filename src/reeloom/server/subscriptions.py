"""Subscriptions: search an indexer for a title, grab what matches.

A subscription names a TMDB title and the search that finds its releases —
a query, optionally narrowed to release groups and include/exclude patterns.
Every check runs the query through Prowlarr (which holds the tracker's
credentials and enforces its limits), picks releases not grabbed yet, and
adds each .torrent to the PT client with its own folder under the watch's
inbound root as save path and ``in_progress/<folder>`` as the incomplete
path. The scanner never looks into ``in_progress``, so a folder appears in
the inbound root only once every byte is there; from then on it is an
ordinary run, except that the Agent is told which TMDB entry it belongs to.

Once that run is done the torrent is dropped from the client (its files have
already been moved into the library). The client is download-only: seeding
is configured there, not here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from reeloom.adapters.prowlarr import ProwlarrError, Release
from reeloom.adapters.qbittorrent import QbittorrentClientError
from reeloom.models import (
    Grab,
    GrabState,
    ReeloomError,
    RunState,
    Subscription,
    WatchConfig,
)
from reeloom.release import (
    Filter,
    ReleaseEpisodes,
    coverage,
    folder_for,
    is_covered,
    parse_episodes,
)
from reeloom.scanner import IN_PROGRESS_BUCKET
from reeloom.server.composition import NotConfigured
from reeloom.torrent import TorrentError, magnet_from_torrent
from reeloom.trash import rmdir_if_empty

_LOGGER = logging.getLogger(__name__)

CATEGORY = "reeloom"
TAG = "reeloom"
#: Grabs per check: the indexer's own grab limit is per hour, and a first
#: check with backfill on can match a whole season.
MAX_GRABS_PER_CHECK = 8
TRACK_INTERVAL_SECONDS = 60

GRAB = "grab"


class SubscriptionError(ReeloomError):
    pass


class GrabNotifier(Protocol):
    async def grab_event(
        self,
        subscription_name: str,
        title: str,
        *,
        grabbed: bool,
        error: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class Verdict:
    release: Release
    parsed: ReleaseEpisodes
    verdict: str
    """``grab``, or why not: group / include / exclude / season / grabbed /
    covered / too_old."""

    def to_json(self) -> dict[str, Any]:
        return {
            **self.release.to_json(),
            **self.parsed.to_json(),
            "verdict": self.verdict,
        }


def evaluate(
    subscription: Subscription,
    releases: Sequence[Release],
    grabs: Sequence[Grab],
) -> list[Verdict]:
    """Decide, oldest release first, which ones to grab.

    Pure. A release is grabbed when it passes the filters, was not grabbed
    before (by guid) and holds at least one episode no live grab holds yet;
    the first release published for an episode wins. Failed grabs keep their
    guid (the same release is not retried) but release their episodes.
    """

    seen = {grab.guid for grab in grabs}
    covered: list[tuple[int, int | None]] = []
    for grab in grabs:
        if grab.state is GrabState.FAILED:
            continue
        covered += coverage(ReleaseEpisodes(grab.season, grab.episodes))
    rules = Filter(
        groups=subscription.groups,
        include=subscription.include,
        exclude=subscription.exclude,
        season=subscription.season,
    )
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    ordered = sorted(releases, key=lambda item: item.published_at or epoch)
    verdicts: list[Verdict] = []
    for release in ordered:
        parsed = parse_episodes(release.title)
        reason = rules.rejects(release.title, parsed)
        if reason is None and release.guid in seen:
            reason = "grabbed"
        if reason is None and subscription.ignore_before is not None and (
            release.published_at is None
            or release.published_at < subscription.ignore_before
        ):
            reason = "too_old"
        if reason is None and is_covered(parsed, covered):
            reason = "covered"
        if reason is None:
            seen.add(release.guid)
            covered += coverage(parsed)
        verdicts.append(Verdict(release, parsed, reason or GRAB))
    verdicts.reverse()  # newest first for display
    return verdicts


class SubscriptionService:
    def __init__(self, database, clients, *, notifier: GrabNotifier | None = None):
        self._db = database
        self._clients = clients
        self._notifier = notifier
        self._lock = asyncio.Lock()

    # ---- background loop ----------------------------------------------

    async def run_forever(self) -> None:
        """Its own task beside the run worker, so a slow indexer search never
        holds up archiving."""

        while True:
            try:
                await self.poll()
            except Exception:
                _LOGGER.exception("subscription poll failed")
            await asyncio.sleep(TRACK_INTERVAL_SECONDS)

    async def poll(self) -> None:
        """Track live grabs, then check at most one due subscription.

        One subscription per pass spreads indexer queries over time instead
        of firing them all whenever the interval comes round.
        """

        async with self._lock:
            try:
                await self._track()
            except NotConfigured:
                return
            except ReeloomError as error:
                _LOGGER.warning("grab tracking failed: %s", error.code)
            subscription = await self._next_due()
            if subscription is None:
                return
            await self._check_and_record(subscription)

    async def check_now(self, subscription_id: str) -> list[Grab]:
        """Check one subscription immediately (the UI's 立即检查)."""

        async with self._lock:
            subscription = await self._db.get_subscription(subscription_id)
            if subscription is None:
                raise SubscriptionError("subscription_not_found")
            return await self._check_and_record(subscription, raise_errors=True)

    async def preview(self, subscription: Subscription) -> list[Verdict]:
        """Run the search and show what a check would grab, grabbing nothing."""

        prowlarr = await self._clients.prowlarr()
        releases = await prowlarr.search(
            subscription.query, await self._clients.prowlarr_indexer_ids()
        )
        grabs = (
            await self._db.list_grabs(subscription_id=subscription.id)
            if subscription.id
            else []
        )
        return evaluate(subscription, releases, grabs)

    async def forget(self, grab_id: str) -> None:
        """Forget a grab so its release may be grabbed again.

        A grab still downloading is cancelled first, its partial data deleted
        by the client (an explicit user action, and the data is re-download-
        able). Any other grab only loses its record: the client and the files
        are left alone.
        """

        grab = await self._db.get_grab(grab_id)
        if grab is None:
            raise SubscriptionError("grab_not_found")
        if grab.state is GrabState.DOWNLOADING:
            client = await self._clients.pt_client()
            await client.delete(grab.info_hash, delete_files=True)
        await self._db.delete_grab(grab_id)

    # ---- checking -----------------------------------------------------

    async def _next_due(self) -> Subscription | None:
        minutes = await self._clients.subscription_interval_minutes()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(minutes, 5))
        due = [
            item
            for item in await self._db.list_subscriptions()
            if item.enabled
            and (item.last_checked_at is None or item.last_checked_at <= cutoff)
        ]
        if not due:
            return None
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        return min(due, key=lambda item: item.last_checked_at or epoch)

    async def _check_and_record(
        self, subscription: Subscription, *, raise_errors: bool = False
    ) -> list[Grab]:
        try:
            grabs = await self._check(subscription)
        except NotConfigured:
            if raise_errors:
                raise
            return []
        except ReeloomError as error:
            _LOGGER.warning(
                "subscription=%s check failed: %s", subscription.id, error.code
            )
            await self._db.update_subscription(
                subscription.id,
                {
                    "last_checked_at": datetime.now(timezone.utc),
                    "last_error": error.code,
                },
            )
            if raise_errors:
                raise
            return []
        await self._db.update_subscription(
            subscription.id,
            {"last_checked_at": datetime.now(timezone.utc), "last_error": None},
        )
        return grabs

    async def _check(self, subscription: Subscription) -> list[Grab]:
        config = await self._db.get_config(subscription.config_id)
        if config is None:
            raise SubscriptionError("config_deleted")
        verdicts = await self.preview(subscription)
        wanted = [item for item in verdicts if item.verdict == GRAB]
        wanted.reverse()  # oldest first, as decided
        grabbed: list[Grab] = []
        for item in wanted[:MAX_GRABS_PER_CHECK]:
            try:
                grab = await self._grab(subscription, config, item)
            except ProwlarrError as error:
                if error.code in {"prowlarr_rate_limited", "prowlarr_timeout",
                                  "prowlarr_unreachable"}:
                    # Transient: nothing recorded, the next check tries again.
                    raise
                grab = await self._record_failure(subscription, config, item, error)
            if grab is not None:
                grabbed.append(grab)
        if grabbed:
            _LOGGER.info(
                "subscription=%s grabbed %d release(s)", subscription.id, len(grabbed)
            )
        return grabbed

    async def _taken_folders(self, config: WatchConfig) -> set[str]:
        taken = await self._db.grab_folder_names(config.id)
        root = Path(config.inbound_root)
        for base in (root, root / IN_PROGRESS_BUCKET):
            try:
                taken |= set(await asyncio.to_thread(os.listdir, base))
            except FileNotFoundError:
                pass
        return taken

    async def _grab(
        self, subscription: Subscription, config: WatchConfig, item: Verdict
    ) -> Grab | None:
        prowlarr = await self._clients.prowlarr()
        client = await self._clients.pt_client()
        data = await prowlarr.fetch_torrent(item.release.download_url)
        try:
            torrent = magnet_from_torrent(data)
        except TorrentError as error:
            raise ProwlarrError("prowlarr_not_a_torrent", detail=error.code) from error

        folder = folder_for(item.release.title, await self._taken_folders(config))
        root = Path(config.inbound_root)
        grab = Grab(
            id=str(uuid.uuid4()),
            subscription_id=subscription.id,
            config_id=config.id,
            guid=item.release.guid,
            title=item.release.title,
            folder_name=folder,
            info_hash=torrent.info_hash,
            state=GrabState.DOWNLOADING,
            season=item.parsed.season,
            episodes=item.parsed.episodes,
            size_bytes=item.release.size_bytes,
        )
        # Recorded before the client sees it: a crash in between leaves a
        # grab the tracker resolves (present → tracked, absent → failed)
        # instead of a torrent nobody knows about.
        if not await self._db.create_grab(grab):
            return None
        try:
            await client.ensure_category(CATEGORY)
            await client.add_torrent(
                data,
                filename=f"{torrent.info_hash.lower()}.torrent",
                save_path=str(root / folder),
                download_path=str(root / IN_PROGRESS_BUCKET / folder),
                category=CATEGORY,
                tags=TAG,
            )
        except QbittorrentClientError as error:
            await self._db.update_grab(
                grab.id, {"state": GrabState.FAILED, "error": error.code}
            )
            await self._notify(subscription, grab.title, error=error.code)
            if error.code in {"pt_client_unreachable", "pt_client_timeout",
                              "pt_client_login_failed", "pt_client_banned"}:
                raise
            return None
        await self._notify(subscription, grab.title)
        return grab

    async def _record_failure(
        self,
        subscription: Subscription,
        config: WatchConfig,
        item: Verdict,
        error: ReeloomError,
    ) -> Grab | None:
        grab = Grab(
            id=str(uuid.uuid4()),
            subscription_id=subscription.id,
            config_id=config.id,
            guid=item.release.guid,
            title=item.release.title,
            folder_name="",
            info_hash="",
            state=GrabState.FAILED,
            season=item.parsed.season,
            episodes=item.parsed.episodes,
            size_bytes=item.release.size_bytes,
            error=error.code,
        )
        if await self._db.create_grab(grab):
            await self._db.update_grab(
                grab.id, {"state": GrabState.FAILED, "error": error.code}
            )
            await self._notify(subscription, grab.title, error=error.code)
        return None

    async def _notify(
        self, subscription: Subscription, title: str, *, error: str | None = None
    ) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.grab_event(
                subscription.name, title, grabbed=error is None, error=error
            )
        except Exception:  # noqa: BLE001 - a notification never breaks a grab
            _LOGGER.exception("grab notification failed")

    # ---- tracking -----------------------------------------------------

    async def _track(self) -> None:
        live = await self._db.list_grabs(
            states=[GrabState.DOWNLOADING, GrabState.COMPLETED]
        )
        if not live:
            return
        client = await self._clients.pt_client()
        statuses = await client.torrents([grab.info_hash for grab in live])
        configs: dict[str, WatchConfig | None] = {}
        for grab in live:
            if grab.config_id not in configs:
                configs[grab.config_id] = await self._db.get_config(grab.config_id)
            config = configs[grab.config_id]
            if config is None:
                continue
            status = statuses.get(grab.info_hash)
            if grab.state is GrabState.DOWNLOADING:
                await self._track_download(grab, config, status)
            else:
                await self._track_import(grab, config, status is not None)

    async def _track_download(self, grab: Grab, config: WatchConfig, status) -> None:
        folder = Path(config.inbound_root) / grab.folder_name
        if status is None:
            if await asyncio.to_thread(folder.is_dir):
                # Finished and then removed from the client by hand.
                await self._db.update_grab(
                    grab.id, {"state": GrabState.COMPLETED, "progress": 1.0}
                )
            else:
                await self._db.update_grab(
                    grab.id, {"state": GrabState.FAILED, "error": "torrent_missing"}
                )
                await self._notify_by_id(grab, "torrent_missing")
            return
        if status.errored:
            await self._db.update_grab(
                grab.id, {"state": GrabState.FAILED, "error": status.state}
            )
            await self._notify_by_id(grab, status.state)
            return
        if status.complete:
            await self._db.update_grab(
                grab.id, {"state": GrabState.COMPLETED, "progress": 1.0}
            )
            return
        if abs(status.progress - grab.progress) >= 0.001:
            await self._db.update_grab(grab.id, {"progress": status.progress})

    async def _track_import(
        self, grab: Grab, config: WatchConfig, in_client: bool
    ) -> None:
        run = await self._db.latest_run_for_folder(config.id, grab.folder_name)
        if run is None:
            return
        if run.state is not RunState.DONE:
            if grab.run_id != run.id:
                await self._db.update_grab(grab.id, {"run_id": run.id})
            return
        if in_client:
            client = await self._clients.pt_client()
            # The files were moved into the library; the client only forgets.
            await client.delete(grab.info_hash, delete_files=False)
        await asyncio.to_thread(
            _remove_empty_tree,
            Path(config.inbound_root) / IN_PROGRESS_BUCKET / grab.folder_name,
        )
        await self._db.update_grab(
            grab.id, {"state": GrabState.IMPORTED, "run_id": run.id}
        )

    async def _notify_by_id(self, grab: Grab, error: str) -> None:
        subscription = await self._db.get_subscription(grab.subscription_id)
        if subscription is not None:
            await self._notify(subscription, grab.title, error=error)


def _remove_empty_tree(folder: Path) -> None:
    if not folder.is_dir() or folder.is_symlink():
        return
    for current, directories, _ in os.walk(folder, topdown=False, followlinks=False):
        for name in directories:
            rmdir_if_empty(Path(current) / name)
    rmdir_if_empty(folder)
