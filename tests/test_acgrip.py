"""ACG.RIP client, driven entirely by a mock transport. No network."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from reeloom.adapters.acgrip import (
    AcgripClient,
    AcgripError,
    Attachment,
    _attachment_path,
    _search_result_path,
)

SEARCH_FORM = """
<html><body><form action="search.php?mod=forum" method="post">
<input type="hidden" name="formhash" value="a1b2c3d4">
<input name="srchtxt"></form></body></html>
"""

RESULTS = """
<html><body>
<a href="/thread-12345-1-1.html">[Group] Show 简体 01-12 合集</a>
<a href="/thread-999-1-1.html">[Group] Other Show</a>
<a href="https://evil.example/thread-1-1-1.html">offsite</a>
</body></html>
"""

THREAD = """
<html><body>
<a href="forum.php?mod=attachment&amp;aid=4242abcd">Show.S01.CHS.7z</a>
<a href="forum.php?mod=attachment&amp;aid=4243abcd">notes.txt</a>
<a href="https://elsewhere.example/forum.php?mod=attachment&amp;aid=1">offsite.7z</a>
</body></html>
"""


def free_running_clock():
    """A clock far past every cooldown, so pacing never sleeps in tests."""

    state = {"now": 0.0}

    def clock() -> float:
        state["now"] += 1000.0
        return state["now"]

    return clock


def make_client(handler, clock=None) -> AcgripClient:
    return AcgripClient(
        transport=httpx.MockTransport(handler), clock=clock or free_running_clock()
    )


def default_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    query = request.url.query.decode()
    if path == "/search.php" and request.method == "GET" and "searchid" in query:
        return httpx.Response(200, text=RESULTS)
    if path == "/search.php" and request.method == "GET":
        return httpx.Response(200, text=SEARCH_FORM)
    if path == "/search.php" and request.method == "POST":
        return httpx.Response(
            200,
            text=(
                '<html><head><meta http-equiv="refresh" '
                'content="0;url=search.php?mod=forum&searchid=77&orderby=dateline">'
                "</head></html>"
            ),
        )
    if path == "/forum.php" and "viewthread" in query:
        return httpx.Response(200, text=THREAD)
    if path == "/forum.php" and "attachment" in query:
        return httpx.Response(200, content=b"7z-bytes")
    return httpx.Response(404)


# ---- URL validation -----------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/search.php?mod=forum&searchid=1",
        "http://bbs.acgrip.com/search.php?mod=forum&searchid=1",
        "/search.php?mod=forum",
        "/forum.php?mod=forum&searchid=1",
        "/search.php?mod=forum&searchid=abc",
        "",
    ],
)
def test_search_redirects_off_the_expected_shape_are_refused(value: str) -> None:
    assert _search_result_path(value) is None


def test_a_same_origin_search_listing_is_accepted() -> None:
    assert (
        _search_result_path("search.php?mod=forum&searchid=77")
        == "/search.php?mod=forum&searchid=77"
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://elsewhere.example/forum.php?mod=attachment&aid=1",
        "/forum.php?mod=viewthread&tid=1",
        "/download.php?aid=1",
    ],
)
def test_attachment_paths_are_restricted(value: str) -> None:
    assert _attachment_path(value) is None


# ---- search -------------------------------------------------------------


async def test_search_follows_the_meta_refresh_and_lists_threads() -> None:
    client = make_client(default_handler)

    threads = await client.search("Show")

    assert [thread.thread_id for thread in threads] == [12345, 999]
    assert threads[0].title == "[Group] Show 简体 01-12 合集"
    await client.aclose()


async def test_offsite_thread_links_are_ignored() -> None:
    client = make_client(default_handler)
    threads = await client.search("Show")
    assert all(thread.thread_id != 1 for thread in threads)
    await client.aclose()


async def test_a_missing_form_hash_is_an_error() -> None:
    client = make_client(lambda request: httpx.Response(200, text="<html></html>"))

    with pytest.raises(AcgripError) as error:
        await client.search("Show")

    assert error.value.code == "search_form_not_found"
    await client.aclose()


async def test_a_bot_challenge_stops_the_search() -> None:
    client = make_client(
        lambda request: httpx.Response(200, text="<div>Just a moment...</div>")
    )

    with pytest.raises(AcgripError) as error:
        await client.search("Show")

    assert error.value.code == "bot_challenge"
    await client.aclose()


async def test_a_login_wall_stops_the_search() -> None:
    client = make_client(
        lambda request: httpx.Response(200, text="<p>您需要先登录才能继续本操作</p>")
    )

    with pytest.raises(AcgripError) as error:
        await client.search("Show")

    assert error.value.code == "login_required"
    await client.aclose()


async def test_an_oversized_page_is_refused() -> None:
    client = make_client(
        lambda request: httpx.Response(200, text="x" * (2 * 1024 * 1024))
    )

    with pytest.raises(AcgripError) as error:
        await client.search("Show")

    assert error.value.code == "response_too_large"
    await client.aclose()


async def test_only_site_session_cookies_are_kept() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = default_handler(request)
        response.headers["set-cookie"] = "tracking_id=abc; Path=/"
        return response

    client = make_client(handler)
    await client.search("Show")

    assert [cookie.name for cookie in client._client.cookies.jar] == []
    await client.aclose()


# ---- attachments and download -------------------------------------------


async def test_thread_attachments_are_listed_from_the_same_origin_only() -> None:
    client = make_client(default_handler)

    attachments = await client.get_attachments(12345)

    assert [item.filename for item in attachments] == [
        "Show.S01.CHS.7z",
        "notes.txt",
    ]
    assert attachments[0].attachment_id == 4242
    await client.aclose()


async def test_download_writes_the_attachment(tmp_path: Path) -> None:
    client = make_client(default_handler)
    attachment = Attachment(
        attachment_id=1,
        filename="release.7z",
        download_path="/forum.php?mod=attachment&aid=1",
    )

    written = await client.download(attachment, tmp_path / "release.7z")

    assert written == len(b"7z-bytes")
    assert (tmp_path / "release.7z").read_bytes() == b"7z-bytes"
    await client.aclose()


async def test_an_offsite_download_path_is_refused(tmp_path: Path) -> None:
    client = make_client(default_handler)
    attachment = Attachment(
        attachment_id=1,
        filename="x.7z",
        download_path="https://elsewhere.example/forum.php?mod=attachment&aid=1",
    )

    with pytest.raises(AcgripError) as error:
        await client.download(attachment, tmp_path / "x.7z")

    assert error.value.code == "invalid_attachment_path"
    assert not (tmp_path / "x.7z").exists()
    await client.aclose()


async def test_an_empty_download_leaves_no_file(tmp_path: Path) -> None:
    client = make_client(lambda request: httpx.Response(200, content=b""))
    attachment = Attachment(
        attachment_id=1,
        filename="x.7z",
        download_path="/forum.php?mod=attachment&aid=1",
    )

    with pytest.raises(AcgripError) as error:
        await client.download(attachment, tmp_path / "x.7z")

    assert error.value.code == "download_empty"
    assert not (tmp_path / "x.7z").exists()
    await client.aclose()


async def test_searches_are_paced(monkeypatch) -> None:
    now = {"value": 0.0}
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now["value"] += seconds

    monkeypatch.setattr("reeloom.adapters.acgrip.asyncio.sleep", fake_sleep)
    client = make_client(default_handler, clock=lambda: now["value"])

    await client.search("Show")
    await client.search("Show Again")

    # The second search waits out the cooldown rather than hammering the forum.
    assert any(value >= 4.0 for value in slept)
    await client.aclose()
