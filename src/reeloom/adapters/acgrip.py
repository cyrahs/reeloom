from __future__ import annotations

import asyncio
import html
import hashlib
import os
import re
import stat
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from reeloom.kernel.subtitle_acquisition import (
    CURRENT_SUBTITLE_SEARCH_PARSER_VERSION,
    CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION,
    MAX_ARCHIVE_VOLUME_BYTES,
    MAX_ARCHIVE_VOLUMES,
    MAX_SEARCH_RESULTS_PER_PAGE,
    MAX_SEARCH_RESULTS_PER_RUN,
    MAX_TOTAL_ARCHIVE_BYTES,
    SubtitleArchiveFormat,
    SubtitleArchiveSetCapability,
    SubtitleArchiveSetId,
    SubtitleArchiveSetSummary,
    SubtitleArchiveVolume,
    SubtitleReleaseId,
    SubtitleReleaseSummary,
    SubtitleSearchCursorId,
    SubtitleSearchPage,
)
from reeloom.ports.subtitle_acquisition import (
    DownloadedArchiveVolume,
    DownloadedSubtitleArchiveSet,
    SubtitleArchiveError,
    SubtitleArchiveErrorCode,
    SubtitleSearchErrorCode,
    SubtitleSearchProviderError,
    SubtitleSearchRequest,
    SubtitleSearchResult,
)
from reeloom.policy.path_policy import AuthorizedRoot

ACGRIP_ORIGIN = "https://bbs.acgrip.com"
ACGRIP_FORUM_IDS = (37, 46)
ACGRIP_MAX_HTTP_RESPONSES = 20
ACGRIP_MAX_RESPONSE_BYTES = 1 * 1024 * 1024
ACGRIP_MAX_TOTAL_HTML_BYTES = 8 * 1024 * 1024
ACGRIP_MAX_ATTACHMENTS = 100
ACGRIP_REQUEST_TIMEOUT_SECONDS = 5.0
ACGRIP_TOOL_TIMEOUT_SECONDS = 30.0
ACGRIP_REQUEST_INTERVAL_SECONDS = 1.0

_SEARCH_PATH = "/search.php?mod=forum"
_THREAD_PATH = re.compile(r"^/thread-([1-9][0-9]*)-([1-9][0-9]*)-1\.html$")
_ANONYMOUS_COOKIE_NAME = re.compile(
    r"^[A-Za-z0-9]{1,16}_2132_(?:saltkey|lastvisit|sid|lastact)$"
)
_SEARCH_RESULT_KEYS = frozenset(
    {"mod", "searchid", "orderby", "ascdesc", "searchsubmit", "kw", "page"}
)
_SIGNED_AID = re.compile(r"^[A-Za-z0-9_+/.-]{8,254}={0,2}$")
_URL_TEXT = re.compile(r"(?i)(?:https?://|www\.|magnet:\?)[^\s]+")
_SPACE = re.compile(r"\s+")
_SIZE = re.compile(r"(?i)([0-9]+(?:\.[0-9]{1,3})?)\s*(B|KB|MB)\b")
_PART_RAR = re.compile(r"(?i)^(.*)\.part([0-9]{1,3})\.rar$")
_OLD_RAR = re.compile(r"(?i)^(.*)\.r([0-9]{2})$")
_COVERAGE = re.compile(
    r"(?i)(S[0-9]{1,3}(?:E[0-9]{1,4}(?:[-~]E?[0-9]{1,4})?)?"
    r"|EP?\s*[0-9]{1,4}(?:\s*[-~]\s*[0-9]{1,4})?"
    r"|[0-9]{1,3}\s*[-~]\s*[0-9]{1,3}|全季|全集|一二季)"
)
_CHALLENGE_MARKERS = (
    "cf-chl-widget",
    "challenge-form",
    "just a moment",
    "checking your browser",
    "captcha",
    "验证码",
)
_LOGIN_MARKERS = (
    "您需要先登录才能继续本操作",
    "请先登录",
    "loginform",
)


def _provider_error(
    code: SubtitleSearchErrorCode,
    *,
    retryable: bool,
) -> SubtitleSearchProviderError:
    return SubtitleSearchProviderError(code, retryable=retryable)


def _clean_text(value: str, *, max_bytes: int) -> str:
    value = _URL_TEXT.sub(" ", html.unescape(value))
    value = "".join(
        char if not unicodedata.category(char).startswith("C") else " "
        for char in unicodedata.normalize("NFKC", value)
    )
    value = _SPACE.sub(" ", value).strip()
    encoded = value.encode("utf-8")
    if len(encoded) > max_bytes:
        value = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()
    return value


def _title_key(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", value).casefold()
        if char.isalnum()
    )


def _matches_alias(value: str, aliases: tuple[str, ...]) -> bool:
    key = _title_key(value)
    return bool(key) and any(
        (alias_key := _title_key(alias))
        and (alias_key in key or key in alias_key)
        for alias in aliases
    )


def _validated_search_result_path(
    value: str,
    *,
    allow_same_origin_absolute: bool = False,
) -> str:
    try:
        parsed = urlsplit(html.unescape(value))
        port = parsed.port
    except ValueError:
        raise _provider_error(
            SubtitleSearchErrorCode.PARSER_DRIFT,
            retryable=False,
        ) from None
    if parsed.fragment or parsed.username or parsed.password or port:
        raise _provider_error(
            SubtitleSearchErrorCode.PARSER_DRIFT,
            retryable=False,
        )
    if parsed.scheme or parsed.netloc:
        if not (
            allow_same_origin_absolute
            and parsed.scheme == "https"
            and parsed.netloc == "bbs.acgrip.com"
        ):
            raise _provider_error(
                SubtitleSearchErrorCode.PARSER_DRIFT,
                retryable=False,
            )
    path = parsed.path if parsed.path.startswith("/") else f"/{parsed.path}"
    if path != "/search.php" or not parsed.query or len(parsed.query) > 1024:
        raise _provider_error(
            SubtitleSearchErrorCode.PARSER_DRIFT,
            retryable=False,
        )
    try:
        query = parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=len(_SEARCH_RESULT_KEYS),
        )
    except ValueError:
        raise _provider_error(
            SubtitleSearchErrorCode.PARSER_DRIFT,
            retryable=False,
        ) from None
    if (
        set(query) - _SEARCH_RESULT_KEYS
        or query.get("mod") != ["forum"]
        or len(query.get("searchid", ())) != 1
        or not query["searchid"][0].isdigit()
        or not 1 <= int(query["searchid"][0]) <= 2_147_483_647
        or any(len(values) != 1 for values in query.values())
        or any(
            len(values[0].encode("utf-8")) > 256
            or any(ord(char) < 32 for char in values[0])
            for values in query.values()
        )
        or (
            "page" in query
            and (not query["page"][0].isdigit() or int(query["page"][0]) < 1)
        )
    ):
        raise _provider_error(
            SubtitleSearchErrorCode.PARSER_DRIFT,
            retryable=False,
        )
    return path + f"?{parsed.query}"


