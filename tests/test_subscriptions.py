from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from reeloom.adapters.prowlarr import ProwlarrError, Release
from reeloom.adapters.qbittorrent import TorrentStatus
from reeloom.models import (
    Grab,
    GrabState,
    MediaType,
    Run,
    RunState,
    Subscription,
    WatchConfig,
)
from reeloom.server.subscriptions import GRAB, SubscriptionService, evaluate
from tests.test_torrent import bencode

NOW = datetime(2026, 11, 26, 7, 0, tzinfo=timezone.utc)


def release(title: str, *, hours_ago: float = 1.0, guid: str | None = None) -> Release:
    return Release(
        guid=guid or f"guid-{title}",
        title=title,
        size_bytes=1000,
        indexer_id=5,
        indexer="M-Team - TP",
        download_url=f"http://127.0.0.1:9696/5/download?link={uuid.uuid4().hex}",
        info_url="",
        published_at=NOW - timedelta(hours=hours_ago),
        seeders=1,
        flags=(),
    )


def torrent(name: str) -> bytes:
    return bencode(
        {
            "info": {
                "name": name,
                "piece length": 16384,
                "pieces": uuid.uuid4().bytes + b"\x00" * 4,
                "length": 10,
            }
        }
    )


SUB = Subscription(
    id="sub-1",
    name="后会友期",
    config_id="cfg-1",
    tmdb_id=338938,
    query="后会友期",
    season=1,
    groups=("HHWEB",),
)


def grab(**kwargs: Any) -> Grab:
    values: dict[str, Any] = dict(
        id=str(uuid.uuid4()),
        subscription_id="sub-1",
        config_id="cfg-1",
        guid="g",
        title="t",
        folder_name="f",
        info_hash="H",
        state=GrabState.DOWNLOADING,
        season=1,
        episodes=(1,),
    )
    values.update(kwargs)
    return Grab(**values)


def test_first_release_per_episode_wins_and_filters_apply() -> None:
    releases = [
        release("I Miss You S01 E01-E02 2026 1080p WEB-DL-HHWEB", hours_ago=50),
        release("I Miss You S01E03 2026 1080p WEB-DL-HHWEB", hours_ago=40),
        release("I Miss You S01E03 REPACK 2026 1080p WEB-DL-HHWEB", hours_ago=30),
        release("I Miss You S01E03 2026 2160p MGTV WEB-DL-ADWeb", hours_ago=45),
        release("I Miss You S02E01 2026 1080p WEB-DL-HHWEB", hours_ago=10),
        release("I Miss You S01E04 2026 1080p WEB-DL-HHWEB", hours_ago=5),
    ]
    existing = [grab(guid="old", episodes=(4,))]
    verdicts = {item.release.title: item.verdict for item in evaluate(SUB, releases, existing)}
    assert verdicts == {
        "I Miss You S01 E01-E02 2026 1080p WEB-DL-HHWEB": GRAB,
        "I Miss You S01E03 2026 1080p WEB-DL-HHWEB": GRAB,
        "I Miss You S01E03 REPACK 2026 1080p WEB-DL-HHWEB": "covered",
        "I Miss You S01E03 2026 2160p MGTV WEB-DL-ADWeb": "group",
        "I Miss You S02E01 2026 1080p WEB-DL-HHWEB": "season",
        "I Miss You S01E04 2026 1080p WEB-DL-HHWEB": "covered",
    }


def test_failed_grab_keeps_its_guid_but_frees_its_episodes() -> None:
    first = release("Show S01E01 1080p-HHWEB", guid="a", hours_ago=3)
    second = release("Show S01E01 v2 1080p-HHWEB", guid="b", hours_ago=2)
    failed = grab(guid="a", state=GrabState.FAILED, episodes=(1,))
    verdicts = {v.release.guid: v.verdict for v in evaluate(SUB, [first, second], [failed])}
    assert verdicts == {"a": "grabbed", "b": GRAB}


def test_without_backfill_only_newer_releases_are_grabbed() -> None:
    sub = replace(SUB, ignore_before=NOW - timedelta(hours=2))
    old = release("Show S01E01 1080p-HHWEB", hours_ago=3)
    new = release("Show S01E02 1080p-HHWEB", hours_ago=1)
    verdicts = {v.release.title: v.verdict for v in evaluate(sub, [old, new], [])}
    assert verdicts == {old.title: "too_old", new.title: GRAB}


