from __future__ import annotations

import json

import httpx

from reeloom.adapters.llm import Conversation, OpenAICompatibleModel


def model_capturing(
    bodies: list[dict], **kwargs: str
) -> OpenAICompatibleModel:
    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {}},
        )

    return OpenAICompatibleModel(
        base_url="https://api.example.com/v1",
        api_key="key",
        model="gpt-5",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def test_reasoning_effort_is_sent_when_configured() -> None:
    bodies: list[dict] = []
    model = model_capturing(bodies, reasoning_effort="high")
    conversation = Conversation()
    conversation.user("hi")
    await model.complete(conversation, [])
    await model.aclose()
    assert bodies[0]["reasoning_effort"] == "high"


async def test_reasoning_effort_is_absent_by_default() -> None:
    bodies: list[dict] = []
    model = model_capturing(bodies)
    conversation = Conversation()
    conversation.user("hi")
    await model.complete(conversation, [])
    await model.aclose()
    assert "reasoning_effort" not in bodies[0]
