from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from reeloom.adapters.acgrip import (
    ACGRIP_MAX_RESPONSE_BYTES,
    AcgripDiscuzParser,
    AcgripSubtitleArchiveFetcher,
    AcgripSubtitleSearchProvider,
)
from reeloom.kernel.subtitle_acquisition import (
    SubtitleArchiveFormat,
    SubtitleArchiveSetCapability,
    SubtitleArchiveSetId,
    SubtitleReleaseId,
    SubtitleSearchCursorId,
    SubtitleSearchEmptyStage,
    SubtitleSearchFailureStage,
)
from reeloom.ports.subtitle_acquisition import (
    SubtitleArchiveError,
    SubtitleArchiveErrorCode,
    SubtitleSearchErrorCode,
    SubtitleSearchProviderError,
    SubtitleSearchRequest,
)
from reeloom.policy.path_policy import AuthorizedRoot

_FORM = b"""
<!doctype html><html><head><title>search</title></head><body>
<form method="post" action="search.php?mod=forum">
  <input type="hidden" name="formhash" value="a1b2c3d4">
  <input name="srchtxt"><input name="searchsubmit" value="yes">
</form></body></html>
"""


def _results(
    *thread_ids: int,
    next_page: bool = False,
    title: str = "测试动画 / Test Anime",
) -> bytes:
    links = "".join(
        f'<li><a href="thread-{thread_id}-1-1.html">{title}</a></li>'
        for thread_id in thread_ids
    )
    next_link = (
        '<a class="nxt" href="search.php?mod=forum&amp;searchid=88&amp;page=2">下一页</a>'
        if next_page
        else ""
    )
    return (
        f"<!doctype html><html><body><h1>搜索结果</h1><ul>{links}</ul>"
        f"{next_link}</body></html>"
    ).encode()


def _attachment(
    aid: int,
    filename: str,
    size: str,
    description: str,
    *,
    native: bool = True,
) -> str:
    href = (
        f"forum.php?mod=attachment&amp;aid=SignedToken{aid}"
        if native
        else "https://outside.invalid/file.zip"
    )
    return f"""
    <dl class="tattl"><dd><p class="attnm">
      <a id="aid{aid}" href="{href}">{filename}</a>
    </p><p>{size}, 下载次数: 3</p><p class="xg2">{description}</p></dd></dl>
    """


def _post(pid: int, message: str, attachments: str) -> str:
    return f"""
    <div id="post_{pid}"><table id="pid{pid}"><tr><td>
      <table><tr><td class="t_f" id="postmessage_{pid}">{message}</td></tr></table>
      <div class="pattl">{attachments}</div>
    </td></tr></table></div>
    """


def _thread(
    *posts: str,
    thread_id: int = 10081,
    forum_id: int = 37,
    max_page: int = 1,
    title: str = "测试动画 / Test Anime",
) -> bytes:
    pages = "".join(
        f'<a href="thread-{thread_id}-{page}-1.html">{page}</a>'
        for page in range(2, max_page + 1)
    )
    return f"""
    <!doctype html><html><body>
      <a href="forum-{forum_id}-1.html">返回列表</a>
      <span id="thread_subject">{title}</span>
      <div id="postlist">{''.join(posts)}</div>{pages}
    </body></html>
    """.encode()


def _request(*, cursor: SubtitleSearchCursorId | None = None, limit: int = 10):
    return SubtitleSearchRequest(
        ("测试动画", "Test Anime"),
        1,
        cursor,
        limit,
    )


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.value += delay


def test_discuz_parser_reads_reply_attachments_and_ignores_external_urls() -> None:
    parser = AcgripDiscuzParser()
    content = _thread(
        _post(
            95257,
            "全集简繁字幕 https://outside.invalid/prompt",
            _attachment(34768, "Group A Subs.7z", "211.41 KB", "简繁")
            + _attachment(
                99999,
                "outside.zip",
                "1 KB",
                "ignore",
                native=False,
            ),
        ),
        _post(
            95261,
            "回复楼层 S01E01-E12",
            _attachment(40001, "Release.part01.rar", "1 MB", "简体")
            + _attachment(40002, "Release.part02.rar", "2 MB", "繁体"),
        ),
    )

    parsed = parser.thread(content, thread_id=10081)

    assert parsed.forum_id == 37
    assert tuple(item.post_id for item in parsed.posts) == (95257, 95261)
    assert tuple(
        item.attachment_id
        for post in parsed.posts
        for item in post.attachments
    ) == (34768, 40001, 40002)
    assert "https://" not in parsed.posts[0].text


