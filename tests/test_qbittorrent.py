"""The qBittorrent-compatible API, driven the way Radarr/Sonarr drive it."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from reeloom.adapters.clouddrive import OfflineStatus
from reeloom.models import DownloadState
from reeloom.server.api import create_app
from reeloom.server.composition import QbittorrentConfig
from reeloom.server.downloads import DownloadService
from tests.fakes import FakeCloudDrive, FakeDatabase, StubDownloadClients
from tests.test_torrent import INFO, bencode

TOKEN = "test-admin-token-1234567890"
PASSWORD = "qbit-password-123"
ROOT = "/arr"
HASH = "C9E15763F722F23E98A29DECDFAE341B98D53056"
MAGNET = f"magnet:?xt=urn:btih:{HASH.lower()}&dn=Movie.2024.1080p"


class Wakes:
    def __init__(self) -> None:
        self.count = 0

    def wake(self) -> None:
        self.count += 1

    def intake_status(self) -> list:
        return []


@pytest.fixture
def cloud(tmp_path: Path) -> FakeCloudDrive:
    (tmp_path / "arr").mkdir()
    (tmp_path / "manual").mkdir()
    return FakeCloudDrive(tmp_path)


@pytest.fixture
def clients(cloud: FakeCloudDrive) -> StubDownloadClients:
    return StubDownloadClients(
        cloud, qbittorrent=QbittorrentConfig(password=PASSWORD, save_root=ROOT)
    )


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def service(database, clients) -> DownloadService:
    return DownloadService(database, clients)


@pytest.fixture
def wakes() -> Wakes:
    return Wakes()


@pytest_asyncio.fixture
async def anonymous(database, clients, service, wakes):
    app = create_app(
        database=database,
        admin_token=TOKEN,
        worker=wakes,
        downloads=service,
        clients=clients,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def qbit(anonymous):
    response = await anonymous.post(
        "/api/v2/auth/login", data={"username": "radarr", "password": PASSWORD}
    )
    assert response.text == "Ok."
    assert "SID" in anonymous.cookies
    return anonymous


async def add_magnet(qbit, category: str = "radarr", url: str = MAGNET) -> str:
    response = await qbit.post(
        "/api/v2/torrents/add", data={"urls": url, "category": category}
    )
    return response.text


async def finish(cloud: FakeCloudDrive, service: DownloadService, tmp_path: Path):
    """CloudDrive reports the task finished with its folder in place; one
    poll moves it out of in_progress."""

    folder = tmp_path / "arr/radarr/in_progress/Movie.2024.1080p"
    folder.mkdir(parents=True)
    (folder / "movie.mkv").write_bytes(b"x" * 32)
    (folder / "movie.nfo").write_bytes(b"x")
    cloud.script_task(
        HASH,
        name="Movie.2024.1080p",
        status=OfflineStatus.FINISHED,
        progress=100,
        size=33,
    )
    await service.poll()


# ---- auth ---------------------------------------------------------------


async def test_unauthenticated_calls_get_403(anonymous) -> None:
    # Radarr probes webapiVersion without a session and reads 403 as
    # "API v2 is here, log in first".
    for path in (
        "/api/v2/app/webapiVersion",
        "/api/v2/torrents/info",
        "/api/v2/app/preferences",
    ):
        assert (await anonymous.get(path)).status_code == 403
    response = await anonymous.post("/api/v2/torrents/add", data={"urls": MAGNET})
    assert response.status_code == 403


async def test_wrong_password_fails_login(anonymous) -> None:
    response = await anonymous.post(
        "/api/v2/auth/login", data={"username": "radarr", "password": "nope"}
    )
    assert response.status_code == 200
    assert response.text == "Fails."
    assert "SID" not in anonymous.cookies


async def test_session_cookie_authorizes(qbit) -> None:
    assert (await qbit.get("/api/v2/app/webapiVersion")).text == "2.9.3"
    assert (await qbit.get("/api/v2/app/version")).text == "v4.6.7"


async def test_bearer_and_basic_carry_the_password(anonymous) -> None:
    bearer = await anonymous.get(
        "/api/v2/app/webapiVersion",
        headers={"Authorization": f"Bearer {PASSWORD}"},
    )
    assert bearer.status_code == 200
    basic = base64.b64encode(f"radarr:{PASSWORD}".encode()).decode()
    response = await anonymous.get(
        "/api/v2/app/webapiVersion", headers={"Authorization": f"Basic {basic}"}
    )
    assert response.status_code == 200
    wrong = await anonymous.get(
        "/api/v2/app/webapiVersion", headers={"Authorization": "Bearer nope"}
    )
    assert wrong.status_code == 403


async def test_changing_the_password_closes_sessions(qbit, clients) -> None:
    clients.qbittorrent_config = QbittorrentConfig(
        password="another-password", save_root=ROOT
    )
    assert (await qbit.get("/api/v2/app/webapiVersion")).status_code == 403


async def test_unconfigured_api_stays_closed(anonymous, clients) -> None:
    clients.qbittorrent_config = None
    response = await anonymous.post(
        "/api/v2/auth/login", data={"username": "x", "password": PASSWORD}
    )
    assert response.text == "Fails."
    assert (await anonymous.get("/api/v2/app/webapiVersion")).status_code == 403


async def test_admin_token_does_not_open_the_qbittorrent_api(anonymous) -> None:
    response = await anonymous.get(
        "/api/v2/app/webapiVersion", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 403


# ---- application ----------------------------------------------------------


async def test_preferences_make_finished_torrents_done_seeding(qbit) -> None:
    prefs = (await qbit.get("/api/v2/app/preferences")).json()
    assert prefs["save_path"] == ROOT
    # Radarr: ratio limit -2 on the torrent defers to this global limit of 0,
    # which a ratio of 0 has reached; action 0 (pause) is not "removes".
    assert prefs["max_ratio_enabled"] is True
    assert prefs["max_ratio"] == 0
    assert prefs["max_ratio_act"] == 0
    assert prefs["dht"] is True
    assert prefs["queueing_enabled"] is False


# ---- categories -------------------------------------------------------------


async def test_categories_are_folders_under_the_save_root(qbit, tmp_path) -> None:
    assert (await qbit.get("/api/v2/torrents/categories")).json() == {}
    response = await qbit.post(
        "/api/v2/torrents/createCategory", data={"category": "radarr"}
    )
    assert response.status_code == 200
    assert (tmp_path / "arr/radarr").is_dir()
    (tmp_path / "arr/in_progress").mkdir()
    (tmp_path / "arr/.hidden").mkdir()

    categories = (await qbit.get("/api/v2/torrents/categories")).json()
    assert categories == {"radarr": {"name": "radarr", "savePath": "/arr/radarr"}}


@pytest.mark.parametrize("name", ["", "in_progress", "../x", "a/b", ".hidden"])
async def test_unsafe_category_names_are_refused(qbit, name) -> None:
    response = await qbit.post(
        "/api/v2/torrents/createCategory", data={"category": name}
    )
    assert response.status_code == 400


# ---- add ----------------------------------------------------------------------


async def test_add_magnet_submits_into_the_category(
    qbit, cloud, database, wakes
) -> None:
    assert await add_magnet(qbit) == "Ok."
    assert cloud.added == [([MAGNET], "/arr/radarr/in_progress")]
    assert wakes.count == 1

    info = (await qbit.get("/api/v2/torrents/info?category=radarr")).json()
    assert len(info) == 1
    torrent = info[0]
    assert torrent["hash"] == HASH.lower()
    assert torrent["name"] == "Movie.2024.1080p"
    assert torrent["state"] == "metaDL"
    assert torrent["category"] == "radarr"
    assert torrent["save_path"] == "/arr/radarr"
    assert (await qbit.get("/api/v2/torrents/info?category=sonarr")).json() == []


async def test_add_without_category_uses_the_save_root(qbit, cloud) -> None:
    assert await add_magnet(qbit, category="") == "Ok."
    assert cloud.added == [([MAGNET], "/arr/in_progress")]
    info = (await qbit.get("/api/v2/torrents/info")).json()
    assert [item["category"] for item in info] == [""]


async def test_add_never_fetches_urls(qbit, cloud) -> None:
    text = await add_magnet(qbit, url="http://indexer.example/file.torrent")
    assert text == "Fails."
    assert cloud.added == []


async def test_add_torrent_file_submits_its_magnet(qbit, cloud) -> None:
    data = bencode({"announce": "udp://t.example/announce", "info": INFO})
    response = await qbit.post(
        "/api/v2/torrents/add",
        data={"category": "radarr"},
        files={"torrents": ("movie.torrent", data, "application/x-bittorrent")},
    )
    assert response.text == "Ok."
    [(urls, directory)] = cloud.added
    assert directory == "/arr/radarr/in_progress"
    assert urls[0].startswith("magnet:?xt=urn:btih:")
    assert "tr=udp%3A%2F%2Ft.example%2Fannounce" in urls[0]


async def test_private_torrents_are_refused(qbit, cloud) -> None:
    data = bencode({"info": {**INFO, "private": 1}})
    response = await qbit.post(
        "/api/v2/torrents/add",
        data={"category": "radarr"},
        files={"torrents": ("pt.torrent", data, "application/x-bittorrent")},
    )
    assert response.text == "Fails."
    assert cloud.added == []


async def test_re_adding_the_same_download_is_idempotent(qbit, cloud) -> None:
    assert await add_magnet(qbit) == "Ok."
    assert await add_magnet(qbit) == "Ok."
    assert len(cloud.added) == 1


async def test_hash_held_by_a_manual_download_fails(qbit, service, cloud) -> None:
    await service.submit(MAGNET, "/manual")
    assert await add_magnet(qbit) == "Fails."


# ---- progress and completion ---------------------------------------------------


async def test_manual_downloads_stay_invisible(qbit, service) -> None:
    await service.submit(MAGNET, "/manual")
    assert (await qbit.get("/api/v2/torrents/info")).json() == []
    response = await qbit.get(f"/api/v2/torrents/properties?hash={HASH.lower()}")
    assert response.status_code == 404


async def test_downloading_progress_is_a_fraction(qbit, cloud, service) -> None:
    await add_magnet(qbit)
    cloud.script_task(
        HASH,
        name="Movie.2024.1080p",
        status=OfflineStatus.DOWNLOADING,
        progress=40,
        size=1000,
    )
    await service.poll()
    [torrent] = (await qbit.get("/api/v2/torrents/info")).json()
    assert torrent["state"] == "downloading"
    assert torrent["progress"] == pytest.approx(0.4)
    assert torrent["amount_left"] == 600
    assert torrent["eta"] == 8640000


async def test_finished_download_reports_paused_up_at_its_folder(
    qbit, cloud, service, tmp_path
) -> None:
    await add_magnet(qbit)
    await finish(cloud, service, tmp_path)

    [torrent] = (await qbit.get("/api/v2/torrents/info?category=radarr")).json()
    assert torrent["state"] == "pausedUP"
    assert torrent["progress"] == 1
    assert torrent["save_path"] == "/arr/radarr"
    # Radarr demands content_path != save_path for a finished torrent.
    assert torrent["content_path"] == "/arr/radarr/Movie.2024.1080p"
    assert torrent["ratio"] == 0
    assert torrent["ratio_limit"] == -2
    assert (tmp_path / "arr/radarr/Movie.2024.1080p/movie.mkv").is_file()

    properties = (
        await qbit.get(f"/api/v2/torrents/properties?hash={HASH.lower()}")
    ).json()
    assert properties["save_path"] == "/arr/radarr"
    files = (await qbit.get(f"/api/v2/torrents/files?hash={HASH.lower()}")).json()
    assert sorted(item["name"] for item in files) == [
        "Movie.2024.1080p/movie.mkv",
        "Movie.2024.1080p/movie.nfo",
    ]


async def test_failed_and_stalled_downloads_surface(qbit, cloud, service, database):
    await add_magnet(qbit)
    cloud.script_task(HASH, name="Movie", status=OfflineStatus.ERROR)
    await service.poll()
    [torrent] = (await qbit.get("/api/v2/torrents/info")).json()
    assert torrent["state"] == "error"
    assert torrent["error"] == "clouddrive_reported_error"

    [row] = database.downloads.values()
    await database.transition_download(
        row.id, expected=[DownloadState.FAILED], target=DownloadState.STALLED
    )
    [torrent] = (await qbit.get("/api/v2/torrents/info")).json()
    assert torrent["state"] == "stalledDL"


# ---- delete --------------------------------------------------------------------


async def test_delete_unfinished_drops_the_task(qbit, cloud, database) -> None:
    await add_magnet(qbit)
    response = await qbit.post(
        "/api/v2/torrents/delete",
        data={"hashes": HASH.lower(), "deleteFiles": "true"},
    )
    assert response.status_code == 200
    assert cloud.removed == [([HASH], "/arr/radarr", True)]
    [row] = database.downloads.values()
    assert row.state is DownloadState.REMOVED
    assert (await qbit.get("/api/v2/torrents/info")).json() == []


async def test_delete_without_delete_files_keeps_the_data(qbit, cloud) -> None:
    await add_magnet(qbit)
    await qbit.post("/api/v2/torrents/delete", data={"hashes": HASH.lower()})
    assert cloud.removed == [([HASH], "/arr/radarr", False)]


async def test_delete_finished_only_forgets_the_row(
    qbit, cloud, service, database, tmp_path
) -> None:
    await add_magnet(qbit)
    await finish(cloud, service, tmp_path)
    await qbit.post(
        "/api/v2/torrents/delete",
        data={"hashes": HASH.lower(), "deleteFiles": "true"},
    )
    # CloudDrive is never asked to delete a finished download's data: the
    # client may already have moved those files into its library.
    assert cloud.removed == []
    assert database.downloads == {}
    assert (tmp_path / "arr/radarr/Movie.2024.1080p").is_dir()


async def test_delete_never_reaches_manual_downloads(qbit, service, cloud) -> None:
    await service.submit(MAGNET, "/manual")
    await qbit.post(
        "/api/v2/torrents/delete", data={"hashes": "all", "deleteFiles": "true"}
    )
    assert cloud.removed == []


# ---- the rest of the surface -------------------------------------------------------


async def test_priority_calls_answer_like_queueing_is_off(qbit) -> None:
    response = await qbit.post("/api/v2/torrents/topPrio", data={"hashes": HASH})
    assert response.status_code == 409


async def test_tuning_calls_are_accepted(qbit) -> None:
    for action in ("setShareLimits", "setForceStart", "setCategory", "addTags"):
        response = await qbit.post(f"/api/v2/torrents/{action}", data={"hashes": HASH})
        assert response.status_code == 200


async def test_settings_expose_and_validate_the_save_root(qbit) -> None:
    auth = {"Authorization": f"Bearer {TOKEN}"}
    response = await qbit.put(
        "/api/settings",
        headers=auth,
        json={"qbittorrent_save_root": "/arr/", "qbittorrent_password": PASSWORD},
    )
    assert response.status_code == 200
    body = (await qbit.get("/api/settings", headers=auth)).json()
    assert body["qbittorrent_save_root"] == "/arr"
    assert body["qbittorrent_password_set"] is True
    assert "qbittorrent_password" not in body

    bad = await qbit.put(
        "/api/settings", headers=auth, json={"qbittorrent_save_root": "relative"}
    )
    assert bad.status_code == 422
    short = await qbit.put(
        "/api/settings", headers=auth, json={"qbittorrent_password": "short"}
    )
    assert short.status_code == 422
