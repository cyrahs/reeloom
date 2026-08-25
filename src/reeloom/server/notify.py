"""Run notifications.

One message per settled run, sent directly. No outbox, no projector, no
delivery ledger: a notification that fails to send is logged and dropped,
because the run's own record is the durable one.
"""

from __future__ import annotations

import logging
from html import escape

import httpx

from reeloom.adapters.telegram import TelegramClient, TelegramError
from reeloom.models import (
    MediaIdentity,
    MediaType,
    ReeloomError,
    Run,
    RunState,
    WatchConfig,
)

_LOGGER = logging.getLogger(__name__)

_BRAND = "REELOOM"
_TMDB_WEB = "https://www.themoviedb.org"
_HEADLINE = {
    RunState.DONE: "✅ 整理完成",
    RunState.NEEDS_ATTENTION: "⚠️ 需要处理",
    RunState.FAILED: "❌ 整理失败",
    RunState.DISCARDED: "🗑 已放弃",
}
# States that need a human: their notification is pinned so it stays visible
# until someone deals with it.
_PIN_STATES = frozenset({RunState.NEEDS_ATTENTION, RunState.FAILED})


def render(run: Run, config: WatchConfig, public_url: str = "") -> str:
    """Telegram HTML. Every interpolated value is untrusted and escaped."""

    lines = [
        f"{_BRAND} · {_text(config.name)}",
        _HEADLINE.get(run.state, run.state.value),
    ]
    if run.plan:
        identity = run.plan.identity
        lines.append(_title(identity, _title_url(run.id, identity, public_url)))

    if run.result:
        result = run.result
        summary = [f"移动 {result.moved}"]
        if result.archived:
            summary.append(f"归档 {result.archived}")
        if result.subtitles_moved:
            summary.append(f"字幕 {result.subtitles_moved}")
        if result.subtitles_acquired:
            summary.append(f"下载字幕 {result.subtitles_acquired}")
        if result.subtitles_embedded:
            summary.append(f"内封 {result.subtitles_embedded}")
        if result.replaced:
            summary.append(f"洗版 {len(result.replaced)}")
        duplicate_count = len(result.discarded) + len(result.duplicates)
        if duplicate_count:
            summary.append(f"重复 {duplicate_count}")
        lines.append(" · ".join(summary))
        if result.missing:
            lines.append(f"缺失：{_join(result.missing)}")
        if result.subtitle_note:
            lines.append(f"字幕：{_text(result.subtitle_note)}")

    if run.error:
        code = str(run.error.get("code", "error"))
        if code == "replace_confirmation":
            lines.append("等待洗版确认：请在网页端选择替换、丢弃或共存")
        else:
            detail = _text(
                str(run.error.get("reason") or run.error.get("detail") or "")
            )
            lines.append(f"原因：{_text(code)}{f' — {detail}' if detail else ''}")

    return "\n".join(lines)


def _title_url(run_id: str, identity: MediaIdentity, public_url: str) -> str:
    """The run's page when the deployment knows its own URL, else TMDB.
    Every part is trusted: ``run_id`` is a UUID, ``tmdb_id`` an int, and
    ``public_url`` comes from the operator's environment."""

    if public_url:
        return f"{public_url}/#/runs/{run_id}"
    kind = "movie" if identity.media_type is MediaType.MOVIE else "tv"
    return f"{_TMDB_WEB}/{kind}/{identity.tmdb_id}"


def _title(identity: MediaIdentity, url: str) -> str:
    return f'<b><a href="{url}">{_text(identity.title)} ({identity.year})</a></b>'


def _text(value: str) -> str:
    return escape(value, quote=False)


def _join(values: tuple[str, ...], limit: int = 5) -> str:
    shown = ", ".join(_text(value) for value in values[:limit])
    return shown if len(values) <= limit else f"{shown} 等 {len(values)} 个"


class TelegramNotifier:
    """Implements the worker's ``Notifier`` protocol."""

    def __init__(
        self,
        clients,
        *,
        public_url: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._clients = clients
        self._public_url = public_url
        self._transport = transport

    async def run_settled(self, run: Run, config: WatchConfig) -> None:
        client = await self._client()
        if client is None:
            return
        text = render(run, config, self._public_url)
        poster = await self._poster(run)
        try:
            message_id = None
            if poster is not None:
                message_id = await client.send_photo(poster, text)
            if message_id is None:
                message_id = await client.send(text)
            if message_id is None:
                _LOGGER.info("notification dropped for run=%s", run.id)
            elif message_id and await self._should_pin(run):
                await client.pin(message_id)
        finally:
            await client.aclose()

    async def send_test(self) -> str | None:
        """Send one plain and one needs-attention test message; the second is
        pinned when pinning is enabled. Returns an error code, or None."""

        client = await self._client()
        if client is None:
            return "telegram_not_configured"
        try:
            plain = f"{_BRAND} · 测试通知\n{_HEADLINE[RunState.DONE]}\n这是一条普通测试通知"
            if await client.send(plain) is None:
                return "send_failed"
            attention = (
                f"{_BRAND} · 测试通知\n{_HEADLINE[RunState.NEEDS_ATTENTION]}\n"
                "这是一条需要处理的测试通知"
            )
            message_id = await client.send(attention)
            if message_id is None:
                return "send_failed"
            if message_id and await self._pin_enabled():
                await client.pin(message_id)
            return None
        finally:
            await client.aclose()

    async def _client(self) -> TelegramClient | None:
        credentials = await self._clients.telegram()
        if credentials is None:
            return None
        token, chat_id = credentials
        try:
            return TelegramClient(
                bot_token=token, chat_id=chat_id, transport=self._transport
            )
        except TelegramError as error:
            _LOGGER.warning("telegram not usable: %s", error.code)
            return None

    async def _should_pin(self, run: Run) -> bool:
        return run.state in _PIN_STATES and await self._pin_enabled()

    async def _pin_enabled(self) -> bool:
        return await self._clients.telegram_pin_alerts()

    async def _poster(self, run: Run) -> str | None:
        """Poster URL for the identified work; None means send text only."""

        if run.plan is None:
            return None
        identity = run.plan.identity
        try:
            tmdb = await self._clients.tmdb()
            return await tmdb.poster_url(
                identity.tmdb_id,
                movie=identity.media_type is MediaType.MOVIE,
            )
        except ReeloomError as error:
            _LOGGER.info("poster lookup skipped: %s", error.code)
            return None