def _validated_path(value: str) -> str:
    parsed = urlsplit(html.unescape(value))
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise _provider_error(
            SubtitleSearchErrorCode.PARSER_DRIFT,
            retryable=False,
        )
    path = parsed.path if parsed.path.startswith("/") else f"/{parsed.path}"
    candidate = path + (f"?{parsed.query}" if parsed.query else "")
    if candidate == _SEARCH_PATH:
        return candidate
    if _THREAD_PATH.fullmatch(path) and not parsed.query:
        return path
    return _validated_search_result_path(candidate)


def _retain_anonymous_session_cookies(client: httpx.AsyncClient) -> None:
    """Keep only bounded, freshly site-issued Discuz anonymous session cookies."""

    for cookie in tuple(client.cookies.jar):
        valid = (
            _ANONYMOUS_COOKIE_NAME.fullmatch(cookie.name) is not None
            and cookie.domain.lstrip(".") == "bbs.acgrip.com"
            and cookie.path == "/"
            and cookie.secure
            and 0 < len(cookie.value) <= 512
            and all(32 <= ord(char) < 127 and char != ";" for char in cookie.value)
        )
        if not valid:
            client.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)


def _thread_link(value: str) -> tuple[int, int] | None:
    parsed = urlsplit(html.unescape(value))
    if parsed.scheme and (
        parsed.scheme != "https" or parsed.netloc != "bbs.acgrip.com"
    ):
        return None
    if parsed.netloc and parsed.netloc != "bbs.acgrip.com":
        return None
    path = parsed.path.lstrip("/")
    match = re.fullmatch(r"thread-([1-9][0-9]*)-([1-9][0-9]*)-1\.html", path)
    if match is not None:
        return int(match.group(1)), int(match.group(2))
    if path != "forum.php":
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (
        query.get("mod") != ["viewthread"]
        or len(query.get("tid", ())) != 1
        or not query["tid"][0].isdigit()
        or set(query) - {"mod", "tid", "page", "highlight"}
        or any(len(values) != 1 for values in query.values())
    ):
        return None
    page = query.get("page", ["1"])[0]
    if not page.isdigit() or int(page) < 1:
        return None
    highlight = query.get("highlight", [""])[0]
    if len(highlight.encode("utf-8")) > 256 or any(
        ord(char) < 32 for char in highlight
    ):
        return None
    return int(query["tid"][0]), int(page)


def _forum_id_from_href(value: str) -> int | None:
    parsed = urlsplit(html.unescape(value))
    if parsed.scheme and parsed.netloc != "bbs.acgrip.com":
        return None
    match = re.fullmatch(r"/?forum-([1-9][0-9]*)-[1-9][0-9]*\.html", parsed.path)
    if match is not None:
        return int(match.group(1))
    query = parse_qs(parsed.query)
    values = query.get("fid", ())
    return int(values[0]) if len(values) == 1 and values[0].isdigit() else None


def _native_attachment_path(value: str, numeric_aid: int) -> str | None:
    parsed = urlsplit(html.unescape(value))
    if parsed.scheme and (
        parsed.scheme != "https" or parsed.netloc != "bbs.acgrip.com"
    ):
        return None
    if parsed.netloc and parsed.netloc != "bbs.acgrip.com":
        return None
    if parsed.path.lstrip("/") != "forum.php":
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    if not (
        query.get("mod") == ["attachment"]
        and len(query.get("aid", ())) == 1
        and _SIGNED_AID.fullmatch(query["aid"][0]) is not None
        and numeric_aid > 0
        and not (set(query) - {"mod", "aid", "nothumb", "noupdate"})
    ):
        return None
    path = parsed.path if parsed.path.startswith("/") else f"/{parsed.path}"
    return path + f"?{parsed.query}"
def _raise_for_page_markers(content: str) -> None:
    lowered = content.casefold()
    if any(marker in lowered for marker in _CHALLENGE_MARKERS) or any(
        marker in content for marker in _LOGIN_MARKERS
    ):
        raise _provider_error(
            SubtitleSearchErrorCode.CHALLENGE_OR_LOGIN,
            retryable=False,
        )
    if "Discuz! System Error" in content or "系统错误" in content:
        raise _provider_error(
            SubtitleSearchErrorCode.PARSER_DRIFT,
            retryable=False,
        )


class _SearchFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_search_form = False
        self.form_depth = 0
        self.formhash: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        if tag == "form":
            action = values.get("action") or ""
            if html.unescape(action) == "search.php?mod=forum":
                self.in_search_form = True
                self.form_depth = 1
            elif self.in_search_form:
                self.form_depth += 1
        elif self.in_search_form and tag == "input":
            if values.get("name") == "formhash":
                value = values.get("value")
                if value is not None and re.fullmatch(r"[0-9a-f]{8}", value):
                    self.formhash = value

    def handle_endtag(self, tag: str) -> None:
        if self.in_search_form and tag == "form":
            self.form_depth -= 1
            if self.form_depth <= 0:
                self.in_search_form = False


@dataclass(frozen=True, slots=True)
class _SearchLinks:
    thread_ids: tuple[int, ...]
    navigation_path: str | None
    next_path: str | None


