from __future__ import annotations

import asyncio

import pytest

import reeloom.adapters.openai_model as adapter
from reeloom.adapters.openai_model import (
    OpenAIModelConfig,
    OpenAIModelProvider,
)


def test_provider_uses_explicit_responses_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def close(self) -> None:
            captured["closed"] = True

    class FakeModel:
        def __init__(self, **kwargs: object) -> None:
            captured["model_kwargs"] = kwargs

    monkeypatch.setenv("OPENAI_BASE_URL", "https://attacker.invalid/v1")
    monkeypatch.setattr(adapter, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(adapter, "OpenAIResponsesModel", FakeModel)
    config = OpenAIModelConfig(
        model_name="gpt-5.6",
        base_url="https://gateway.example/openai/v1/",
        request_timeout_seconds=30,
        max_retries=1,
        project="project-1",
        reasoning_effort="low",
        verbosity="low",
    )

    provider = OpenAIModelProvider(
        api_key="explicit-secret",
        config=config,
    )
    asyncio.run(provider.close())

    assert captured["api_key"] == "explicit-secret"
    assert captured["base_url"] == "https://gateway.example/openai/v1"
    assert captured["timeout"] == 30.0
    assert captured["max_retries"] == 1
    assert captured["project"] == "project-1"
    assert captured["organization"] == ""
    assert captured["model_kwargs"] == {
        "model": "gpt-5.6",
        "openai_client": provider._client,
    }
    assert captured["closed"] is True
    assert config.model_settings().reasoning is not None
    assert config.model_settings().reasoning.effort == "low"
    assert config.model_settings().verbosity == "low"


def test_model_config_defaults_to_five_retries() -> None:
    config = OpenAIModelConfig(model_name="gpt-5.6")

    assert config.max_retries == 5
    assert config.base_url == "https://api.openai.com/v1"


@pytest.mark.parametrize(
    ("model_name", "timeout", "retries"),
    (
        ("", 60, 2),
        ("model with spaces", 60, 2),
        ("gpt-5.6", 0, 2),
        ("gpt-5.6", 60, 11),
    ),
)
def test_model_config_is_strict(
    model_name: str,
    timeout: float,
    retries: int,
) -> None:
    with pytest.raises(ValueError):
        OpenAIModelConfig(
            model_name=model_name,
            request_timeout_seconds=timeout,
            max_retries=retries,
        )


def test_model_config_rejects_unknown_behavior_settings() -> None:
    with pytest.raises(ValueError):
        OpenAIModelConfig(
            model_name="gpt-5.6",
            reasoning_effort="unbounded",
        )


@pytest.mark.parametrize(
    "base_url",
    (
        "http://api.example/v1",
        "https://user:secret@api.example/v1",
        "https://api.example/v1?query=value",
        "https://api.example/v1#fragment",
    ),
)
def test_model_config_rejects_unsafe_base_url(base_url: str) -> None:
    with pytest.raises(ValueError, match="invalid base_url"):
        OpenAIModelConfig(model_name="gpt-5.6", base_url=base_url)


def test_provider_rejects_blank_or_whitespace_credentials() -> None:
    config = OpenAIModelConfig(model_name="gpt-5.6")

    for api_key in ("", "secret with spaces", "\nsecret"):
        with pytest.raises(ValueError):
            OpenAIModelProvider(api_key=api_key, config=config)


def test_provider_rejects_ambient_custom_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS",
        "Authorization: Bearer attacker",
    )

    with pytest.raises(ValueError):
        OpenAIModelProvider(
            api_key="explicit-secret",
            config=OpenAIModelConfig(model_name="gpt-5.6"),
        )
