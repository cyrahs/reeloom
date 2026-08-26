from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from reeloom.adapters.telegram import TelegramClient, TelegramError, clip, validate
from reeloom.models import (
    MediaIdentity,
    MediaType,
    Plan,
    Run,
    RunResult,
    RunState,
    WatchConfig,
)
from reeloom.server.composition import NotConfigured
from reeloom.server.notify import TelegramNotifier, render

from tests.fakes import FakeTmdb

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
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 7}}
        )

    client = TelegramClient(
        bot_token=TOKEN, chat_id=CHAT, transport=httpx.MockTransport(handler)
    )

    assert await client.send("hello") == 7
    assert seen[0].url.host == "api.telegram.org"
    assert seen[0].url.path.endswith("/sendMessage")
    assert parse_qs(seen[0].content.decode())["parse_mode"] == ["HTML"]
    await client.aclose()


def test_an_oversized_message_is_cut_at_a_line_boundary() -> None:
    # Cutting mid-tag would make Telegram reject the whole message.
    text = "Reeloom\n<b>kept</b>\n" + "x" * 40
    assert clip(text, 30) == "Reeloom\n<b>kept</b>"
    assert clip(text, 500) == text


async def test_a_failed_send_is_reported_not_raised() -> None:
    client = TelegramClient(
        bot_token=TOKEN,
        chat_id=CHAT,
        transport=httpx.MockTransport(lambda request: httpx.Response(429)),
    )
    assert await client.send("hello") is None
    await client.aclose()


async def test_a_network_error_is_reported_not_raised() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = TelegramClient(
        bot_token=TOKEN, chat_id=CHAT, transport=httpx.MockTransport(explode)
    )
    assert await client.send("hello") is None
    await client.aclose()


def test_success_message_lists_the_outcome(config: WatchConfig) -> None:
    run = make_run(
        plan=Plan(identity=IDENTITY, moves=(), unmapped=("O1", "O2")),
        result=RunResult(
            moved=12,
            archived=3,
            subtitles_moved=24,
            subtitles_acquired=12,
            subtitles_embedded=2,
            duplicates=("ep01.mkv",),
        ),
    )

    text = render(run, config)

    assert text.splitlines()[:2] == ["REELOOM · anime", "✅ 整理完成"]
    assert "移动 12" in text and "归档 3" in text
    assert "字幕 24" in text and "下载字幕 12" in text
    assert "内封 2" in text
    assert "重复 1" in text
    # Duplicate file names belong to the web UI, not the notification.
    assert "ep01.mkv" not in text
    # The intake folder name and the unmapped count belong to the web UI.
    assert "[Group] Show S01" not in text
    assert "未映射" not in text


def test_the_title_links_to_the_run_page(config: WatchConfig) -> None:
    run = make_run(plan=Plan(identity=IDENTITY, moves=()))

    assert (
        '<b><a href="https://reeloom.example/#/runs/run-1">Show (2024)</a></b>'
        in render(run, config, "https://reeloom.example")
    )


def test_the_title_falls_back_to_tmdb_without_a_public_url(
    config: WatchConfig,
) -> None:
    run = make_run(plan=Plan(identity=IDENTITY, moves=()))

    assert (
        '<b><a href="https://www.themoviedb.org/tv/123">Show (2024)</a></b>'
        in render(run, config)
    )


def test_a_movie_falls_back_to_the_tmdb_movie_page(config: WatchConfig) -> None:
    identity = MediaIdentity(MediaType.MOVIE, 77, "Film", 2020)
    run = make_run(plan=Plan(identity=identity, moves=()))

    assert 'href="https://www.themoviedb.org/movie/77"' in render(run, config)


def test_untrusted_text_cannot_inject_markup(config: WatchConfig) -> None:
    identity = MediaIdentity(MediaType.ANIME, 5, "<b>A & B</b>", 2024)
    run = make_run(
        plan=Plan(identity=identity, moves=()),
        result=RunResult(missing=("<i>ep01</i>.mkv",)),
    )

    text = render(run, config)

    assert "&lt;b&gt;A &amp; B&lt;/b&gt;" in text
    assert "&lt;i&gt;ep01&lt;/i&gt;.mkv" in text


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
    def __init__(self, credentials, tmdb=None, pin_alerts=True) -> None:
        self.credentials = credentials
        self._tmdb = tmdb
        self.pin_alerts = pin_alerts

    async def telegram(self):
        return self.credentials

    async def telegram_pin_alerts(self):
        return self.pin_alerts

    async def tmdb(self):
        if self._tmdb is None:
            raise NotConfigured("tmdb_not_configured")
        return self._tmdb


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