def test_search_parser_accepts_bounded_discuz_highlight_parameter() -> None:
    content = b"""
    <html><body>
      <a href="forum.php?mod=viewthread&amp;tid=11895&amp;highlight=%E8%91%AC%E9%80%81">
        \xe8\x91\xac\xe9\x80\x81\xe7\x9a\x84\xe8\x8a\x99\xe8\x8e\x89\xe8\x8e\xb2 S1-S2
      </a>
    </body></html>
    """

    links = AcgripDiscuzParser().search_links(
        content,
        aliases=("\u846c\u9001\u7684\u8299\u8389\u83b2",),
    )

    assert links.thread_ids == (11895,)


def test_provider_returns_opaque_sets_and_never_sends_cookie_or_dynamic_url() -> None:
    requests: list[httpx.Request] = []
    thread = _thread(
        _post(
            95257,
            "全集简繁字幕，匹配 BDRip",
            _attachment(34768, "Group A Subs.7z", "211.41 KB", "简繁")
            + _attachment(34771, "Group B Subs.zip", "113.1 KB", "简体"),
        ),
        _post(
            95261,
            "S01E01-E12 中日字幕",
            _attachment(34801, "Release.part01.rar", "1 MB", "sc jp")
            + _attachment(34802, "Release.part02.rar", "2 MB", "tc jp"),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "bbs.acgrip.com"
        assert request.url.scheme == "https"
        assert "cookie" not in request.headers
        if request.method == "GET" and request.url.path == "/search.php":
            return httpx.Response(
                200,
                content=_FORM,
                headers={"content-type": "text/html", "set-cookie": "bad=1"},
            )
        if request.method == "POST":
            form = parse_qs(request.content.decode())
            assert form["srchfid[]"] == ["37", "46"]
            assert form["formhash"] == ["a1b2c3d4"]
            return httpx.Response(200, content=_results(10081))
        if request.url.path == "/thread-10081-1-1.html":
            return httpx.Response(200, content=thread)
        return httpx.Response(404)

    clock = _Clock()
    provider = AcgripSubtitleSearchProvider(
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
    )
    try:
        result = asyncio.run(provider.search(_request()))
    finally:
        asyncio.run(provider.aclose())

    assert len(result.page.items) == 2
    assert tuple(
        archive.format
        for release in result.page.items
        for archive in release.archive_sets
    ) == (
        SubtitleArchiveFormat.SEVEN_Z,
        SubtitleArchiveFormat.ZIP,
        SubtitleArchiveFormat.RAR,
    )
    assert tuple(
        capability.attachment_ids for capability in result.capabilities
    ) == ((34768,), (34771,), (34801, 34802))
    archive_sets = tuple(
        archive
        for release in result.page.items
        for archive in release.archive_sets
    )
    assert tuple(item.label_hint for item in archive_sets) == (
        "Group A Subs.7z",
        "Group B Subs.zip",
        "Release.part01.rar + Release.part02.rar",
    )
    assert archive_sets[0].language_hints == ("zh-hans", "zh-hant")
    assert archive_sets[1].language_hints == ("zh-hans",)
    assert all(
        "forum.php" not in repr(item)
        and "SignedToken" not in repr(item)
        for item in result.capabilities
    )
    assert clock.sleeps and all(delay >= 1.0 for delay in clock.sleeps)
    assert result.diagnostics.query_aliases == ("测试动画", "Test Anime")
    assert result.diagnostics.alias_thread_counts == (1, 1)
    assert result.diagnostics.discovered_thread_count == 1
    assert result.diagnostics.fetched_thread_count == 1
    assert result.diagnostics.fetched_thread_page_count == 1
    assert result.diagnostics.parsed_post_count == 2
    assert result.diagnostics.native_attachment_count == 4
    assert result.diagnostics.selectable_archive_set_count == 3
    assert result.diagnostics.release_count == 2
    assert (
        result.diagnostics.empty_stage
        is SubtitleSearchEmptyStage.NOT_EMPTY
    )


def test_provider_diagnoses_explicit_forum_search_empty_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=_FORM)
        return httpx.Response(
            200,
            content="<html><body>没有找到匹配结果</body></html>".encode(),
        )

    clock = _Clock()
    provider = AcgripSubtitleSearchProvider(
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
    )
    try:
        result = asyncio.run(provider.search(_request()))
    finally:
        asyncio.run(provider.aclose())

    assert result.page.items == ()
    assert result.diagnostics.query_aliases == ("测试动画", "Test Anime")
    assert result.diagnostics.alias_thread_counts == (0, 0)
    assert result.diagnostics.discovered_thread_count == 0
    assert result.diagnostics.empty_stage is SubtitleSearchEmptyStage.FORUM_SEARCH


def test_provider_spaces_search_submissions_by_five_seconds() -> None:
    clock = _Clock()
    search_submission_times: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=_FORM)
        search_submission_times.append(clock.value)
        return httpx.Response(
            200,
            content="<html><body>没有找到匹配结果</body></html>".encode(),
        )

    provider = AcgripSubtitleSearchProvider(
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
    )
    try:
        result = asyncio.run(provider.search(_request()))
    finally:
        asyncio.run(provider.aclose())

    assert result.diagnostics.alias_thread_counts == (0, 0)
    assert search_submission_times == [1.0, 6.0]
    assert clock.sleeps == [1.0, 5.0]