# ---- service -----------------------------------------------------------


class FakeDb:
    def __init__(self, config: WatchConfig) -> None:
        self.config = config
        self.subscriptions: dict[str, Subscription] = {}
        self.grabs: dict[str, Grab] = {}
        self.runs: list[Run] = []

    async def get_config(self, config_id: str):
        return self.config if config_id == self.config.id else None

    async def list_subscriptions(self):
        return list(self.subscriptions.values())

    async def get_subscription(self, subscription_id: str):
        return self.subscriptions.get(subscription_id)

    async def update_subscription(self, subscription_id: str, values):
        self.subscriptions[subscription_id] = replace(
            self.subscriptions[subscription_id], **values
        )

    async def create_grab(self, item: Grab) -> bool:
        if any(
            g.subscription_id == item.subscription_id and g.guid == item.guid
            for g in self.grabs.values()
        ):
            return False
        self.grabs[item.id] = item
        return True

    async def get_grab(self, grab_id: str):
        return self.grabs.get(grab_id)

    async def list_grabs(self, *, subscription_id=None, states=None, limit=500):
        return [
            g
            for g in self.grabs.values()
            if (subscription_id is None or g.subscription_id == subscription_id)
            and (not states or g.state in states)
        ]

    async def update_grab(self, grab_id: str, values):
        self.grabs[grab_id] = replace(self.grabs[grab_id], **values)

    async def delete_grab(self, grab_id: str):
        self.grabs.pop(grab_id, None)

    async def grab_folder_names(self, config_id: str):
        return {g.folder_name for g in self.grabs.values()}

    async def latest_run_for_folder(self, config_id: str, folder_name: str):
        matches = [run for run in self.runs if run.folder_name == folder_name]
        return matches[-1] if matches else None


class FakeProwlarr:
    def __init__(self, releases, torrents) -> None:
        self.releases = releases
        self.torrents = torrents
        self.searches: list[str] = []
        self.fail_download: str | None = None

    async def search(self, query, indexer_ids):
        self.searches.append(query)
        return list(self.releases)

    async def fetch_torrent(self, url):
        if self.fail_download:
            raise ProwlarrError(self.fail_download)
        return self.torrents[url]


class FakePtClient:
    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []
        self.statuses: dict[str, TorrentStatus] = {}
        self.deleted: list[tuple[str, bool]] = []

    async def ensure_category(self, category):
        pass

    async def add_torrent(self, data, **fields):
        self.added.append(fields)

    async def torrents(self, hashes):
        return {h: self.statuses[h] for h in hashes if h in self.statuses}

    async def delete(self, info_hash, *, delete_files):
        self.deleted.append((info_hash, delete_files))
        self.statuses.pop(info_hash, None)


class Clients:
    def __init__(self, prowlarr, client) -> None:
        self._prowlarr = prowlarr
        self._client = client

    async def prowlarr(self):
        return self._prowlarr

    async def prowlarr_indexer_ids(self):
        return (5,)

    async def pt_client(self):
        return self._client

    async def subscription_interval_minutes(self):
        return 30


class Notes:
    def __init__(self) -> None:
        self.events: list[tuple[str, bool, str | None]] = []

    async def grab_event(self, name, title, *, grabbed, error=None):
        self.events.append((title, grabbed, error))


@pytest.fixture
def setup(tmp_path: Path):
    inbound = tmp_path / "pt" / "tv"
    inbound.mkdir(parents=True)
    config = WatchConfig(
        id="cfg-1",
        name="pt-tv",
        inbound_root=str(inbound),
        library_root=str(tmp_path / "library"),
        media_type=MediaType.TV,
        cross_filesystem=True,
    )
    database = FakeDb(config)
    database.subscriptions[SUB.id] = SUB
    releases = [
        release("I Miss You S01E01 2026 1080p WEB-DL-HHWEB", hours_ago=3),
        release("I Miss You S01E02 2026 1080p WEB-DL-HHWEB", hours_ago=2),
    ]
    prowlarr = FakeProwlarr(
        releases, {item.download_url: torrent(item.title) for item in releases}
    )
    client = FakePtClient()
    notes = Notes()
    service = SubscriptionService(database, Clients(prowlarr, client), notifier=notes)
    return service, database, prowlarr, client, notes, inbound