POSTER = "https://image.tmdb.org/t/p/w780/abc123.jpg"


def identified_run() -> Run:
    return make_run(plan=Plan(identity=IDENTITY, moves=()), result=RunResult(moved=3))


async def test_notifier_attaches_the_poster_when_identified(
    config: WatchConfig,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier(
        StubClients((TOKEN, CHAT), FakeTmdb(poster=POSTER)),
        transport=httpx.MockTransport(handler),
    )
    await notifier.run_settled(identified_run(), config)

    assert len(seen) == 1
    assert seen[0].url.path.endswith("/sendPhoto")
    fields = parse_qs(seen[0].content.decode())
    assert fields["photo"] == [POSTER]
    assert "整理完成" in fields["caption"][0]


async def test_notifier_falls_back_to_text_when_the_photo_is_rejected(
    config: WatchConfig,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/sendPhoto"):
            return httpx.Response(400, json={"ok": False})
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier(
        StubClients((TOKEN, CHAT), FakeTmdb(poster=POSTER)),
        transport=httpx.MockTransport(handler),
    )
    await notifier.run_settled(identified_run(), config)

    assert [request.url.path.rsplit("/", 1)[1] for request in seen] == [
        "sendPhoto",
        "sendMessage",
    ]


async def test_notifier_threads_the_public_url_into_the_message(
    config: WatchConfig,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier(
        StubClients((TOKEN, CHAT)),
        public_url="https://reeloom.example",
        transport=httpx.MockTransport(handler),
    )
    await notifier.run_settled(identified_run(), config)

    fields = parse_qs(seen[0].content.decode())
    assert "https://reeloom.example/#/runs/run-1" in fields["text"][0]


async def test_notifier_sends_plain_text_when_there_is_no_poster(
    config: WatchConfig,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    # No TMDB configured at all, and configured but the work has no poster:
    # both send the text message untouched.
    for clients in (
        StubClients((TOKEN, CHAT)),
        StubClients((TOKEN, CHAT), FakeTmdb(poster=None)),
    ):
        notifier = TelegramNotifier(clients, transport=transport)
        await notifier.run_settled(identified_run(), config)

    assert len(seen) == 2
    assert all(request.url.path.endswith("/sendMessage") for request in seen)


def test_replacement_lines_and_confirmation_hint(config: WatchConfig) -> None:
    run = make_run(
        plan=Plan(identity=IDENTITY, moves=()),
        result=RunResult(
            moved=12,
            replaced=("Show S01E01.mkv", "Show S01E02.mkv"),
            discarded=("dup01.mkv",),
            duplicates=("dup02.mkv",),
        ),
    )
    text = render(run, config)
    assert "洗版 2" in text
    assert "重复 2" in text
    assert "Show S01E01.mkv" not in text
    assert "dup01.mkv" not in text

    parked = make_run(
        state=RunState.NEEDS_ATTENTION,
        plan=Plan(identity=IDENTITY, moves=()),
        error={"code": "replace_confirmation", "groups": []},
    )
    text = render(parked, config)
    assert "等待洗版确认" in text
    assert "replace_confirmation" not in text


# ---- pinning ------------------------------------------------------------


def recording_transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": {"message_id": 7}}
        )

    return httpx.MockTransport(handler)


def methods(seen: list[httpx.Request]) -> list[str]:
    return [request.url.path.rsplit("/", 1)[1] for request in seen]


@pytest.mark.parametrize(
    "state", [RunState.NEEDS_ATTENTION, RunState.FAILED]
)
async def test_attention_and_failure_notifications_are_pinned(
    config: WatchConfig, state: RunState
) -> None:
    seen: list[httpx.Request] = []
    notifier = TelegramNotifier(
        StubClients((TOKEN, CHAT)), transport=recording_transport(seen)
    )
    await notifier.run_settled(make_run(state=state), config)

    assert methods(seen) == ["sendMessage", "pinChatMessage"]
    fields = parse_qs(seen[1].content.decode())
    assert fields["message_id"] == ["7"]
    # The message itself already notified; the pin must stay silent.
    assert fields["disable_notification"] == ["true"]


async def test_a_settled_run_is_not_pinned(config: WatchConfig) -> None:
    seen: list[httpx.Request] = []
    notifier = TelegramNotifier(
        StubClients((TOKEN, CHAT)), transport=recording_transport(seen)
    )
    await notifier.run_settled(make_run(state=RunState.DONE), config)

    assert methods(seen) == ["sendMessage"]


async def test_pinning_can_be_disabled(config: WatchConfig) -> None:
    seen: list[httpx.Request] = []
    notifier = TelegramNotifier(
        StubClients((TOKEN, CHAT), pin_alerts=False),
        transport=recording_transport(seen),
    )
    await notifier.run_settled(make_run(state=RunState.FAILED), config)

    assert methods(seen) == ["sendMessage"]


async def test_a_reply_without_a_message_id_skips_the_pin(
    config: WatchConfig,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier(
        StubClients((TOKEN, CHAT)), transport=httpx.MockTransport(handler)
    )
    await notifier.run_settled(make_run(state=RunState.FAILED), config)

    # Delivered but unpinnable must not fall back to a duplicate send.
    assert methods(seen) == ["sendMessage"]


# ---- test notifications --------------------------------------------------


async def test_send_test_sends_a_plain_and_a_pinned_attention_message() -> None:
    seen: list[httpx.Request] = []
    notifier = TelegramNotifier(
        StubClients((TOKEN, CHAT)), transport=recording_transport(seen)
    )

    assert await notifier.send_test() is None
    assert methods(seen) == ["sendMessage", "sendMessage", "pinChatMessage"]
    assert "普通测试通知" in parse_qs(seen[0].content.decode())["text"][0]
    assert "需要处理" in parse_qs(seen[1].content.decode())["text"][0]


async def test_send_test_honors_the_pin_switch() -> None:
    seen: list[httpx.Request] = []
    notifier = TelegramNotifier(
        StubClients((TOKEN, CHAT), pin_alerts=False),
        transport=recording_transport(seen),
    )

    assert await notifier.send_test() is None
    assert methods(seen) == ["sendMessage", "sendMessage"]


async def test_send_test_reports_missing_configuration() -> None:
    notifier = TelegramNotifier(StubClients(None))
    assert await notifier.send_test() == "telegram_not_configured"


async def test_send_test_reports_a_failed_send() -> None:
    notifier = TelegramNotifier(
        StubClients((TOKEN, CHAT)),
        transport=httpx.MockTransport(lambda request: httpx.Response(429)),
    )
    assert await notifier.send_test() == "send_failed"


# ---- magnet download alerts ---------------------------------------------


def make_download(**kwargs):
    from reeloom.models import DownloadState, MagnetDownload

    return MagnetDownload(
        id="dl-1",
        magnet="magnet:?xt=urn:btih:" + "c" * 40 + "&dn=<i>x</i>",
        info_hash="C" * 40,
        download_dir="/115/downloads",
        state=kwargs.pop("state", DownloadState.FAILED),
        **kwargs,
    )


def test_download_alert_carries_name_progress_and_reason() -> None:
    from reeloom.models import DownloadState
    from reeloom.server.notify import render_download

    text = render_download(
        make_download(
            state=DownloadState.STALLED,
            name="Show S01",
            progress=78.4,
            size_bytes=12_884_901_888,
            error="no progress for 1d at 78%",
        )
    )

    assert "REELOOM · 磁力下载" in text
    assert "下载停滞" in text
    assert "Show S01" in text
    assert "进度 78.4%" in text
    assert "12.0 GB" in text
    assert "目录：/115/downloads" in text
    assert "原因：no progress for 1d at 78%" in text


def test_download_alert_escapes_untrusted_text_and_falls_back_to_the_magnet() -> None:
    from reeloom.server.notify import render_download

    text = render_download(make_download(name=None))
    assert "下载失败" in text
    assert "magnet:?xt=urn:btih:" in text  # magnet head stands in for the name

    escaped = render_download(make_download(name="<i>evil</i> & co"))
    assert "&lt;i&gt;evil&lt;/i&gt; &amp; co" in escaped
    assert "<i>" not in escaped


async def test_download_alert_is_pinned_like_other_attention_states() -> None:
    seen: list[httpx.Request] = []
    notifier = TelegramNotifier(
        StubClients((TOKEN, CHAT)), transport=recording_transport(seen)
    )
    await notifier.download_trouble(make_download())
    assert methods(seen) == ["sendMessage", "pinChatMessage"]


async def test_download_alert_honors_the_pin_switch() -> None:
    seen: list[httpx.Request] = []
    notifier = TelegramNotifier(
        StubClients((TOKEN, CHAT), pin_alerts=False),
        transport=recording_transport(seen),
    )
    await notifier.download_trouble(make_download())
    assert methods(seen) == ["sendMessage"]


async def test_download_alert_is_silent_without_credentials() -> None:
    notifier = TelegramNotifier(StubClients(None))
    await notifier.download_trouble(make_download())  # must not raise
