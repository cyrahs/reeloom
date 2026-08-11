"""Run notifications.

One message per settled run, sent directly. No outbox, no projector, no
delivery ledger: a notification that fails to send is logged and dropped,
because the run's own record is the durable one.
"""

from __future__ import annotations

import logging

from reeloom.adapters.telegram import TelegramClient, TelegramError
from reeloom.models import Run, RunState, WatchConfig

_LOGGER = logging.getLogger(__name__)

_HEADLINE = {
    RunState.DONE: "✅ 整理完成",
    RunState.NEEDS_ATTENTION: "⚠️ 需要处理",
    RunState.FAILED: "❌ 整理失败",
    RunState.DISCARDED: "🗑 已放弃",
}


def render(run: Run, config: WatchConfig) -> str:
    lines = [
        f"{_HEADLINE.get(run.state, run.state.value)} · {config.name}",
        run.folder_name,
    ]
    if run.plan:
        identity = run.plan.identity
        lines.append(f"{identity.title} ({identity.year})")

    if run.result:
        result = run.result
        summary = [f"移动 {result.moved}"]
        if result.archived:
            summary.append(f"归档 {result.archived}")
        if result.subtitles_acquired:
            summary.append(f"字幕 {result.subtitles_acquired}")
        lines.append(" · ".join(summary))
        if result.duplicates:
            lines.append(f"重复（已入 fail）：{_join(result.duplicates)}")
        if result.missing:
            lines.append(f"缺失：{_join(result.missing)}")
        if result.subtitle_note:
            lines.append(f"字幕：{result.subtitle_note}")

    if run.error:
        code = run.error.get("code", "error")
        detail = run.error.get("reason") or run.error.get("detail") or ""
        lines.append(f"原因：{code}{f' — {detail}' if detail else ''}")

    if run.plan and run.plan.unmapped:
        lines.append(f"未映射 {len(run.plan.unmapped)} 个文件（已归档）")

    return "\n".join(lines)


def _join(values: tuple[str, ...], limit: int = 5) -> str:
    shown = ", ".join(values[:limit])
    return shown if len(values) <= limit else f"{shown} 等 {len(values)} 个"


class TelegramNotifier:
    """Implements the worker's ``Notifier`` protocol."""

    def __init__(self, clients) -> None:
        self._clients = clients

    async def run_settled(self, run: Run, config: WatchConfig) -> None:
        credentials = await self._clients.telegram()
        if credentials is None:
            return
        token, chat_id = credentials
        try:
            client = TelegramClient(bot_token=token, chat_id=chat_id)
        except TelegramError as error:
            _LOGGER.warning("telegram not usable: %s", error.code)
            return
        try:
            if not await client.send(render(run, config)):
                _LOGGER.info("notification dropped for run=%s", run.id)
        finally:
            await client.aclose()
