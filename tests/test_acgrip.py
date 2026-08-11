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
    _relaxed_keyword,
    _search_result_path,
    _thread_id,
)

SEARCH_FORM = """
<html><body><form action="search.php?mod=forum" method="post">
<input type="hidden" name="formhash" value="a1b2c3d4">
<input name="srchtxt"></form></body></html>
"""

RESULTS = """
<html><body>
<a href="/thread-12345-1-1.html">[Group] Show 简体 01-12 合集</a>
<a href="forum.php?mod=viewthread&amp;tid=10806&amp;highlight=Show">百合是我的工作！ / 私の百合はお仕事です！</a>
<a href="/thread-999-1-1.html">[Group] Other Show</a>
<a href="https://evil.example/thread-1-1-1.html">offsite</a>
<a href="https://evil.example/forum.php?mod=viewthread&amp;tid=2">offsite dynamic</a>
</body></html>
"""

THREAD = """
<html><body>
<table><tr><td class="t_f" id="postmessage_100">
本帖字幕为 <strong>简体中文</strong>，
<table><tr><td>外挂 ass 格式</td></tr></table>
全 12 集。
</td></tr></table>
<a href="forum.php?mod=attachment&amp;aid=4242abcd">Show.S01.CHS.7z</a>
<a href="forum.php?mod=attachment&amp;aid=4243abcd">notes.txt</a>
<a href="forum.php?mod=attachment&amp;aid=Mzc5NzN8Zjc5NjNhY2F8MTc4NjQzMDAxM3wwfDEwODA2" id="aid37973" target="_blank">Show.S01.CHT.7z</a>
<a href="https://elsewhere.example/forum.php?mod=attachment&amp;aid=1">offsite.7z</a>
<table><tr><td class="t_f" id="postmessage_101">感谢分享！</td></tr></table>
<td class="t_f">not a post: no postmessage id</td>
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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/thread-12345-1-1.html", 12345),
        ("https://bbs.acgrip.com/thread-12345-2-1.html", 12345),
        ("forum.php?mod=viewthread&tid=10806", 10806),
        ("forum.php?mod=viewthread&tid=10806&highlight=Show%2BTitle", 10806),
        ("/forum.php?mod=viewthread&tid=10806&page=2", 10806),
        ("/forum.php?mod=viewthread&tid=4&extra=page%3D1", 4),
        ("https://bbs.acgrip.com/forum.php?mod=viewthread&tid=7", 7),
    ],
)
def test_thread_links_of_both_discuz_styles_are_read(
    value: str, expected: int
) -> None:
    assert _thread_id(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/thread-1-1-1.html",
        "https://evil.example/forum.php?mod=viewthread&tid=2",
        "http://bbs.acgrip.com/forum.php?mod=viewthread&tid=3",
        "/forum.php?mod=viewthread&tid=abc",
        "/forum.php?mod=attachment&aid=1",
        "/thread-12345-1-2.html",
        "",
    ],
)
def test_thread_links_off_the_expected_shape_are_refused(value: str) -> None:
    assert _thread_id(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("我的百合乃工作是也!", "我的百合乃工作是也"),
        ("Watashi no Yuri wa Oshigoto Desu!", "watashi no yuri wa oshigoto desu"),
        ("[Group] Show (2024)", "group show 2024"),
        ("!?", None),
        ("", None),
    ],
)
def test_relaxed_keywords_keep_only_term_runs(
    value: str, expected: str | None
) -> None:
    assert _relaxed_keyword(value) == expected


# ---- search -------------------------------------------------------------


async def test_search_follows_the_meta_refresh_and_lists_threads() -> None:
    client = make_client(default_handler)

    threads = await client.search("Show")

    assert [thread.thread_id for thread in threads] == [12345, 10806, 999]
    assert threads[0].title == "[Group] Show 简体 01-12 合集"
    await client.aclose()


async def test_dynamic_viewthread_links_are_listed() -> None:
    client = make_client(default_handler)
    threads = await client.search("Show")
    assert any(
        thread.thread_id == 10806 and "百合是我的工作" in thread.title
        for thread in threads
    )
    await client.aclose()


async def test_offsite_thread_links_are_ignored() -> None:
    client = make_client(default_handler)
    threads = await client.search("Show")
    assert all(thread.thread_id not in (1, 2) for thread in threads)
    await client.aclose()


async def test_a_fullwidth_keyword_retries_relaxed_and_finds_threads() -> None:
    keywords: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search.php" and request.method == "POST":
            body = request.content.decode()
            from urllib.parse import parse_qs

            keywords.append(parse_qs(body)["srchtxt"][0])
        if (
            request.url.path == "/search.php"
            and request.method == "GET"
            and "searchid" in request.url.query.decode()
        ):
            # Only the relaxed, punctuation-free query matches anything. The
            # empty page still links a pinned notice, as the live site does.
            if keywords and keywords[-1] == "我的百合乃工作是也":
                return httpx.Response(200, text=RESULTS)
            return httpx.Response(
                200,
                text=(
                    '<html><body><a href="/thread-4593-1-1.html">镜像站说明</a>'
                    "对不起，没有找到匹配结果。</body></html>"
                ),
            )
        return default_handler(request)

    client = make_client(handler)

    threads = await client.search("我的百合乃工作是也！")

    # NFKC folds the full-width ！ to ASCII first; the relaxed retry drops it.
    assert keywords == ["我的百合乃工作是也!", "我的百合乃工作是也"]
    assert threads
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

    attachments = (await client.get_thread(12345)).attachments

    assert [item.filename for item in attachments] == [
        "Show.S01.CHS.7z",
        "notes.txt",
        "Show.S01.CHT.7z",
    ]
    assert attachments[0].attachment_id == 4242
    # A signed base64 aid takes its identity from the anchor's id attribute.
    assert attachments[2].attachment_id == 37973
    assert "Mzc5NzN8" in attachments[2].download_path
    await client.aclose()


async def test_thread_excerpt_collects_cleaned_post_text() -> None:
    client = make_client(default_handler)

    page = await client.get_thread(12345)

    # Nested tables stay inside their post; posts join in page order; the
    # td without a postmessage id is not a post.
    assert "简体中文" in page.excerpt
    assert "外挂 ass 格式" in page.excerpt
    assert "全 12 集" in page.excerpt
    assert "感谢分享" in page.excerpt
    assert "not a post" not in page.excerpt
    assert "\n" not in page.excerpt
    await client.aclose()


async def test_an_oversized_thread_page_is_truncated_not_refused() -> None:
    padding = "填" * (2 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/forum.php":
            return httpx.Response(200, text=THREAD + padding)
        return default_handler(request)

    client = make_client(handler)

    page = await client.get_thread(12345)

    assert [item.attachment_id for item in page.attachments] == [4242, 4243, 37973]
    assert "简体中文" in page.excerpt
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
