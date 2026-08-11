"""OpenAI-compatible chat client.

Just enough of the API to run a sequential tool loop: no SDK, no session
persistence, no provider registry. The offline tests substitute
``ScriptedModel``, which implements the same two-method surface.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from reeloom.models import ReeloomError

_MAX_RESPONSE_BYTES = 1024 * 1024

_LOGGER = logging.getLogger(__name__)


class ModelError(ReeloomError):
    pass


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str
    """Raw JSON text; malformed arguments are a case the loop must handle."""

    def parsed(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments or "{}")
        except json.JSONDecodeError as error:
            raise ModelError("malformed_tool_arguments", tool=self.name) from error
        if not isinstance(value, dict):
            raise ModelError("tool_arguments_not_object", tool=self.name)
        return value


@dataclass(frozen=True, slots=True)
class ModelReply:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Conversation:
    messages: list[dict[str, Any]] = field(default_factory=list)

    def system(self, content: str) -> None:
        self.messages.append({"role": "system", "content": content})

    def user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def assistant(self, reply: ModelReply) -> None:
        message: dict[str, Any] = {"role": "assistant", "content": reply.content}
        if reply.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in reply.tool_calls
            ]
        self.messages.append(message)

    def tool_result(self, call: ToolCall, payload: Any) -> None:
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(payload, ensure_ascii=False),
            }
        )


class Model(Protocol):
    async def complete(
        self, conversation: Conversation, tools: list[dict[str, Any]]
    ) -> ModelReply: ...


class OpenAICompatibleModel:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        reasoning_effort: str = "",
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url.startswith("https://") and "localhost" not in base_url:
            raise ModelError("insecure_base_url", base_url=base_url)
        if not model:
            raise ModelError("missing_model")
        self.__api_key = api_key
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )

    def __repr__(self) -> str:
        return f"OpenAICompatibleModel(model={self._model!r}, api_key=<redacted>)"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self, conversation: Conversation, tools: list[dict[str, Any]]
    ) -> ModelReply:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": conversation.messages,
            # One call at a time keeps the domain actions strictly ordered.
            "parallel_tool_calls": False,
        }
        if self._reasoning_effort:
            # Left out entirely when unset so providers that don't know the
            # parameter never see it.
            body["reasoning_effort"] = self._reasoning_effort
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        try:
            response = await self._client.post("/chat/completions", json=body)
        except httpx.TimeoutException as error:
            raise ModelError("model_timeout") from error
        except httpx.TransportError as error:
            raise ModelError("model_unreachable") from error

        if response.status_code >= 400:
            raise ModelError(
                "model_error",
                status=response.status_code,
                detail=response.text[:400],
            )
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise ModelError("model_response_too_large")

        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise ModelError("model_empty_response")
        message = choices[0].get("message") or {}
        usage = payload.get("usage") or {}
        return ModelReply(
            content=str(message.get("content") or ""),
            tool_calls=tuple(
                ToolCall(
                    id=str(call.get("id") or ""),
                    name=str((call.get("function") or {}).get("name") or ""),
                    arguments=str(
                        (call.get("function") or {}).get("arguments") or "{}"
                    ),
                )
                for call in message.get("tool_calls") or []
            ),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )
