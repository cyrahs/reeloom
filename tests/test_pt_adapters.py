from __future__ import annotations

import httpx
import pytest

from reeloom.adapters.prowlarr import ProwlarrClient, ProwlarrError
from reeloom.adapters.qbittorrent import QbittorrentClient, QbittorrentClientError

KEY = "prowlarr-secret-key"

RESULT = {
    "guid": "https://kp.m-team.cc/detail/1",
    "title": "Show S01E01 1080p-HHWEB",
    "size": 123,
    "indexerId": 5,
    "indexer": "M-Team - TP",
    "publishDate": "2026-11-26T06:05:58Z",
    "downloadUrl": f"http://127.0.0.1:9696/5/download?apikey={KEY}&link=abc&file=x",
    "infoUrl": "https://kp.m-team.cc/detail/1",
    "indexerFlags": ["halfleech"],
    "seeders": 3,
}


async def test_prowlarr_search_and_download_reanchored_on_configured_host() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/search":
            other = {**RESULT, "guid": "g2", "indexerId": 9}
            return httpx.Response(200, json=[RESULT, other, {"title": "junk"}])
        if request.url.path == "/5/download":
            return httpx.Response(200, content=b"d4:infod4:name1:xee")
        return httpx.Response(404)

    client = ProwlarrClient(
        "http://prowlarr.media.svc:9696/", KEY, transport=httpx.MockTransport(handler)
    )
    releases = await client.search("后会友期", (5,))
    assert [item.guid for item in releases] == [RESULT["guid"]]
    release = releases[0]
    assert release.published_at is not None and release.published_at.year == 2026
    assert KEY not in str(release.to_json())

    data = await client.fetch_torrent(release.download_url)
    assert data.startswith(b"d")
    download = seen[-1]
    assert download.url.host == "prowlarr.media.svc"
    assert download.url.params["link"] == "abc"
    assert seen[0].headers["X-Api-Key"] == KEY
    assert seen[0].url.params.get_list("indexerIds") == ["5"]
    await client.aclose()


async def test_prowlarr_refuses_non_download_links_and_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "magnet:?xt=urn:btih:x"})

    client = ProwlarrClient(
        "http://prowlarr:9696", KEY, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ProwlarrError) as raised:
        await client.fetch_torrent("http://evil.example/steal")
    assert raised.value.code == "prowlarr_unexpected_link"
    with pytest.raises(ProwlarrError) as raised:
        await client.fetch_torrent("http://127.0.0.1:9696/5/download?link=x")
    assert raised.value.code == "prowlarr_redirected_download"
    await client.aclose()


async def test_prowlarr_rate_limit_is_a_stable_error() -> None:
    client = ProwlarrClient(
        "http://prowlarr:9696",
        KEY,
        transport=httpx.MockTransport(lambda request: httpx.Response(429)),
    )
    with pytest.raises(ProwlarrError) as raised:
        await client.search("x", ())
    assert raised.value.code == "prowlarr_rate_limited"
    await client.aclose()


class FakeQbit:
    def __init__(self, *, modern: bool = False) -> None:
        # modern: qBittorrent 5.x, which answers a good login with 204 and
        # an empty body, a bad one with 401, and add/delete with 204.
        self.modern = modern
        self.logins = 0
        self.expire_next = False
        self.added: list[dict[str, str]] = []
        self.deleted: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/auth/login":
            self.logins += 1
            body = request.content.decode()
            if "password=right" in body:
                if self.modern:
                    return httpx.Response(
                        204, headers={"set-cookie": "SID=abc; path=/"}
                    )
                return httpx.Response(
                    200, text="Ok.", headers={"set-cookie": "SID=abc; path=/"}
                )
            if self.modern:
                return httpx.Response(401, text="Fails.")
            return httpx.Response(200, text="Fails.")
        if request.headers.get("cookie") != "SID=abc":
            return httpx.Response(403)
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(403)
        if path == "/api/v2/torrents/createCategory":
            return httpx.Response(409)
        if path == "/api/v2/torrents/add":
            self.added.append({"body": request.content.decode("latin-1")})
            if self.modern:
                return httpx.Response(204)
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/torrents/info":
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": "abcd",
                        "name": "x",
                        "state": "stoppedUP",
                        "progress": 1,
                        "amount_left": 0,
                        "save_path": "/pt/x",
                    }
                ],
            )
        if path == "/api/v2/torrents/delete":
            self.deleted.append({"body": request.content.decode()})
            return httpx.Response(204 if self.modern else 200)
        return httpx.Response(404)


@pytest.mark.parametrize("modern", [False, True], ids=["4.x", "5.x"])
async def test_qbittorrent_client_logs_in_once_and_again_after_expiry(
    modern: bool,
) -> None:
    fake = FakeQbit(modern=modern)
    client = QbittorrentClient(
        "http://qbittorrent-pt.media.svc", "admin", "right",
        transport=httpx.MockTransport(fake),
    )
    await client.ensure_category("reeloom")
    await client.add_torrent(
        b"d4:infoe",
        filename="a.torrent",
        save_path="/pt/reeloom/tv/X",
        download_path="/pt/reeloom/tv/in_progress/X",
        category="reeloom",
        tags="reeloom",
    )
    assert fake.logins == 1
    body = fake.added[0]["body"]
    assert "/pt/reeloom/tv/in_progress/X" in body and "useDownloadPath" in body
    fake.expire_next = True
    statuses = await client.torrents(["ABCD"])
    assert fake.logins == 2
    assert statuses["ABCD"].complete
    await client.delete("ABCD", delete_files=False)
    assert "deleteFiles=false" in fake.deleted[0]["body"]
    await client.aclose()


@pytest.mark.parametrize("modern", [False, True], ids=["4.x", "5.x"])
async def test_qbittorrent_wrong_password_is_not_retried(modern: bool) -> None:
    fake = FakeQbit(modern=modern)
    client = QbittorrentClient(
        "http://qbt", "admin", "wrong", transport=httpx.MockTransport(fake)
    )
    for _ in range(3):
        with pytest.raises(QbittorrentClientError) as raised:
            await client.version()
        assert raised.value.code == "pt_client_login_failed"
    assert fake.logins == 1
    await client.aclose()
