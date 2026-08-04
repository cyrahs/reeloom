"""Fixed-purpose Telegram Bot API outbound adapter."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass

import httpx

from reeloom.server.notification_outbox import (
    DeliveryErrorCode,
    DeliveryResult,
)
from reeloom.server.notifications import RenderedNotification

_ORIGIN = "https://api.telegram.org"
_TOKEN = re.compile(r"^[0-9]{5,20}:[A-Za-z0-9_-]{20,128}$")
_CHAT_ID = re.compile(r"^-?[0-9]{1,20}$")


def validate_bot_token(value: object) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise ValueError("invalid Telegram bot token")
    return value


def validate_chat_id(value: object) -> str:
    if not isinstance(value, str) or _CHAT_ID.fullmatch(value) is None:
        raise ValueError("invalid Telegram chat id")
    return value


@dataclass(frozen=True, slots=True)
class TelegramHttpLimits:
    timeout_seconds: float = 5.0
    max_response_bytes: int = 16_384

    def __post_init__(self) -> None:
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not 0 < self.timeout_seconds <= 30
            or type(self.max_response_bytes) is not int
            or not 1_024 <= self.max_response_bytes <= 65_536
        ):
            raise ValueError("invalid Telegram HTTP limits")


class TelegramHttpAdapter:
    """Send one bounded notification to one fixed chat and API origin."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        limits: TelegramHttpLimits | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.__bot_token = validate_bot_token(bot_token)
        self.__chat_id = validate_chat_id(chat_id)
        self._limits = limits or TelegramHttpLimits()
        self._send_lock = threading.Lock()
        self._client = httpx.Client(
            base_url=_ORIGIN,
            headers={
                "Accept": "application/json",
                "User-Agent": "reeloom/0.1",
            },
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def __repr__(self) -> str:
        return (
            "TelegramHttpAdapter(bot_token=<redacted>, "
            "chat_id=<redacted>, "
            f"timeout_seconds={self._limits.timeout_seconds})"
        )

    def close(self) -> None:
        self._client.close()

    def send(self, notification: RenderedNotification) -> DeliveryResult:
        if not isinstance(notification, RenderedNotification):
            return DeliveryResult.failed(DeliveryErrorCode.INVALID_PAYLOAD)
        with self._send_lock:
            return self._send(notification)

    def _send(self, notification: RenderedNotification) -> DeliveryResult:
        fields = {
            "chat_id": self.__chat_id,
            "parse_mode": notification.parse_mode,
        }
        if notification.photo_url is None:
            method = "sendMessage"
            fields["text"] = notification.caption
        else:
            method = "sendPhoto"
            fields["photo"] = notification.photo_url
            fields["caption"] = notification.caption
        try:
            with self._client.stream(
                "POST",
                f"/bot{self.__bot_token}/{method}",
                data=fields,
                timeout=self._limits.timeout_seconds,
            ) as response:
                body = self._read_body(response)
                if response.status_code == 429:
                    retry_after = _retry_after(body)
                    if retry_after is None:
                        return DeliveryResult.failed(
                            DeliveryErrorCode.INVALID_RESPONSE
                        )
                    return DeliveryResult.failed(
                        DeliveryErrorCode.RATE_LIMITED,
                        retry_after_seconds=retry_after,
                    )
                if response.status_code >= 500:
                    return DeliveryResult.failed(
                        DeliveryErrorCode.SERVER_ERROR
                    )
                if not 200 <= response.status_code < 300:
                    return DeliveryResult.failed(
                        DeliveryErrorCode.CLIENT_ERROR
                    )
                message_id = _message_id(body)
                if message_id is None:
                    return DeliveryResult.failed(
                        DeliveryErrorCode.INVALID_RESPONSE
                    )
                return DeliveryResult.sent(message_id)
        except _ResponseTooLarge:
            return DeliveryResult.failed(DeliveryErrorCode.INVALID_RESPONSE)
        except httpx.TimeoutException:
            return DeliveryResult.failed(DeliveryErrorCode.TIMEOUT)
        except httpx.TransportError:
            return DeliveryResult.failed(DeliveryErrorCode.CONNECTION)

    def _read_body(self, response: httpx.Response) -> bytes:
        content_length = response.headers.get("content-length")
        if (
            content_length is not None
            and content_length.isdigit()
            and int(content_length) > self._limits.max_response_bytes
        ):
            raise _ResponseTooLarge
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > self._limits.max_response_bytes:
                raise _ResponseTooLarge
        return bytes(body)


class _ResponseTooLarge(Exception):
    pass


def _json_object(body: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if type(value) is not dict or not all(isinstance(key, str) for key in value):
        return None
    return value


def _message_id(body: bytes) -> int | None:
    value = _json_object(body)
    if value is None or value.get("ok") is not True:
        return None
    result = value.get("result")
    if type(result) is not dict:
        return None
    message_id = result.get("message_id")
    if (
        type(message_id) is not int
        or not 1 <= message_id <= (1 << 63) - 1
    ):
        return None
    return message_id


def _retry_after(body: bytes) -> int | None:
    value = _json_object(body)
    if value is None:
        return None
    parameters = value.get("parameters")
    if type(parameters) is not dict:
        return None
    retry_after = parameters.get("retry_after")
    if type(retry_after) is not int or not 1 <= retry_after <= 86_400:
        return None
    return retry_after