class _SearchLinksParser(HTMLParser):
    def __init__(self, aliases: tuple[str, ...]) -> None:
        super().__init__(convert_charrefs=True)
        self.aliases = aliases
        self.anchor_href: str | None = None
        self.anchor_classes: frozenset[str] = frozenset()
        self.anchor_text: list[str] = []
        self.thread_ids: list[int] = []
        self.next_path: str | None = None
        self.meta_refresh: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        if tag == "a" and self.anchor_href is None:
            self.anchor_href = values.get("href")
            self.anchor_classes = frozenset(
                (values.get("class") or "").split()
            )
            self.anchor_text = []
        elif tag == "meta" and (values.get("http-equiv") or "").casefold() == "refresh":
            content = values.get("content") or ""
            match = re.search(r"(?i)url\s*=\s*['\"]?([^'\";]+)", content)
            if match is not None:
                self.meta_refresh = match.group(1)

    def handle_data(self, data: str) -> None:
        if self.anchor_href is not None:
            self.anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self.anchor_href is None:
            return
        href = self.anchor_href
        text = _clean_text(" ".join(self.anchor_text), max_bytes=512)
        linked = _thread_link(href)
        if linked is not None and _matches_alias(text, self.aliases):
            if linked[0] not in self.thread_ids:
                self.thread_ids.append(linked[0])
        if "nxt" in self.anchor_classes and "search.php" in href:
            self.next_path = href
        self.anchor_href = None
        self.anchor_classes = frozenset()
        self.anchor_text = []


@dataclass(frozen=True, slots=True)
class _ParsedAttachment:
    attachment_id: int
    filename: str
    declared_size: int
    context: str
    download_path: str


@dataclass(frozen=True, slots=True)
class _ParsedPost:
    post_id: int
    text: str
    attachments: tuple[_ParsedAttachment, ...]


@dataclass(frozen=True, slots=True)
class _ParsedThread:
    forum_id: int
    title: str
    posts: tuple[_ParsedPost, ...]
    max_page: int


@dataclass(slots=True)
class _PostBuilder:
    post_id: int
    message: list[str]
    attachments: list[_ParsedAttachment]
    pending_aid: int | None = None
    pending_filename: list[str] | None = None
    pending_tail: list[str] | None = None
    pending_download_path: str | None = None


class _ThreadParser(HTMLParser):
    def __init__(self, thread_id: int) -> None:
        super().__init__(convert_charrefs=True)
        self.thread_id = thread_id
        self.div_depth = 0
        self.post_root_depth: int | None = None
        self.post: _PostBuilder | None = None
        self.posts: list[_ParsedPost] = []
        self.message_pid: int | None = None
        self.message_td_depth = 0
        self.td_depth = 0
        self.title_capture = False
        self.title_parts: list[str] = []
        self.attachment_anchor = False
        self.ignore_depth = 0
        self.forum_ids: set[int] = set()
        self.max_page = 1

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        if tag in {"script", "style"}:
            self.ignore_depth += 1
            return
        if self.ignore_depth:
            return
        if tag == "div":
            self.div_depth += 1
            post_id = values.get("id") or ""
            match = re.fullmatch(r"post_([1-9][0-9]*)", post_id)
            if match is not None:
                self._finish_post()
                self.post = _PostBuilder(int(match.group(1)), [], [])
                self.post_root_depth = self.div_depth
        elif tag == "td":
            self.td_depth += 1
            identity = values.get("id") or ""
            match = re.fullmatch(r"postmessage_([1-9][0-9]*)", identity)
            if match is not None and self.post is not None:
                if int(match.group(1)) == self.post.post_id:
                    self.message_pid = self.post.post_id
                    self.message_td_depth = self.td_depth
        elif tag == "span" and values.get("id") == "thread_subject":
            self.title_capture = True
        elif tag == "a":
            href = values.get("href") or ""
            forum_id = _forum_id_from_href(href)
            if forum_id is not None:
                self.forum_ids.add(forum_id)
            linked = _thread_link(href)
            if linked is not None and linked[0] == self.thread_id:
                self.max_page = max(self.max_page, linked[1])
            identity = values.get("id") or ""
            match = re.fullmatch(r"aid([1-9][0-9]*)", identity)
            attachment_path = (
                None
                if match is None
                else _native_attachment_path(href, int(match.group(1)))
            )
            if (
                match is not None
                and self.post is not None
                and attachment_path is not None
            ):
                self._finish_attachment()
                self.post.pending_aid = int(match.group(1))
                self.post.pending_filename = []
                self.post.pending_tail = []
                self.post.pending_download_path = attachment_path
                self.attachment_anchor = True

    def handle_data(self, data: str) -> None:
        if self.ignore_depth:
            return
        if self.title_capture:
            self.title_parts.append(data)
        if self.post is None:
            return
        if self.attachment_anchor and self.post.pending_filename is not None:
            self.post.pending_filename.append(data)
        elif self.post.pending_tail is not None:
            self.post.pending_tail.append(data)
        if self.message_pid == self.post.post_id:
            self.post.message.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignore_depth:
            self.ignore_depth -= 1
            return
        if self.ignore_depth:
            return
        if tag == "a" and self.attachment_anchor:
            self.attachment_anchor = False
        elif tag == "span" and self.title_capture:
            self.title_capture = False
        elif tag == "td":
            if self.message_pid is not None and self.td_depth == self.message_td_depth:
                self.message_pid = None
            self.td_depth = max(0, self.td_depth - 1)
        elif tag == "div":
            if (
                self.post is not None
                and self.post_root_depth is not None
                and self.div_depth == self.post_root_depth
            ):
                self._finish_post()
            self.div_depth = max(0, self.div_depth - 1)

    def close(self) -> None:
        super().close()
        self._finish_post()

    def _finish_attachment(self) -> None:
        if (
            self.post is None
            or self.post.pending_aid is None
            or self.post.pending_filename is None
            or self.post.pending_tail is None
            or self.post.pending_download_path is None
        ):
            return
        filename = _clean_text(
            " ".join(self.post.pending_filename),
            max_bytes=255,
        )
        tail = _clean_text(" ".join(self.post.pending_tail), max_bytes=1_024)
        match = _SIZE.search(tail)
        if filename and match is not None:
            units = {"b": 1, "kb": 1024, "mb": 1024 * 1024}
            try:
                size = int(Decimal(match.group(1)) * units[match.group(2).casefold()])
            except (InvalidOperation, OverflowError):
                size = -1
            if 0 <= size <= MAX_TOTAL_ARCHIVE_BYTES:
                self.post.attachments.append(
                    _ParsedAttachment(
                        self.post.pending_aid,
                        filename,
                        size,
                        tail,
                        self.post.pending_download_path,
                    )
                )
        self.post.pending_aid = None
        self.post.pending_filename = None
        self.post.pending_tail = None
        self.post.pending_download_path = None

    def _finish_post(self) -> None:
        if self.post is None:
            return
        self._finish_attachment()
        self.posts.append(
            _ParsedPost(
                self.post.post_id,
                _clean_text(" ".join(self.post.message), max_bytes=2_048),
                tuple(self.post.attachments),
            )
        )
        self.post = None
        self.post_root_depth = None
        self.message_pid = None


