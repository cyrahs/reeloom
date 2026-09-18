"""Credentials must never reach the logs: not via httpx's request line, not
via an adapter's own warning, not via a chained cause in a traceback."""

from __future__ import annotations

import logging

import httpx
import pytest

from reeloom.adapters.llm import Conversation, ModelError, OpenAICompatibleModel
from reeloom.adapters.telegram import TelegramClient
from reeloom.adapters.tmdb import TmdbClient, TmdbError
from reeloom.redact import RedactingFormatter, redact
from reeloom.server import main

KEY = "tmdb-secret-key-0123456789abcdef"
TOKEN = "123456789:AAEabcdefghijklmnopqrstuvwxyz0123456"
LLM_KEY = "sk-live-secret-9876543210"


def exploding(request: httpx.Request) -> httpx.Response:
    # Worst case for a transport error: the message quotes the full URL.
    raise httpx.ConnectError(f"cannot reach {request.url}", request=request)


def render(message: str, error: BaseException | None = None) -> str:
    """Format one record the way the production root handler would."""

    exc_info = (type(error), error, error.__traceback__) if error else None
    record = logging.LogRecord(
        "reeloom.test", logging.ERROR, __file__, 0, message, None, exc_info
    )
    return RedactingFormatter(main.LOG_FORMAT).format(record)


def test_httpx_request_lines_lose_the_key_and_the_token() -> None:
    tmdb = (
        "HTTP Request: GET https://api.themoviedb.org/3/search/tv"
        f'?query=x&language=zh-CN&api_key={KEY} "HTTP/1.1 200 OK"'
    )
    telegram = (
        f"HTTP Request: POST https://api.telegram.org/bot{TOKEN}/sendPhoto"
        ' "HTTP/1.1 200 OK"'
    )
    for line in (tmdb, telegram):
        rendered = render(line)
        assert KEY not in rendered and TOKEN not in rendered
    assert "api_key=<redacted> " in render(tmdb)
    assert "/bot<redacted>/sendPhoto" in render(telegram)
    assert redact("nothing secret here") == "nothing secret here"


async def test_a_tmdb_failure_never_shows_the_key_even_in_a_traceback() -> None:
    client = TmdbClient(KEY, transport=httpx.MockTransport(exploding))
    with pytest.raises(TmdbError) as info:
        await client.search("frieren", movie=False)
    await client.aclose()
    error = info.value

    # The raw httpx cause is the dangerous object; reeloom's own error is not.
    assert KEY in str(error.__cause__)
    assert KEY not in str(error)
    assert "ConnectError: cannot reach" in str(error)

    rendered = render("run failed", error)
    assert KEY not in rendered
    assert "api_key=<redacted>" in rendered
    assert "ConnectError" in rendered


async def test_a_telegram_failure_log_never_shows_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = TelegramClient(
        bot_token=TOKEN, chat_id="42", transport=httpx.MockTransport(exploding)
    )
    with caplog.at_level(logging.WARNING, logger="reeloom.adapters.telegram"):
        assert await client.send("hello") is None
    await client.aclose()

    assert "telegram send failed: ConnectError" in caplog.text
    assert TOKEN not in caplog.text
    assert TOKEN not in render(caplog.text)


async def test_a_model_error_never_echoes_the_api_key() -> None:
    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"message": f"Incorrect API key: {LLM_KEY}"}}
        )

    model = OpenAICompatibleModel(
        base_url="https://api.example.com/v1",
        api_key=LLM_KEY,
        model="m",
        transport=httpx.MockTransport(reject),
    )
    with pytest.raises(ModelError) as info:
        await model.complete(Conversation(), [])
    await model.aclose()
    assert LLM_KEY not in str(info.value)
    assert "Incorrect API key" in str(info.value)


async def test_configure_logging_silences_http_clients_and_redacts_root() -> None:
    root = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    saved = (root.level, list(root.handlers), httpx_logger.level)
    captured: list[logging.LogRecord] = []
    spy = logging.Handler()
    spy.emit = captured.append  # type: ignore[method-assign]
    try:
        main.configure_logging()
        assert root.handlers
        assert all(
            isinstance(handler.formatter, RedactingFormatter)
            for handler in root.handlers
        )
        assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
        assert httpx_logger.getEffectiveLevel() == logging.WARNING

        # A real request: httpx would log its URL at INFO; nothing must arrive.
        httpx_logger.addHandler(spy)
        client = TmdbClient(
            KEY,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"results": []})
            ),
        )
        assert (await client.search("frieren", movie=False)).hits == ()
        await client.aclose()
        assert captured == []
    finally:
        httpx_logger.removeHandler(spy)
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved[1]:
            root.addHandler(handler)
        root.setLevel(saved[0])
        httpx_logger.setLevel(saved[2])
        logging.getLogger("httpcore").setLevel(logging.NOTSET)
