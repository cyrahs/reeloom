from __future__ import annotations

import httpx
import pytest

from reeloom.adapters.telegram import TelegramClient, TelegramError, validate
from reeloom.models import (
    MediaIdentity,
    MediaType,
    Plan,
    Run,
    RunResult,
    RunState,
    WatchConfig,
)
from reeloom.server.notify import TelegramNotifier, render

TOKEN = "123456789:AAEEabcdefghijklmnopqrstuvwxyz012345"
CHAT = "-1001234567890"
IDENTITY = MediaIdentity(MediaType.ANIME, 123, "Show", 2024)


def make_run(**kwargs) -> Run:
    return Run(
        id="run-1",
        config_id="config-1",
        folder_name="[Group] Show S01",
        state=kwargs.pop("state", RunState.DONE),
        **kwargs,
    )


def test_rejects_malformed_credentials() -> None:
    with pytest.raises(TelegramError):
        validate("not-a-token", CHAT)
    with pytest.raises(TelegramError):
        validate(TOKEN, "not-a-chat")


def test_credentials_never_appear_in_a_repr() -> None:
    client = TelegramClient(bot_token=TOKEN, chat_id=CHAT)
    assert TOKEN not in repr(client)
    assert CHAT not in repr(client)


async def test_send_posts_to_the_fixed_origin() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = TelegramClient(
        bot_token=TOKEN, chat_id=CHAT, transport=httpx.MockTransport(handler)
    )

    assert await client.send("hello") is True
    assert seen[0].url.host == "api.telegram.org"
    assert seen[0].url.path.endswith("/sendMessage")
    await client.aclose()


async def test_a_failed_send_is_reported_not_raised() -> None:
    client = TelegramClient(
        bot_token=TOKEN,
        chat_id=CHAT,
        transport=httpx.MockTransport(lambda request: httpx.Response(429)),
    )
    assert await client.send("hello") is False
    await client.aclose()


async def test_a_network_error_is_reported_not_raised() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = TelegramClient(
        bot_token=TOKEN, chat_id=CHAT, transport=httpx.MockTransport(explode)
    )
    assert await client.send("hello") is False
    await client.aclose()


def test_success_message_lists_the_outcome(config: WatchConfig) -> None:
    run = make_run(
        plan=Plan(identity=IDENTITY, moves=(), unmapped=("O1", "O2")),
        result=RunResult(
            moved=12, archived=3, subtitles_acquired=12, duplicates=("ep01.mkv",)
        ),
    )

    text = render(run, config)

    assert "整理完成" in text
    assert "[Group] Show S01" in text
    assert "Show (2024)" in text
    assert "移动 12" in text and "归档 3" in text and "字幕 12" in text
    assert "ep01.mkv" in text
    assert "未映射 2 个文件" in text


def test_attention_message_carries_the_reason(config: WatchConfig) -> None:
    run = make_run(
        state=RunState.NEEDS_ATTENTION,
        error={"code": "agent_reported_problem", "reason": "two series mixed"},
    )

    text = render(run, config)

    assert "需要处理" in text
    assert "agent_reported_problem" in text
    assert "two series mixed" in text


def test_long_lists_are_summarized(config: WatchConfig) -> None:
    run = make_run(
        result=RunResult(missing=tuple(f"ep{index}.mkv" for index in range(9)))
    )
    assert "等 9 个" in render(run, config)


class StubClients:
    def __init__(self, credentials) -> None:
        self.credentials = credentials

    async def telegram(self):
        return self.credentials


async def test_notifier_is_silent_when_telegram_is_not_configured(
    config: WatchConfig,
) -> None:
    notifier = TelegramNotifier(StubClients(None))
    await notifier.run_settled(make_run(), config)


async def test_notifier_survives_malformed_stored_credentials(
    config: WatchConfig,
) -> None:
    notifier = TelegramNotifier(StubClients(("bad-token", CHAT)))
    await notifier.run_settled(make_run(), config)