class AcgripDiscuzParser:
    """Strict parser for the bounded Discuz fragments used by this provider."""

    @staticmethod
    def decode(content: bytes) -> str:
        try:
            value = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _provider_error(
                SubtitleSearchErrorCode.PARSER_DRIFT,
                retryable=False,
            ) from None
        _raise_for_page_markers(value)
        return value

    def search_formhash(self, content: bytes) -> str:
        value = self.decode(content)
        parser = _SearchFormParser()
        parser.feed(value)
        parser.close()
        if parser.formhash is None:
            raise _provider_error(
                SubtitleSearchErrorCode.PARSER_DRIFT,
                retryable=False,
            )
        return parser.formhash

    def search_links(
        self,
        content: bytes,
        *,
        aliases: tuple[str, ...],
    ) -> _SearchLinks:
        value = self.decode(content)
        parser = _SearchLinksParser(aliases)
        parser.feed(value)
        parser.close()
        navigation: str | None = None
        candidates: list[str] = []
        if parser.meta_refresh is not None:
            candidates.append(parser.meta_refresh)
        candidates.extend(
            match.group(0).replace("&amp;", "&")
            for match in re.finditer(
                r"search\.php\?mod=forum(?:&amp;|&)searchid=[1-9][0-9]*"
                r"(?:(?:&amp;|&)(?:orderby|ascdesc|searchsubmit|page)="
                r"[A-Za-z0-9_-]+)*",
                value,
            )
        )
        if not parser.thread_ids:
            for candidate in candidates:
                try:
                    navigation = _validated_path(candidate)
                    break
                except SubtitleSearchProviderError:
                    continue
        next_path = (
            None
            if parser.next_path is None
            else _validated_path(parser.next_path)
        )
        no_results = any(
            marker in value
            for marker in ("没有找到匹配结果", "对不起，没有找到匹配结果", "搜索结果: 0")
        )
        if not parser.thread_ids and navigation is None and not no_results:
            raise _provider_error(
                SubtitleSearchErrorCode.PARSER_DRIFT,
                retryable=False,
            )
        return _SearchLinks(
            tuple(parser.thread_ids),
            navigation,
            next_path,
        )

    def thread(
        self,
        content: bytes,
        *,
        thread_id: int,
    ) -> _ParsedThread:
        value = self.decode(content)
        parser = _ThreadParser(thread_id)
        parser.feed(value)
        parser.close()
        title = _clean_text(" ".join(parser.title_parts), max_bytes=240)
        allowed_fids = parser.forum_ids & set(ACGRIP_FORUM_IDS)
        if (
            not title
            or len(allowed_fids) != 1
            or not parser.posts
            or any(item.post_id < 1 for item in parser.posts)
        ):
            raise _provider_error(
                SubtitleSearchErrorCode.PARSER_DRIFT,
                retryable=False,
            )
        return _ParsedThread(
            next(iter(allowed_fids)),
            title,
            tuple(parser.posts),
            parser.max_page,
        )


@dataclass(frozen=True, slots=True)
class _ArchiveGroup:
    format: SubtitleArchiveFormat
    attachments: tuple[_ParsedAttachment, ...]
    warnings: tuple[str, ...]


def _archive_groups(
    attachments: tuple[_ParsedAttachment, ...],
) -> tuple[_ArchiveGroup, ...]:
    supported = tuple(
        item
        for item in attachments
        if item.filename.casefold().endswith((".zip", ".7z", ".rar"))
        or _OLD_RAR.fullmatch(item.filename) is not None
    )
    groups: list[_ArchiveGroup] = []
    consumed: set[int] = set()
    part_groups: dict[str, list[tuple[int, _ParsedAttachment]]] = {}
    old_groups: dict[str, list[tuple[int, _ParsedAttachment]]] = {}
    rar_heads: dict[str, _ParsedAttachment] = {}
    for item in supported:
        name = unicodedata.normalize("NFKC", item.filename).casefold()
        part = _PART_RAR.fullmatch(name)
        old = _OLD_RAR.fullmatch(name)
        if part is not None:
            part_groups.setdefault(part.group(1), []).append((int(part.group(2)), item))
        elif old is not None:
            old_groups.setdefault(old.group(1), []).append((int(old.group(2)), item))
        elif name.endswith(".rar"):
            rar_heads[name[:-4]] = item
    for values in part_groups.values():
        ordered = sorted(values)
        indexes = tuple(item[0] for item in ordered)
        if (
            indexes == tuple(range(1, len(indexes) + 1))
            and len(indexes) <= MAX_ARCHIVE_VOLUMES
            and len({item[1].attachment_id for item in ordered}) == len(ordered)
        ):
            selected = tuple(item[1] for item in ordered)
            consumed.update(item.attachment_id for item in selected)
            groups.append(
                _ArchiveGroup(
                    SubtitleArchiveFormat.RAR,
                    selected,
                    ("multipart_header_unverified",),
                )
            )
    for stem, values in old_groups.items():
        head = rar_heads.get(stem)
        ordered = sorted(values)
        indexes = tuple(item[0] for item in ordered)
        if (
            head is not None
            and indexes == tuple(range(0, len(indexes)))
            and len(indexes) + 1 <= MAX_ARCHIVE_VOLUMES
        ):
            selected = (head, *(item[1] for item in ordered))
            consumed.update(item.attachment_id for item in selected)
            groups.append(
                _ArchiveGroup(
                    SubtitleArchiveFormat.RAR,
                    selected,
                    ("multipart_header_unverified",),
                )
            )
    for item in supported:
        if item.attachment_id in consumed:
            continue
        name = item.filename.casefold()
        if _PART_RAR.fullmatch(name) or _OLD_RAR.fullmatch(name):
            continue
        if name.endswith(".zip"):
            format = SubtitleArchiveFormat.ZIP
        elif name.endswith(".7z"):
            format = SubtitleArchiveFormat.SEVEN_Z
        elif name.endswith(".rar"):
            if name[:-4] in old_groups:
                continue
            format = SubtitleArchiveFormat.RAR
        else:
            continue
        groups.append(_ArchiveGroup(format, (item,), ()))
    return tuple(
        group
        for group in groups
        if sum(item.declared_size for item in group.attachments)
        <= MAX_TOTAL_ARCHIVE_BYTES
    )