def test_provider_uses_ascii_space_alias_to_recall_punctuated_title() -> None:
    queries: list[str] = []
    title = "空之色，水之色"
    thread = _thread(
        _post(
            120565,
            "中文字幕",
            _attachment(50001, "Subtitle.7z", "10 KB", "简体"),
        ),
        thread_id=13588,
        title=title,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/search.php":
            return httpx.Response(200, content=_FORM)
        if request.method == "POST":
            query = parse_qs(request.content.decode())["srchtxt"][0]
            queries.append(query)
            if query == "空之色 水之色":
                return httpx.Response(
                    200,
                    content=_results(13588, title=title),
                )
            return httpx.Response(
                200,
                content="<html><body>没有找到匹配结果</body></html>".encode(),
            )
        if request.url.path == "/thread-13588-1-1.html":
            return httpx.Response(200, content=thread)
        return httpx.Response(404)

    clock = _Clock()
    provider = AcgripSubtitleSearchProvider(
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
    )
    request = SubtitleSearchRequest(
        ("空之色,水之色", "空之色 水之色"),
        1,
        None,
        10,
    )
    try:
        result = asyncio.run(provider.search(request))
    finally:
        asyncio.run(provider.aclose())

    assert queries == ["空之色,水之色", "空之色 水之色"]
    assert result.diagnostics.alias_thread_counts == (0, 1)
    assert len(result.page.items) == 1
    assert result.capabilities[0].thread_id == 13588


def test_provider_keeps_only_anonymous_cookie_and_resolves_one_search_redirect() -> None:
    requests: list[httpx.Request] = []
    search_submission_times: list[float] = []
    thread = _thread(
        _post(
            95257,
            "全集简繁字幕",
            _attachment(34768, "Frieren Subs.7z", "211.41 KB", "简繁"),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        cookie = request.headers.get("cookie", "")
        assert "untrusted=" not in cookie
        if (
            request.method == "GET"
            and request.url.path == "/search.php"
            and request.url.params.get("searchid") is None
        ):
            assert not cookie
            return httpx.Response(
                200,
                content=_FORM,
                headers=[
                    ("content-type", "text/html"),
                    (
                        "set-cookie",
                        "3RQm_2132_sid=anonymous123; Path=/; Secure; HttpOnly",
                    ),
                    ("set-cookie", "untrusted=drop-me; Path=/; Secure"),
                ],
            )
        if request.method == "POST":
            search_submission_times.append(clock.value)
            assert cookie == "3RQm_2132_sid=anonymous123"
            return httpx.Response(
                302,
                headers={
                    "location": (
                        "search.php?mod=forum&searchid=465&orderby=lastpost"
                        "&ascdesc=desc&searchsubmit=yes&kw=%E8%91%AC%E9%80%81"
                    )
                },
            )
        if request.url.path == "/search.php":
            assert request.url.params["searchid"] == "465"
            assert cookie == "3RQm_2132_sid=anonymous123"
            return httpx.Response(200, content=_results(10081))
        if request.url.path == "/thread-10081-1-1.html":
            assert cookie == "3RQm_2132_sid=anonymous123"
            return httpx.Response(200, content=thread)
        return httpx.Response(404)

    clock = _Clock()
    provider = AcgripSubtitleSearchProvider(
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
    )
    try:
        result = asyncio.run(provider.search(_request()))
    finally:
        asyncio.run(provider.aclose())

    assert len(result.page.items) == 1
    assert len(requests) == 6
    assert search_submission_times == [1.0, 6.0]


@pytest.mark.parametrize(
    "location",
    (
        "https://outside.invalid/search.php?mod=forum&searchid=1",
        "//outside.invalid/search.php?mod=forum&searchid=1",
        "https://bbs.acgrip.com:444/search.php?mod=forum&searchid=1",
        "search.php?mod=forum&searchid=1&next=https%3A%2F%2Foutside.invalid",
        "search.php?mod=forum&searchid=1&searchid=2",
    ),
)
def test_provider_rejects_non_exact_search_redirect(location: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=_FORM)
        return httpx.Response(302, headers={"location": location})

    provider = AcgripSubtitleSearchProvider(
        transport=httpx.MockTransport(handler),
        sleep=lambda _delay: asyncio.sleep(0),
    )
    try:
        with pytest.raises(SubtitleSearchProviderError) as raised:
            asyncio.run(provider.search(_request()))
    finally:
        asyncio.run(provider.aclose())

    assert raised.value.code is SubtitleSearchErrorCode.PARSER_DRIFT
    assert raised.value.stage is SubtitleSearchFailureStage.FORUM_SEARCH
    assert raised.value.query_alias_index == 0
    assert raised.value.http_response_count == 2
    assert raised.value.received_html_bytes == len(_FORM)
    assert raised.value.http_status == 302


def test_provider_cursor_is_single_use_and_bound_to_exact_request() -> None:
    thread = _thread(
        _post(1, "简体", _attachment(101, "one.zip", "1 KB", "简体")),
        _post(2, "繁体", _attachment(102, "two.7z", "2 KB", "繁体")),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/search.php":
            return httpx.Response(200, content=_FORM)
        if request.method == "POST":
            return httpx.Response(200, content=_results(10081))
        return httpx.Response(200, content=thread)

    clock = _Clock()
    provider = AcgripSubtitleSearchProvider(
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
    )
    try:
        first = asyncio.run(provider.search(_request(limit=1)))
        assert first.page.next_cursor is not None
        second = asyncio.run(
            provider.search(
                _request(cursor=first.page.next_cursor, limit=1)
            )
        )
        assert second.page.complete is True
        with pytest.raises(SubtitleSearchProviderError) as raised:
            asyncio.run(
                provider.search(
                    _request(cursor=first.page.next_cursor, limit=1)
                )
            )
        assert raised.value.code is SubtitleSearchErrorCode.CAPABILITY_UNAVAILABLE
    finally:
        asyncio.run(provider.aclose())


@pytest.mark.parametrize(
    ("response", "code"),
    (
        (
            httpx.Response(302, headers={"location": "https://outside.invalid"}),
            SubtitleSearchErrorCode.PARSER_DRIFT,
        ),
        (
            httpx.Response(429),
            SubtitleSearchErrorCode.RATE_LIMITED,
        ),
        (
            httpx.Response(503),
            SubtitleSearchErrorCode.UNAVAILABLE,
        ),
        (
            httpx.Response(
                200,
                content=b"<html><title>Just a moment...</title><form id='challenge-form'></form></html>",
            ),
            SubtitleSearchErrorCode.CHALLENGE_OR_LOGIN,
        ),
        (
            httpx.Response(200, content=b"<html>unexpected drift</html>"),
            SubtitleSearchErrorCode.PARSER_DRIFT,
        ),
    ),
)
def test_provider_fails_closed_for_redirect_rate_challenge_and_drift(
    response: httpx.Response,
    code: SubtitleSearchErrorCode,
) -> None:
    provider = AcgripSubtitleSearchProvider(
        transport=httpx.MockTransport(lambda _request: response),
        sleep=lambda _delay: asyncio.sleep(0),
    )
    try:
        with pytest.raises(SubtitleSearchProviderError) as raised:
            asyncio.run(
                provider.search(
                    SubtitleSearchRequest(
                        ("测试动画", "Test Anime", "Anime"),
                        1,
                        None,
                        10,
                    )
                )
            )
    finally:
        asyncio.run(provider.aclose())

    assert raised.value.code is code


def test_provider_rejects_decoded_response_over_per_page_limit() -> None:
    provider = AcgripSubtitleSearchProvider(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=b"x" * (ACGRIP_MAX_RESPONSE_BYTES + 1),
            )
        ),
        sleep=lambda _delay: asyncio.sleep(0),
    )
    try:
        with pytest.raises(SubtitleSearchProviderError) as raised:
            asyncio.run(
                provider.search(
                    SubtitleSearchRequest(
                        ("测试动画", "Test Anime", "Anime"),
                        1,
                        None,
                        10,
                    )
                )
            )
    finally:
        asyncio.run(provider.aclose())

    assert raised.value.code is SubtitleSearchErrorCode.RESPONSE_TOO_LARGE


def test_missing_or_cross_style_rar_volumes_are_not_selectable() -> None:
    thread = _thread(
        _post(
            1,
            "全集字幕",
            _attachment(101, "broken.part01.rar", "1 KB", "简体")
            + _attachment(103, "broken.part03.rar", "1 KB", "简体")
            + _attachment(201, "legacy.rar", "1 KB", "简体")
            + _attachment(203, "legacy.r01", "1 KB", "简体")
            + _attachment(301, "split.7z.001", "1 KB", "简体"),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/search.php":
            return httpx.Response(200, content=_FORM)
        if request.method == "POST":
            return httpx.Response(200, content=_results(10081))
        return httpx.Response(200, content=thread)

    clock = _Clock()
    provider = AcgripSubtitleSearchProvider(
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
    )
    try:
        result = asyncio.run(provider.search(_request()))
    finally:
        asyncio.run(provider.aclose())

    assert result.page.items == ()
    assert result.capabilities == ()


def test_provider_follows_bounded_search_and_reply_pagination() -> None:
    requested_paths: list[str] = []
    first_page = _thread(
        _post(1, "首页正文", ""),
        thread_id=10081,
        max_page=2,
    )
    reply_page = _thread(
        _post(
            2,
            "第二页回复附件 S01",
            _attachment(102, "reply.zip", "2 KB", "简体"),
        ),
        thread_id=10081,
        max_page=2,
    )
    second_thread = _thread(
        _post(
            3,
            "搜索结果第二页",
            _attachment(103, "other.7z", "3 KB", "繁体"),
        ),
        thread_id=10082,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(str(request.url))
        if (
            request.method == "GET"
            and request.url.path == "/search.php"
            and request.url.params.get("searchid") is None
        ):
            return httpx.Response(200, content=_FORM)
        if request.method == "POST":
            return httpx.Response(200, content=_results(10081, next_page=True))
        if request.url.params.get("page") == "2":
            return httpx.Response(200, content=_results(10082))
        if request.url.path == "/thread-10081-1-1.html":
            return httpx.Response(200, content=first_page)
        if request.url.path == "/thread-10081-2-1.html":
            return httpx.Response(200, content=reply_page)
        if request.url.path == "/thread-10082-1-1.html":
            return httpx.Response(200, content=second_thread)
        return httpx.Response(404)

    clock = _Clock()
    provider = AcgripSubtitleSearchProvider(
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
    )
    try:
        result = asyncio.run(
            provider.search(
                SubtitleSearchRequest(("测试动画",), 1, None, 10)
            )
        )
    finally:
        asyncio.run(provider.aclose())

    assert len(result.page.items) == 2
    assert tuple(item.attachment_ids for item in result.capabilities) == (
        (102,),
        (103,),
    )
    assert any("searchid=88" in item and "page=2" in item for item in requested_paths)
    assert any("thread-10081-2-1.html" in item for item in requested_paths)


def test_provider_stops_at_twenty_http_responses() -> None:
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "GET" and request.url.path == "/search.php":
            return httpx.Response(200, content=_FORM)
        if request.method == "POST":
            post_count += 1
            start = post_count * 100
            return httpx.Response(
                200,
                content=_results(*range(start, start + 7)),
            )
        thread_id = int(request.url.path.split("-")[1])
        return httpx.Response(
            200,
            content=_thread(
                _post(
                    thread_id,
                    "字幕",
                    _attachment(thread_id, "one.zip", "1 KB", "简体"),
                ),
                thread_id=thread_id,
            ),
        )

    provider = AcgripSubtitleSearchProvider(
        transport=httpx.MockTransport(handler),
        sleep=lambda _delay: asyncio.sleep(0),
    )
    try:
        with pytest.raises(SubtitleSearchProviderError) as raised:
            asyncio.run(
                provider.search(
                    SubtitleSearchRequest(
                        ("测试动画", "Test Anime", "Anime"),
                        1,
                        None,
                        10,
                    )
                )
            )
    finally:
        asyncio.run(provider.aclose())

    assert raised.value.code is SubtitleSearchErrorCode.BUDGET_EXCEEDED


def test_normal_cloudflare_script_reference_is_not_a_challenge_page() -> None:
    parser = AcgripDiscuzParser()
    content = _thread(
        _post(1, "简体字幕", _attachment(101, "one.zip", "1 KB", "简体"))
    ).replace(
        b"</body>",
        b'<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></body>',
    )

    assert parser.thread(content, thread_id=10081).posts[0].post_id == 1


def _capability(*, attachment_ids: tuple[int, ...] = (34768,)):
    return SubtitleArchiveSetCapability(
        SubtitleArchiveSetId(1),
        SubtitleReleaseId(1),
        SubtitleArchiveFormat.ZIP,
        10081,
        95257,
        attachment_ids,
        16,
    )


def test_archive_fetcher_reresolves_signed_url_and_writes_exclusive_volume(
    tmp_path,
) -> None:
    requests: list[httpx.Request] = []
    body = b"PK\x03\x04archive-body"
    thread = _thread(
        _post(
            95257,
            "全集字幕",
            _attachment(34768, "subs.zip", "16 B", "简繁"),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.scheme == "https"
        assert request.url.host == "bbs.acgrip.com"
        assert "cookie" not in request.headers
        if request.url.path == "/thread-10081-1-1.html":
            return httpx.Response(
                200,
                content=thread,
                headers={"content-type": "text/html"},
            )
        if request.url.path == "/forum.php":
            assert request.url.params["aid"] == "SignedToken34768"
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "application/octet-stream"},
            )
        return httpx.Response(404)

    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    clock = _Clock()
    fetcher = AcgripSubtitleArchiveFetcher(
        AuthorizedRoot.create(workspace_path),
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=clock.sleep,
    )
    try:
        result = asyncio.run(fetcher.fetch(_capability()))
    finally:
        asyncio.run(fetcher.aclose())

    assert result.volumes[0].path.read_bytes() == body
    assert result.volumes[0].path.parent.parent == workspace_path
    assert result.capability.attachment_ids == (34768,)
    assert not hasattr(result.capability, "download_path")
    assert len(requests) == 2
    assert clock.sleeps == [1.0]


def test_archive_fetcher_forwards_only_site_issued_anonymous_cookie(tmp_path) -> None:
    body = b"PK\x03\x04archive-body"
    thread = _thread(
        _post(
            95257,
            "全集字幕",
            _attachment(34768, "subs.zip", "16 B", "简繁"),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("cookie", "")
        if request.url.path.startswith("/thread-"):
            assert not cookie
            return httpx.Response(
                200,
                content=thread,
                headers=[
                    ("content-type", "text/html"),
                    (
                        "set-cookie",
                        "3RQm_2132_sid=anonymous123; Path=/; Secure; HttpOnly",
                    ),
                    ("set-cookie", "untrusted=drop-me; Path=/; Secure"),
                ],
            )
        assert cookie == "3RQm_2132_sid=anonymous123"
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/octet-stream"},
        )

    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    fetcher = AcgripSubtitleArchiveFetcher(
        AuthorizedRoot.create(workspace_path),
        transport=httpx.MockTransport(handler),
        sleep=lambda _delay: asyncio.sleep(0),
    )
    try:
        result = asyncio.run(fetcher.fetch(_capability()))
    finally:
        asyncio.run(fetcher.aclose())

    assert result.volumes[0].path.read_bytes() == body


def test_archive_fetcher_rejects_remote_attachment_identity_change(
    tmp_path,
) -> None:
    thread = _thread(
        _post(
            95257,
            "全集字幕",
            _attachment(99999, "subs.zip", "16 B", "changed"),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=thread,
            headers={"content-type": "text/html"},
        )

    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    fetcher = AcgripSubtitleArchiveFetcher(
        AuthorizedRoot.create(workspace_path),
        transport=httpx.MockTransport(handler),
        sleep=lambda _: asyncio.sleep(0),
    )
    try:
        with pytest.raises(SubtitleArchiveError) as raised:
            asyncio.run(fetcher.fetch(_capability()))
    finally:
        asyncio.run(fetcher.aclose())
    assert raised.value.code is SubtitleArchiveErrorCode.CAPABILITY_CHANGED


@pytest.mark.parametrize("status", (302, 403))
def test_archive_fetcher_never_follows_redirect_or_login(
    tmp_path,
    status: int,
) -> None:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    fetcher = AcgripSubtitleArchiveFetcher(
        AuthorizedRoot.create(workspace_path),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                headers={"location": "https://outside.invalid/file"},
            )
        ),
        sleep=lambda _: asyncio.sleep(0),
    )
    try:
        with pytest.raises(SubtitleArchiveError) as raised:
            asyncio.run(fetcher.fetch(_capability()))
    finally:
        asyncio.run(fetcher.aclose())
    assert raised.value.code is SubtitleArchiveErrorCode.CAPABILITY_CHANGED


def test_archive_fetcher_rejects_oversized_remote_body(tmp_path) -> None:
    thread = _thread(
        _post(
            95257,
            "全集字幕",
            _attachment(34768, "subs.zip", "16 B", "简繁"),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/thread-"):
            return httpx.Response(
                200,
                content=thread,
                headers={"content-type": "text/html"},
            )
        return httpx.Response(
            200,
            headers={
                "content-type": "application/octet-stream",
                "content-length": str(16 * 1024 * 1024 + 1),
            },
        )

    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    fetcher = AcgripSubtitleArchiveFetcher(
        AuthorizedRoot.create(workspace_path),
        transport=httpx.MockTransport(handler),
        sleep=lambda _: asyncio.sleep(0),
    )
    try:
        with pytest.raises(SubtitleArchiveError) as raised:
            asyncio.run(fetcher.fetch(_capability()))
    finally:
        asyncio.run(fetcher.aclose())
    assert raised.value.code is SubtitleArchiveErrorCode.DOWNLOAD_TOO_LARGE