async def test_poll_grabs_into_own_folders_and_records_the_check(setup) -> None:
    service, database, prowlarr, client, notes, inbound = setup
    await service.poll()

    assert prowlarr.searches == ["后会友期"]
    assert [item["save_path"] for item in client.added] == [
        str(inbound / "I Miss You S01E01 2026 1080p WEB-DL-HHWEB"),
        str(inbound / "I Miss You S01E02 2026 1080p WEB-DL-HHWEB"),
    ]
    assert client.added[0]["download_path"] == str(
        inbound / "in_progress" / "I Miss You S01E01 2026 1080p WEB-DL-HHWEB"
    )
    assert {g.state for g in database.grabs.values()} == {GrabState.DOWNLOADING}
    assert database.subscriptions[SUB.id].last_checked_at is not None
    assert [grabbed for _, grabbed, _ in notes.events] == [True, True]

    # Not due again within the interval: no second search.
    await service.poll()
    assert prowlarr.searches == ["后会友期"]

    # A manual check finds nothing new.
    assert await service.check_now(SUB.id) == []


async def test_a_completed_download_is_dropped_from_the_client_once_archived(
    setup,
) -> None:
    service, database, _, client, _, inbound = setup
    await service.poll()
    first, second = sorted(database.grabs.values(), key=lambda g: g.title)
    for item in (first, second):
        client.statuses[item.info_hash] = TorrentStatus(
            item.info_hash, item.title, "downloading", 0.5, 10, ""
        )
    client.statuses[first.info_hash] = TorrentStatus(
        first.info_hash, first.title, "stoppedUP", 1.0, 0, ""
    )
    await service.poll()
    assert database.grabs[first.id].state is GrabState.COMPLETED
    assert database.grabs[second.id].progress == 0.5

    # The run for the folder is still going: nothing happens to the torrent.
    database.runs.append(
        Run("run-1", "cfg-1", first.folder_name, RunState.EXECUTING)
    )
    await service.poll()
    assert database.grabs[first.id].run_id == "run-1"
    assert client.deleted == []

    (inbound / "in_progress" / first.folder_name / "sub").mkdir(parents=True)
    database.runs[-1] = replace(database.runs[-1], state=RunState.DONE)
    await service.poll()
    assert database.grabs[first.id].state is GrabState.IMPORTED
    assert client.deleted == [(first.info_hash, False)]
    assert not (inbound / "in_progress" / first.folder_name).exists()


async def test_vanished_torrent_without_folder_fails_and_errors_fail(setup) -> None:
    service, database, _, client, notes, inbound = setup
    await service.poll()
    first, second = sorted(database.grabs.values(), key=lambda g: g.title)
    client.statuses[second.info_hash] = TorrentStatus(
        second.info_hash, second.title, "missingFiles", 0.2, 5, ""
    )
    await service.poll()
    assert database.grabs[first.id].state is GrabState.FAILED
    assert database.grabs[first.id].error == "torrent_missing"
    assert database.grabs[second.id].error == "missingFiles"
    assert notes.events[-1][1] is False


async def test_forget_cancels_a_running_download(setup) -> None:
    service, database, _, client, _, _ = setup
    await service.poll()
    item = next(iter(database.grabs.values()))
    await service.forget(item.id)
    assert client.deleted == [(item.info_hash, True)]
    assert item.id not in database.grabs


async def test_rate_limit_records_nothing_and_marks_the_error(setup) -> None:
    service, database, prowlarr, client, _, _ = setup
    prowlarr.fail_download = "prowlarr_rate_limited"
    await service.poll()
    assert database.grabs == {}
    assert database.subscriptions[SUB.id].last_error == "prowlarr_rate_limited"

    prowlarr.fail_download = "prowlarr_not_a_torrent"
    await service.check_now(SUB.id)
    assert {g.state for g in database.grabs.values()} == {GrabState.FAILED}
    assert client.added == []