def _hints(value: str) -> tuple[tuple[str, ...], str]:
    folded = unicodedata.normalize("NFKC", value).casefold()
    languages: list[str] = []
    for hint, markers in (
        ("zh-hans", ("简体", "简中", "chs", " sc", "简繁")),
        ("zh-hant", ("繁体", "繁中", "cht", " tc", "简繁")),
        ("ja", ("日语", "日文", " jp", "jpn", "中日")),
        ("en", ("英语", "英文", " eng")),
    ):
        if any(marker in folded for marker in markers):
            languages.append(hint)
    coverage = " ".join(
        dict.fromkeys(
            _clean_text(match.group(0), max_bytes=32)
            for match in _COVERAGE.finditer(value)
        )
    )
    return tuple(languages[:8]), coverage[:160]


def _release_group_hints(value: str) -> tuple[str, ...]:
    hints: list[str] = []
    for match in re.finditer(r"[\[【]([^\]】]{1,64})[\]】]", value):
        hint = _clean_text(match.group(1), max_bytes=64)
        if hint and "http" not in hint.casefold() and hint not in hints:
            hints.append(hint)
    return tuple(hints[:8])


@dataclass(frozen=True, slots=True)
class _CachedSearch:
    signature: tuple[tuple[str, ...], int, int]
    offset: int
    items: tuple[SubtitleReleaseSummary, ...]
    capabilities: tuple[SubtitleArchiveSetCapability, ...]


