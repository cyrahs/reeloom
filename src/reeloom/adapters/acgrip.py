"""ACG.RIP subtitle forum client.

Fixed origin, three endpoint shapes (search, thread, attachment), no login,
no custom base URL, no proxy, no redirect following. Only the anonymous
session cookies the site issues during a search are kept, because Discuz
search requires them. Pacing is enforced here rather than by the caller.

Everything it returns — thread titles, filenames — is untrusted text.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from reeloom.models import ReeloomError

ORIGIN = "https://bbs.acgrip.com"
FORUM_IDS = (37, 46)

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_THREADS = 20
MAX_ATTACHMENTS = 40
REQUEST_TIMEOUT = 15.0
REQUEST_INTERVAL = 1.0
SEARCH_INTERVAL = 5.0

_SEARCH_PATH = "/search.php?mod=forum"
_STATIC_THREAD_PATH = re.compile(r"^thread-([1-9][0-9]*)-[1-9][0-9]*-1\.html$")
_FORMHASH = re.compile(r"^[0-9a-f]{8}$")
_META_REFRESH_URL = re.compile(r"(?i)url\s*=\s*['\"]?([^'\";]+)")
_SESSION_COOKIE = re.compile(r"^[A-Za-z0-9]{1,16}_2132_(?:saltkey|lastvisit|sid|lastact)$")
_CHALLENGE = ("cf-chl-widget", "challenge-form", "just a moment", "captcha", "验证码")
_LOGIN = ("您需要先登录才能继续本操作", "请先登录")
_WHITESPACE = re.compile(r"\s+")

_LOGGER = logging.getLogger(__name__)


class AcgripError(ReeloomError):
    pass


@dataclass(frozen=True, slots=True)
class Thread:
    thread_id: int
    title: str


@dataclass(frozen=True, slots=True)
class Attachment:
    attachment_id: int
    filename: str
    download_path: str
    """Site-issued, single-use; never persisted."""


def clean(value: str, limit: int = 300) -> str:
    # NFKC folds full-width punctuation and digits to their ASCII forms so
    # the model compares titles in one script width.
    normalized = unicodedata.normalize("NFKC", html.unescape(value))
    return _WHITESPACE.sub(" ", normalized).strip()[:limit]


class AcgripClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock=time.monotonic,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=ORIGIN,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "User-Agent": "Mozilla/5.0 (compatible; reeloom/2.0)",
            },
        )
        self._clock = clock
        self._last_request = 0.0
        self._last_search = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, keyword: str) -> list[Thread]:
        """Search the forum, retrying once with a relaxed query on no results.

        The relaxed form collapses every punctuation run into an ASCII space,
        which Discuz compiles as an AND between title terms. That recovers
        titles whose stored form differs from the query only in punctuation —
        typically full-width marks like ！ glued to a CJK title.
        """

        keyword = unicodedata.normalize("NFKC", keyword).strip()
        if not keyword:
            raise AcgripError("empty_keyword")

        threads = await self._search_once(keyword[:120])
        if threads:
            return threads
        relaxed = _relaxed_keyword(keyword)
        if relaxed is None or relaxed == keyword.casefold():
            return threads
        return await self._search_once(relaxed[:120])

    async def _search_once(self, keyword: str) -> list[Thread]:
        await self._pace(SEARCH_INTERVAL, search=True)
        landing = await self._get_html(_SEARCH_PATH)
        formhash = _find_formhash(landing)
        if formhash is None:
            raise AcgripError("search_form_not_found")

        body = urlencode(
            [
                ("formhash", formhash),
                ("searchsubmit", "yes"),
                ("srchtxt", keyword[:120]),
                *[("srchfid[]", str(forum)) for forum in FORUM_IDS],
            ]
        )
        await self._pace(REQUEST_INTERVAL)
        response = await self._client.post(
            _SEARCH_PATH,
            content=body.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        _keep_only_session_cookies(self._client)
        text = _decode(_bounded(response))
        _reject_challenge(text)

        # Discuz answers a search POST with a same-origin meta refresh to the
        # result listing. Followed manually and validated, never automatically.
        target = _search_result_path(response.headers.get("location") or "") or (
            _search_result_path(_find_meta_refresh(text) or "")
        )
        if target is None:
            return _threads_from_results(text)

        await self._pace(REQUEST_INTERVAL)
        return _threads_from_results(await self._get_html(target))

    async def get_attachments(self, thread_id: int) -> list[Attachment]:
        path = f"/forum.php?mod=viewthread&tid={int(thread_id)}"
        await self._pace(REQUEST_INTERVAL)
        return _parse_attachments(await self._get_html(path))

    async def download(self, attachment: Attachment, destination: Path) -> int:
        """Stream one attachment to disk under a hard size cap."""

        path = _attachment_path(attachment.download_path)
        if path is None:
            raise AcgripError("invalid_attachment_path")

        await self._pace(REQUEST_INTERVAL)
        written = 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with self._client.stream("GET", path) as response:
            if response.status_code != 200:
                raise AcgripError("download_failed", status=response.status_code)
            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        handle.close()
                        destination.unlink(missing_ok=True)
                        raise AcgripError("download_too_large")
                    handle.write(chunk)
        if written == 0:
            destination.unlink(missing_ok=True)
            raise AcgripError("download_empty")
        return written

    async def _get_html(self, path: str) -> str:
        response = await self._client.get(path)
        if response.status_code != 200:
            raise AcgripError("http_error", status=response.status_code, path=path)
        _keep_only_session_cookies(self._client)
        text = _decode(_bounded(response))
        _reject_challenge(text)
        return text

    async def _pace(self, interval: float, *, search: bool = False) -> None:
        now = self._clock()
        reference = self._last_search if search else self._last_request
        wait = reference + interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        stamp = self._clock()
        self._last_request = stamp
        if search:
            self._last_search = stamp


def _bounded(response: httpx.Response) -> bytes:
    content = response.content
    if len(content) > MAX_RESPONSE_BYTES:
        raise AcgripError("response_too_large", size=len(content))
    return content


def _decode(content: bytes) -> str:
    for encoding in ("utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _reject_challenge(text: str) -> None:
    lowered = text.casefold()
    if any(marker in lowered for marker in _CHALLENGE):
        raise AcgripError("bot_challenge")
    if any(marker in text for marker in _LOGIN):
        raise AcgripError("login_required")


def _keep_only_session_cookies(client: httpx.AsyncClient) -> None:
    for cookie in tuple(client.cookies.jar):
        if (
            _SESSION_COOKIE.fullmatch(cookie.name) is None
            or cookie.domain.lstrip(".") != "bbs.acgrip.com"
            or len(cookie.value or "") > 512
        ):
            client.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)


_MIN_RELAXED_CHARACTERS = 2


def _relaxed_keyword(value: str) -> str | None:
    """Keep letter/mark/number runs, join them with the spaces Discuz ANDs."""

    terms: list[str] = []
    current: list[str] = []
    informative = 0
    for character in value.casefold():
        category = unicodedata.category(character)[0]
        if category in "LMN":
            current.append(character)
            if category in "LN":
                informative += 1
            continue
        if current:
            terms.append("".join(current))
            current = []
    if current:
        terms.append("".join(current))
    if informative < _MIN_RELAXED_CHARACTERS or not terms:
        return None
    return " ".join(terms)


def _search_result_path(value: str) -> str | None:
    """Accept only a same-origin Discuz search listing URL."""

    if not value:
        return None
    parsed = urlparse(html.unescape(value.strip()))
    if parsed.scheme and parsed.scheme != "https":
        return None
    if parsed.netloc and parsed.netloc != "bbs.acgrip.com":
        return None
    path = parsed.path if parsed.path.startswith("/") else f"/{parsed.path}"
    if path != "/search.php" or not parsed.query or len(parsed.query) > 1024:
        return None
    query = parse_qs(parsed.query)
    searchid = query.get("searchid", [""])[0]
    if query.get("mod") != ["forum"] or not searchid.isdigit():
        return None
    return f"{path}?{parsed.query}"


def _attachment_path(value: str) -> str | None:
    parsed = urlparse(html.unescape(value.strip()))
    if parsed.scheme and parsed.scheme != "https":
        return None
    if parsed.netloc and parsed.netloc != "bbs.acgrip.com":
        return None
    path = parsed.path if parsed.path.startswith("/") else f"/{parsed.path}"
    query = parse_qs(parsed.query)
    if path != "/forum.php" or query.get("mod") != ["attachment"]:
        return None
    if len(query.get("aid", [])) != 1 or len(parsed.query) > 1024:
        return None
    return f"{path}?{parsed.query}"


class _FormhashParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.formhash: str | None = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("name") == "formhash":
            value = values.get("value") or ""
            if _FORMHASH.fullmatch(value):
                self.formhash = value


def _find_formhash(text: str) -> str | None:
    parser = _FormhashParser()
    parser.feed(text)
    return parser.formhash


class _MetaRefreshParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.url: str | None = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and (values.get("http-equiv") or "").casefold() == "refresh":
            match = _META_REFRESH_URL.search(values.get("content") or "")
            if match:
                self.url = match.group(1)


def _find_meta_refresh(text: str) -> str | None:
    parser = _MetaRefreshParser()
    parser.feed(text)
    return parser.url


def _thread_id(value: str) -> int | None:
    """Read a thread id out of either Discuz link style, or give up.

    Search listings link results as ``forum.php?mod=viewthread&tid=…`` while
    static rewriting elsewhere produces ``/thread-<tid>-<page>-1.html``. Only
    origin and tid matter: the href itself is never fetched, the viewthread
    path is rebuilt from the integer id.
    """

    parsed = urlparse(html.unescape(value.strip()))
    if parsed.scheme and parsed.scheme != "https":
        return None
    if parsed.netloc and parsed.netloc != "bbs.acgrip.com":
        return None
    path = parsed.path.lstrip("/")
    match = _STATIC_THREAD_PATH.fullmatch(path)
    if match is not None:
        return int(match.group(1))
    if path != "forum.php":
        return None
    query = parse_qs(parsed.query)
    if query.get("mod") != ["viewthread"]:
        return None
    tid = query.get("tid", [""])[0]
    return int(tid) if tid.isdigit() else None


class _ThreadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.threads: dict[int, str] = {}
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a" and self._href is None:
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or self._href is None:
            return
        thread_id = _thread_id(self._href)
        title = clean("".join(self._text))
        if thread_id is not None and title and len(self.threads) < MAX_THREADS:
            self.threads.setdefault(thread_id, title)
        self._href = None
        self._text = []


def _parse_threads(text: str) -> list[Thread]:
    parser = _ThreadParser()
    parser.feed(text)
    return [Thread(tid, title) for tid, title in parser.threads.items()]


_NO_RESULTS_MARKER = "没有找到匹配结果"


def _threads_from_results(text: str) -> list[Thread]:
    # Pinned notices link from every results page, so parsed threads alone
    # cannot tell an empty search from a hit; Discuz's own marker can.
    if _NO_RESULTS_MARKER in text:
        return []
    return _parse_threads(text)


class _AttachmentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.attachments: dict[int, Attachment] = {}
        self._pending: tuple[int, str] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        values = dict(attrs)
        href = values.get("href") or ""
        path = _attachment_path(href)
        if path is None:
            return
        # Discuz signs the aid in the href (base64, no digit prefix); the
        # anchor's own id attribute carries the numeric identity. Fall back
        # to a numeric prefix for skins that inline it in the parameter.
        identity = re.fullmatch(r"aid([1-9][0-9]*)", values.get("id") or "")
        if identity is not None:
            numeric = int(identity.group(1))
        else:
            aid = parse_qs(urlparse(html.unescape(href)).query).get("aid", [""])[0]
            digits = re.match(r"^\d+", aid)
            if digits is None:
                return
            numeric = int(digits.group())
        self._pending = (numeric, path)
        self._text = []

    def handle_data(self, data):
        if self._pending is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or self._pending is None:
            return
        aid, path = self._pending
        filename = clean("".join(self._text), 200)
        if filename and len(self.attachments) < MAX_ATTACHMENTS:
            self.attachments.setdefault(
                aid,
                Attachment(attachment_id=aid, filename=filename, download_path=path),
            )
        self._pending = None
        self._text = []


def _parse_attachments(text: str) -> list[Attachment]:
    parser = _AttachmentParser()
    parser.feed(text)
    return list(parser.attachments.values())
