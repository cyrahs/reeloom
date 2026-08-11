"""Telegram Bot API sender.

Outbound only, one fixed chat, one fixed origin. No webhooks, no commands, no
inbound anything: a notification can never become an instruction.
"""

from __future__ import annotations

import logging
import re

import httpx

from reeloom.models import ReeloomError

_ORIGIN = "https://api.telegram.org"
_TOKEN = re.compile(r"^[0-9]{5,20}:[A-Za-z0-9_-]{20,128}$")
_CHAT_ID = re.compile(r"^-?[0-9]{1,20}$")
MAX_MESSAGE_CHARS = 4000
MAX_CAPTION_CHARS = 1024

_LOGGER = logging.getLogger(__name__)


class TelegramError(ReeloomError):
    pass


def validate(token: str, chat_id: str) -> None:
    if _TOKEN.fullmatch(token) is None:
        raise TelegramError("invalid_bot_token")
    if _CHAT_ID.fullmatch(chat_id) is None:
        raise TelegramError("invalid_chat_id")


class TelegramClient:
    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        validate(bot_token, chat_id)
        self.__token = bot_token
        self._chat_id = chat_id
        self._client = httpx.AsyncClient(
            base_url=_ORIGIN,
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def __repr__(self) -> str:
        return "TelegramClient(bot_token=<redacted>, chat_id=<redacted>)"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, text: str) -> bool:
        """Send one message. Returns False instead of raising on failure."""

        return await self._post(
            "sendMessage",
            {
                "chat_id": self._chat_id,
                "text": text[:MAX_MESSAGE_CHARS],
                "disable_web_page_preview": "true",
            },
        )

    async def send_photo(self, photo_url: str, caption: str) -> bool:
        """Send one photo with a caption. Returns False on failure."""

        return await self._post(
            "sendPhoto",
            {
                "chat_id": self._chat_id,
                "photo": photo_url,
                "caption": caption[:MAX_CAPTION_CHARS],
            },
        )

    async def _post(self, method: str, data: dict[str, str]) -> bool:
        try:
            response = await self._client.post(
                f"/bot{self.__token}/{method}", data=data
            )
        except httpx.HTTPError as error:
            _LOGGER.warning("telegram send failed: %s", type(error).__name__)
            return False
        if response.status_code != 200:
            _LOGGER.warning(
                "telegram rejected the message: status=%s", response.status_code
            )
            return False
        return True