class AcgripSubtitleSearchProvider:
    """Fixed-origin, read-only ACG.RIP search provider with run-local IDs."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=ACGRIP_ORIGIN,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "reeloom/0.1 subtitle-search",
            },
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._clock = clock
        self._sleep = sleep
        self._parser = AcgripDiscuzParser()
        self._response_count = 0
        self._total_bytes = 0
        self._last_request_at: float | None = None
        self._release_ids: dict[tuple[int, int], SubtitleReleaseId] = {}
        self._archive_ids: dict[tuple[int, int, tuple[int, ...]], SubtitleArchiveSetId] = {}
        self._cursor_counter = 0
        self._cursors: dict[SubtitleSearchCursorId, _CachedSearch] = {}

    @property
    def provider_version(self) -> str:
        return (
            f"{CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION}+"
            f"{CURRENT_SUBTITLE_SEARCH_PARSER_VERSION}"
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(
        self,
        request: SubtitleSearchRequest,
    ) -> SubtitleSearchResult:
        if not isinstance(request, SubtitleSearchRequest):
            raise _provider_error(
                SubtitleSearchErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            )
        signature = (
            request.title_aliases,
            request.season_number,
            request.limit,
        )
        if request.cursor is not None:
            cached = self._cursors.pop(request.cursor, None)
            if cached is None or cached.signature != signature:
                raise _provider_error(
                    SubtitleSearchErrorCode.CAPABILITY_UNAVAILABLE,
                    retryable=False,
                )
            return self._page(cached)
        try:
            async with asyncio.timeout(ACGRIP_TOOL_TIMEOUT_SECONDS):
                items, capabilities = await self._collect(request)
        except TimeoutError:
            raise _provider_error(
                SubtitleSearchErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None
        return self._page(
            _CachedSearch(signature, 0, items, capabilities)
        )

    def _page(self, cached: _CachedSearch) -> SubtitleSearchResult:
        limit = cached.signature[2]
        end = min(cached.offset + limit, len(cached.items))
        page_items = cached.items[cached.offset:end]
        archive_ids = {
            archive.archive_set_id
            for release in page_items
            for archive in release.archive_sets
        }
        page_capabilities = tuple(
            item
            for item in cached.capabilities
            if item.archive_set_id in archive_ids
        )
        if end < len(cached.items):
            self._cursor_counter += 1
            cursor = SubtitleSearchCursorId(self._cursor_counter)
            self._cursors[cursor] = _CachedSearch(
                cached.signature,
                end,
                cached.items,
                cached.capabilities,
            )
        else:
            cursor = None
        return SubtitleSearchResult(
            SubtitleSearchPage(
                page_items,
                cursor,
                cursor is None,
            ),
            page_capabilities,
        )

    async def _collect(
        self,
        request: SubtitleSearchRequest,
    ) -> tuple[
        tuple[SubtitleReleaseSummary, ...],
        tuple[SubtitleArchiveSetCapability, ...],
    ]:
        landing = await self._request("GET", _SEARCH_PATH)
        formhash = self._parser.search_formhash(landing)
        thread_ids: list[int] = []
        for alias in request.title_aliases:
            body = urlencode(
                (
                    ("formhash", formhash),
                    ("searchsubmit", "yes"),
                    ("srchtxt", alias),
                    ("srchfid[]", str(ACGRIP_FORUM_IDS[0])),
                    ("srchfid[]", str(ACGRIP_FORUM_IDS[1])),
                )
            ).encode("ascii")
            response = await self._request(
                "POST",
                _SEARCH_PATH,
                body=body,
                allow_search_result_redirect=True,
            )
            links = self._parser.search_links(
                response,
                aliases=request.title_aliases,
            )
            if links.navigation_path is not None:
                response = await self._request("GET", links.navigation_path)
                links = self._parser.search_links(
                    response,
                    aliases=request.title_aliases,
                )
            while True:
                for thread_id in links.thread_ids:
                    if thread_id not in thread_ids:
                        thread_ids.append(thread_id)
                        if len(thread_ids) >= MAX_SEARCH_RESULTS_PER_RUN:
                            break
                if (
                    len(thread_ids) >= MAX_SEARCH_RESULTS_PER_RUN
                    or links.next_path is None
                ):
                    break
                response = await self._request("GET", links.next_path)
                links = self._parser.search_links(
                    response,
                    aliases=request.title_aliases,
                )
        releases: list[SubtitleReleaseSummary] = []
        capabilities: list[SubtitleArchiveSetCapability] = []
        attachment_count = 0
        seen_posts: set[tuple[int, int]] = set()
        for thread_id in thread_ids:
            pages: list[_ParsedThread] = []
            first = self._parser.thread(
                await self._request("GET", f"/thread-{thread_id}-1-1.html"),
                thread_id=thread_id,
            )
            pages.append(first)
            for page_number in range(2, first.max_page + 1):
                pages.append(
                    self._parser.thread(
                        await self._request(
                            "GET",
                            f"/thread-{thread_id}-{page_number}-1.html",
                        ),
                        thread_id=thread_id,
                    )
                )
            for page in pages:
                if page.forum_id not in ACGRIP_FORUM_IDS:
                    continue
                for post in page.posts:
                    post_key = (thread_id, post.post_id)
                    if post_key in seen_posts:
                        continue
                    seen_posts.add(post_key)
                    attachment_count += len(post.attachments)
                    if attachment_count > ACGRIP_MAX_ATTACHMENTS:
                        raise _provider_error(
                            SubtitleSearchErrorCode.BUDGET_EXCEEDED,
                            retryable=False,
                        )
                    built = self._release(
                        thread_id=thread_id,
                        title=page.title,
                        post=post,
                        aliases=request.title_aliases,
                        season_number=request.season_number,
                    )
                    if built is None:
                        continue
                    release, release_capabilities = built
                    releases.append(release)
                    capabilities.extend(release_capabilities)
                    if len(releases) >= MAX_SEARCH_RESULTS_PER_RUN:
                        return tuple(releases), tuple(capabilities)
        return tuple(releases), tuple(capabilities)

    def _release(
        self,
        *,
        thread_id: int,
        title: str,
        post: _ParsedPost,
        aliases: tuple[str, ...],
        season_number: int,
    ) -> tuple[
        SubtitleReleaseSummary,
        tuple[SubtitleArchiveSetCapability, ...],
    ] | None:
        groups = _archive_groups(post.attachments)
        if not groups or not _matches_alias(title, aliases):
            return None
        release_key = (thread_id, post.post_id)
        release_id = self._release_ids.setdefault(
            release_key,
            SubtitleReleaseId(len(self._release_ids) + 1),
        )
        summaries: list[SubtitleArchiveSetSummary] = []
        capabilities: list[SubtitleArchiveSetCapability] = []
        warnings: set[str] = set()
        context = " ".join(
            (
                title,
                post.text,
                *(item.context for item in post.attachments),
                *(item.filename for item in post.attachments),
            )
        )
        languages, coverage = _hints(context)
        for group in groups:
            attachment_ids = tuple(
                item.attachment_id for item in group.attachments
            )
            archive_key = (thread_id, post.post_id, attachment_ids)
            archive_id = self._archive_ids.setdefault(
                archive_key,
                SubtitleArchiveSetId(len(self._archive_ids) + 1),
            )
            declared_size = sum(
                item.declared_size for item in group.attachments
            )
            group_context = " ".join(
                (
                    title,
                    *(item.context for item in group.attachments),
                    *(item.filename for item in group.attachments),
                )
            )
            group_languages, group_coverage = _hints(group_context)
            summaries.append(
                SubtitleArchiveSetSummary(
                    archive_id,
                    group.format,
                    len(group.attachments),
                    declared_size,
                    label_hint=_clean_text(
                        " + ".join(
                            item.filename for item in group.attachments
                        ),
                        max_bytes=240,
                    ),
                    coverage_hint=group_coverage,
                    language_hints=group_languages,
                    release_group_hints=_release_group_hints(
                        group_context
                    ),
                    warnings=tuple(sorted(group.warnings)),
                )
            )
            capabilities.append(
                SubtitleArchiveSetCapability(
                    archive_id,
                    release_id,
                    group.format,
                    thread_id,
                    post.post_id,
                    attachment_ids,
                    declared_size,
                )
            )
            warnings.update(group.warnings)
        reasons = ["title_alias"]
        if coverage and (
            f"s{season_number:02d}" in coverage.casefold()
            or season_number == 1
        ):
            reasons.append("season_hint")
        excerpt = _clean_text(
            " ".join(
                (
                    post.text,
                    *(item.context for item in post.attachments),
                )
            ),
            max_bytes=512,
        )
        return (
            SubtitleReleaseSummary(
                release_id,
                tuple(summaries),
                title,
                excerpt,
                coverage,
                languages,
                (),
                tuple(reasons),
                tuple(sorted(warnings)),
                True,
            ),
            tuple(capabilities),
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        allow_search_result_redirect: bool = False,
    ) -> bytes:
        path = _validated_path(path)
        if method not in {"GET", "POST"} or (method == "POST") != (body is not None):
            raise _provider_error(
                SubtitleSearchErrorCode.CAPABILITY_UNAVAILABLE,
                retryable=False,
            )
        if self._response_count >= ACGRIP_MAX_HTTP_RESPONSES:
            raise _provider_error(
                SubtitleSearchErrorCode.BUDGET_EXCEEDED,
                retryable=False,
            )
        now = self._clock()
        if self._last_request_at is not None:
            delay = ACGRIP_REQUEST_INTERVAL_SECONDS - (
                now - self._last_request_at
            )
            if delay > 0:
                await self._sleep(delay)
                now = self._clock()
        self._last_request_at = now
        headers: Mapping[str, str] | None = None
        if body is not None:
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
        redirect_path: str | None = None
        try:
            async with self._client.stream(
                method,
                path,
                content=body,
                headers=headers,
                timeout=ACGRIP_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                self._response_count += 1
                _retain_anonymous_session_cookies(self._client)
                status = response.status_code
                if 300 <= status < 400:
                    locations = response.headers.get_list("location")
                    if not (
                        allow_search_result_redirect
                        and method == "POST"
                        and path == _SEARCH_PATH
                        and status in {302, 303}
                        and len(locations) == 1
                    ):
                        raise _provider_error(
                            SubtitleSearchErrorCode.PARSER_DRIFT,
                            retryable=False,
                        )
                    redirect_path = _validated_search_result_path(
                        locations[0],
                        allow_same_origin_absolute=True,
                    )
                if redirect_path is not None:
                    pass
                elif status == 429:
                    raise _provider_error(
                        SubtitleSearchErrorCode.RATE_LIMITED,
                        retryable=True,
                    )
                elif status in {401, 403}:
                    raise _provider_error(
                        SubtitleSearchErrorCode.CHALLENGE_OR_LOGIN,
                        retryable=False,
                    )
                elif status >= 500:
                    raise _provider_error(
                        SubtitleSearchErrorCode.UNAVAILABLE,
                        retryable=True,
                    )
                elif not 200 <= status < 300:
                    raise _provider_error(
                        SubtitleSearchErrorCode.PARSER_DRIFT,
                        retryable=False,
                    )
                if redirect_path is not None:
                    content_type = ""
                else:
                    content_type = response.headers.get("content-type", "")
                if content_type and "text/html" not in content_type.casefold():
                    raise _provider_error(
                        SubtitleSearchErrorCode.PARSER_DRIFT,
                        retryable=False,
                    )
                declared = response.headers.get("content-length")
                if (
                    declared is not None
                    and declared.isdigit()
                    and int(declared) > ACGRIP_MAX_RESPONSE_BYTES
                ):
                    raise _provider_error(
                        SubtitleSearchErrorCode.RESPONSE_TOO_LARGE,
                        retryable=False,
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > ACGRIP_MAX_RESPONSE_BYTES:
                        raise _provider_error(
                            SubtitleSearchErrorCode.RESPONSE_TOO_LARGE,
                            retryable=False,
                        )
                self._total_bytes += len(content)
                if self._total_bytes > ACGRIP_MAX_TOTAL_HTML_BYTES:
                    raise _provider_error(
                        SubtitleSearchErrorCode.BUDGET_EXCEEDED,
                        retryable=False,
                    )
                if redirect_path is None:
                    return bytes(content)
            if redirect_path is not None:
                return await self._request("GET", redirect_path)
            raise AssertionError("unreachable response state")
        except SubtitleSearchProviderError:
            raise
        except httpx.TimeoutException:
            raise _provider_error(
                SubtitleSearchErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None
        except httpx.TransportError:
            raise _provider_error(
                SubtitleSearchErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None


def _archive_fetch_error(
    code: SubtitleArchiveErrorCode,
    *,
    retryable: bool = False,
) -> SubtitleArchiveError:
    return SubtitleArchiveError(code, retryable=retryable)


class AcgripSubtitleArchiveFetcher:
    """Re-resolve stable native attachment identities into an isolated root."""

    def __init__(
        self,
        workspace: AuthorizedRoot,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not isinstance(workspace, AuthorizedRoot):
            raise TypeError("workspace must be an AuthorizedRoot")
        self._workspace = workspace
        self._client = httpx.AsyncClient(
            base_url=ACGRIP_ORIGIN,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/octet-stream",
                "User-Agent": "reeloom/0.1 subtitle-acquisition",
            },
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._clock = clock
        self._sleep = sleep
        self._parser = AcgripDiscuzParser()
        self._response_count = 0
        self._html_bytes = 0
        self._download_bytes = 0
        self._last_request_at: float | None = None

    @property
    def provider_version(self) -> str:
        return CURRENT_SUBTITLE_SEARCH_PROVIDER_VERSION

    @property
    def parser_version(self) -> str:
        return CURRENT_SUBTITLE_SEARCH_PARSER_VERSION

    @property
    def workspace_root(self) -> Path:
        return self._workspace.path

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(
        self,
        capability: SubtitleArchiveSetCapability,
    ) -> DownloadedSubtitleArchiveSet:
        if not isinstance(capability, SubtitleArchiveSetCapability):
            raise _archive_fetch_error(SubtitleArchiveErrorCode.CAPABILITY_CHANGED)
        try:
            async with asyncio.timeout(ACGRIP_TOOL_TIMEOUT_SECONDS):
                attachments = await self._resolve(capability)
                return await self._download_set(capability, attachments)
        except SubtitleArchiveError:
            raise
        except SubtitleSearchProviderError as error:
            raise _archive_fetch_error(
                SubtitleArchiveErrorCode.CAPABILITY_CHANGED,
                retryable=error.retryable,
            ) from None
        except TimeoutError:
            raise _archive_fetch_error(
                SubtitleArchiveErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None

    async def _resolve(
        self,
        capability: SubtitleArchiveSetCapability,
    ) -> tuple[_ParsedAttachment, ...]:
        first = self._parser.thread(
            await self._request_html(
                f"/thread-{capability.thread_id}-1-1.html"
            ),
            thread_id=capability.thread_id,
        )
        pages = [first]
        for page_number in range(2, first.max_page + 1):
            pages.append(
                self._parser.thread(
                    await self._request_html(
                        f"/thread-{capability.thread_id}-{page_number}-1.html"
                    ),
                    thread_id=capability.thread_id,
                )
            )
        matches = [
            post
            for page in pages
            if page.forum_id in ACGRIP_FORUM_IDS
            for post in page.posts
            if post.post_id == capability.post_id
        ]
        if len(matches) != 1:
            raise _archive_fetch_error(SubtitleArchiveErrorCode.CAPABILITY_CHANGED)
        groups = [
            group
            for group in _archive_groups(matches[0].attachments)
            if group.format is capability.format
            and tuple(item.attachment_id for item in group.attachments)
            == capability.attachment_ids
        ]
        if len(groups) != 1:
            raise _archive_fetch_error(SubtitleArchiveErrorCode.CAPABILITY_CHANGED)
        return groups[0].attachments

    async def _download_set(
        self,
        capability: SubtitleArchiveSetCapability,
        attachments: tuple[_ParsedAttachment, ...],
    ) -> DownloadedSubtitleArchiveSet:
        root_fd: int | None = None
        attempt_fd: int | None = None
        try:
            no_follow = getattr(os, "O_NOFOLLOW", None)
            if no_follow is None:
                raise OSError("O_NOFOLLOW unavailable")
            root_fd = os.open(
                self._workspace.path,
                os.O_RDONLY
                | os.O_DIRECTORY
                | no_follow
                | getattr(os, "O_CLOEXEC", 0),
            )
            root_stat = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or (root_stat.st_dev, root_stat.st_ino)
                != (self._workspace.device, self._workspace.inode)
            ):
                raise OSError("workspace identity changed")
            attempt_name = (
                "subtitle-acquire-"
                + hashlib.sha256(
                    (
                        f"{capability.thread_id}:{capability.post_id}:"
                        + ",".join(str(item) for item in capability.attachment_ids)
                    ).encode("ascii")
                ).hexdigest()[:16]
                + "-"
                + os.urandom(12).hex()
            )
            os.mkdir(attempt_name, mode=0o700, dir_fd=root_fd)
            attempt_fd = os.open(
                attempt_name,
                os.O_RDONLY
                | os.O_DIRECTORY
                | no_follow
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=root_fd,
            )
            attempt_path = self._workspace.path / attempt_name
            volumes: list[DownloadedArchiveVolume] = []
            for index, attachment in enumerate(attachments, start=1):
                volumes.append(
                    await self._download_volume(
                        attempt_fd,
                        attempt_path,
                        attachment,
                        index=index,
                    )
                )
            os.fsync(attempt_fd)
            os.fsync(root_fd)
            return DownloadedSubtitleArchiveSet(capability, tuple(volumes))
        except SubtitleArchiveError:
            raise
        except OSError:
            raise _archive_fetch_error(
                SubtitleArchiveErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None
        finally:
            if attempt_fd is not None:
                os.close(attempt_fd)
            if root_fd is not None:
                os.close(root_fd)

    async def _download_volume(
        self,
        directory_fd: int,
        directory_path: Path,
        attachment: _ParsedAttachment,
        *,
        index: int,
    ) -> DownloadedArchiveVolume:
        if index > MAX_ARCHIVE_VOLUMES:
            raise _archive_fetch_error(SubtitleArchiveErrorCode.DOWNLOAD_TOO_LARGE)
        filename = f"volume-{index:02d}.bin"
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise _archive_fetch_error(SubtitleArchiveErrorCode.UNAVAILABLE)
        descriptor: int | None = None
        try:
            descriptor = os.open(
                filename,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | no_follow
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory_fd,
            )
            digest = hashlib.sha256()
            size = 0
            await self._pace()
            async with self._client.stream(
                "GET",
                attachment.download_path,
                timeout=ACGRIP_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                self._response_count += 1
                _retain_anonymous_session_cookies(self._client)
                self._check_status(response.status_code)
                content_type = response.headers.get("content-type", "")
                if "text/html" in content_type.casefold():
                    raise _archive_fetch_error(
                        SubtitleArchiveErrorCode.CAPABILITY_CHANGED
                    )
                declared = response.headers.get("content-length")
                if declared is not None and (
                    not declared.isdigit()
                    or int(declared) > MAX_ARCHIVE_VOLUME_BYTES
                ):
                    raise _archive_fetch_error(
                        SubtitleArchiveErrorCode.DOWNLOAD_TOO_LARGE
                    )
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    self._download_bytes += len(chunk)
                    if (
                        size > MAX_ARCHIVE_VOLUME_BYTES
                        or self._download_bytes > MAX_TOTAL_ARCHIVE_BYTES
                    ):
                        raise _archive_fetch_error(
                            SubtitleArchiveErrorCode.DOWNLOAD_TOO_LARGE
                        )
                    remaining = memoryview(chunk)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise OSError("short archive write")
                        remaining = remaining[written:]
                    digest.update(chunk)
            if size < 1:
                raise _archive_fetch_error(SubtitleArchiveErrorCode.CONTENT_DRIFT)
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
                raise _archive_fetch_error(SubtitleArchiveErrorCode.CONTENT_DRIFT)
            return DownloadedArchiveVolume(
                SubtitleArchiveVolume(
                    index,
                    attachment.attachment_id,
                    size,
                    digest.hexdigest(),
                ),
                directory_path / filename,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
        except SubtitleArchiveError:
            raise
        except (httpx.TimeoutException, httpx.TransportError, OSError):
            raise _archive_fetch_error(
                SubtitleArchiveErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    async def _request_html(self, path: str) -> bytes:
        if _THREAD_PATH.fullmatch(path) is None:
            raise _archive_fetch_error(SubtitleArchiveErrorCode.CAPABILITY_CHANGED)
        await self._pace()
        try:
            async with self._client.stream(
                "GET",
                path,
                timeout=ACGRIP_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                self._response_count += 1
                _retain_anonymous_session_cookies(self._client)
                self._check_status(response.status_code)
                content_type = response.headers.get("content-type", "")
                if content_type and "text/html" not in content_type.casefold():
                    raise _archive_fetch_error(
                        SubtitleArchiveErrorCode.CAPABILITY_CHANGED
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > ACGRIP_MAX_RESPONSE_BYTES:
                        raise _archive_fetch_error(
                            SubtitleArchiveErrorCode.DOWNLOAD_TOO_LARGE
                        )
                self._html_bytes += len(content)
                if self._html_bytes > ACGRIP_MAX_TOTAL_HTML_BYTES:
                    raise _archive_fetch_error(
                        SubtitleArchiveErrorCode.DOWNLOAD_TOO_LARGE
                    )
                return bytes(content)
        except SubtitleArchiveError:
            raise
        except SubtitleSearchProviderError as error:
            raise _archive_fetch_error(
                SubtitleArchiveErrorCode.CAPABILITY_CHANGED,
                retryable=error.retryable,
            ) from None
        except (httpx.TimeoutException, httpx.TransportError):
            raise _archive_fetch_error(
                SubtitleArchiveErrorCode.UNAVAILABLE,
                retryable=True,
            ) from None

    async def _pace(self) -> None:
        if self._response_count >= ACGRIP_MAX_HTTP_RESPONSES:
            raise _archive_fetch_error(SubtitleArchiveErrorCode.DOWNLOAD_TOO_LARGE)
        now = self._clock()
        if self._last_request_at is not None:
            delay = ACGRIP_REQUEST_INTERVAL_SECONDS - (now - self._last_request_at)
            if delay > 0:
                await self._sleep(delay)
                now = self._clock()
        self._last_request_at = now

    @staticmethod
    def _check_status(status: int) -> None:
        if 300 <= status < 400 or status in {401, 403}:
            raise _archive_fetch_error(SubtitleArchiveErrorCode.CAPABILITY_CHANGED)
        if status == 429 or status >= 500:
            raise _archive_fetch_error(
                SubtitleArchiveErrorCode.UNAVAILABLE,
                retryable=True,
            )
        if not 200 <= status < 300:
            raise _archive_fetch_error(SubtitleArchiveErrorCode.CAPABILITY_CHANGED)
