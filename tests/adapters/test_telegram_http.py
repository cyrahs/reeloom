from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from reeloom.adapters.telegram import (
    TelegramHttpAdapter,
    TelegramHttpLimits,
)
from reeloom.server.notification_outbox import DeliveryErrorCode
from reeloom.server.notifications import (
    NotificationType,
    RenderedNotification,
    TmdbPosterRef,
)

_TOKEN = "123456789:abcdefghijklmnopqrstuvwxyz_123456789"
_CHAT_ID = "-1001234567890"


def _response(status: int, value: object) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(value).encode(),
        headers={"content-type": "application/json"},
    )


def _notification(*, poster: bool) -> RenderedNotification:
    return RenderedNotification(
        notification_type=NotificationType.TEST,
        caption="*固定 caption*",
        poster=TmdbPosterRef("/poster.jpg") if poster else None,
    )


@pytest.mark.parametrize(
    ("poster", "method", "content_field"),
    ((True, "sendPhoto", "caption"), (False, "sendMessage", "text")),
)
def test_adapter_uses_fixed_origin_and_photo_or_text_fallback(
    poster: bool,
    method: str,
    content_field: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response(
            200,
            {
                "ok": True,
                "result": {"message_id": 42, "date": 1, "chat": {}},
            },
        )

    adapter = TelegramHttpAdapter(
        bot_token=_TOKEN,
        chat_id=_CHAT_ID,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = adapter.send(_notification(poster=poster))
    finally:
        adapter.close()

    assert result.message_id == 42
    assert len(requests) == 1
    request = requests[0]
    assert request.url.scheme == "https"
    assert request.url.host == "api.telegram.org"
    assert request.url.path == f"/bot{_TOKEN}/{method}"
    fields = parse_qs(request.content.decode())
    assert fields["chat_id"] == [_CHAT_ID]
    assert fields["parse_mode"] == ["MarkdownV2"]
    assert fields[content_field] == ["*固定 caption*"]
    if poster:
        assert fields["photo"] == [
            "https://image.tmdb.org/t/p/w780/poster.jpg"
        ]


def test_adapter_does_not_follow_redirect_or_expose_secrets() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            headers={"location": "https://attacker.invalid/collect"},
        )

    adapter = TelegramHttpAdapter(
        bot_token=_TOKEN,
        chat_id=_CHAT_ID,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = adapter.send(_notification(poster=False))
        representation = repr(adapter)
    finally:
        adapter.close()

    assert result.error_code is DeliveryErrorCode.CLIENT_ERROR
    assert len(requests) == 1
    assert _TOKEN not in representation
    assert _CHAT_ID not in representation
    assert "固定 caption" not in representation


def test_adapter_honors_bounded_telegram_retry_after() -> None:
    adapter = TelegramHttpAdapter(
        bot_token=_TOKEN,
        chat_id=_CHAT_ID,
        transport=httpx.MockTransport(
            lambda _: _response(
                429,
                {"ok": False, "parameters": {"retry_after": 17}},
            )
        ),
    )
    try:
        result = adapter.send(_notification(poster=False))
    finally:
        adapter.close()

    assert result.error_code is DeliveryErrorCode.RATE_LIMITED
    assert result.retry_after_seconds == 17


@pytest.mark.parametrize(
    ("response", "error"),
    (
        (_response(500, {"ok": False}), DeliveryErrorCode.SERVER_ERROR),
        (_response(401, {"ok": False}), DeliveryErrorCode.CLIENT_ERROR),
        (_response(200, {"ok": True}), DeliveryErrorCode.INVALID_RESPONSE),
        (
            _response(200, {"ok": True, "result": {}}),
            DeliveryErrorCode.INVALID_RESPONSE,
        ),
        (
            _response(
                200,
                {"ok": True, "result": {"message_id": 1 << 63}},
            ),
            DeliveryErrorCode.INVALID_RESPONSE,
        ),
        (
            httpx.Response(200, content=b"not-json"),
            DeliveryErrorCode.INVALID_RESPONSE,
        ),
    ),
)
def test_adapter_classifies_bounded_failures(
    response: httpx.Response,
    error: DeliveryErrorCode,
) -> None:
    adapter = TelegramHttpAdapter(
        bot_token=_TOKEN,
        chat_id=_CHAT_ID,
        transport=httpx.MockTransport(lambda _: response),
    )
    try:
        result = adapter.send(_notification(poster=False))
    finally:
        adapter.close()

    assert result.error_code is error
    assert result.message_id is None


def test_adapter_bounds_response_and_transport_errors() -> None:
    oversized = TelegramHttpAdapter(
        bot_token=_TOKEN,
        chat_id=_CHAT_ID,
        limits=TelegramHttpLimits(max_response_bytes=1_024),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"x" * 1_025)
        ),
    )
    try:
        assert oversized.send(
            _notification(poster=False)
        ).error_code is DeliveryErrorCode.INVALID_RESPONSE
    finally:
        oversized.close()

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    unavailable = TelegramHttpAdapter(
        bot_token=_TOKEN,
        chat_id=_CHAT_ID,
        transport=httpx.MockTransport(timeout),
    )
    try:
        assert unavailable.send(
            _notification(poster=False)
        ).error_code is DeliveryErrorCode.TIMEOUT
    finally:
        unavailable.close()


@pytest.mark.parametrize(
    ("token", "chat_id"),
    (("secret", _CHAT_ID), (_TOKEN, "@channel"), (_TOKEN, "")),
)
def test_adapter_rejects_unbounded_or_non_numeric_destination(
    token: str,
    chat_id: str,
) -> None:
    with pytest.raises(ValueError):
        TelegramHttpAdapter(bot_token=token, chat_id=chat_id)
