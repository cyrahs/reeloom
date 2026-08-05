from __future__ import annotations

from argparse import Namespace
from typing import NoReturn

import pytest

from scripts import openai_live_smoke


def _unexpected(_: object) -> NoReturn:
    pytest.fail("live OpenAI smoke attempted to run")


def test_openai_smoke_requires_explicit_live_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        openai_live_smoke,
        "_parse_args",
        lambda: Namespace(live=False),
    )
    monkeypatch.setattr(openai_live_smoke, "_live_configuration", _unexpected)
    monkeypatch.setattr(openai_live_smoke.asyncio, "run", _unexpected)

    assert openai_live_smoke.main() == 2


def test_openai_smoke_requires_explicit_model_and_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        openai_live_smoke,
        "_parse_args",
        lambda: Namespace(live=True, model=None),
    )
    monkeypatch.setattr(
        openai_live_smoke,
        "_live_configuration",
        lambda _: (_ for _ in ()).throw(
            openai_live_smoke.OpenAILiveConfigurationError(
                "missing_openai_api_key"
            )
        ),
    )
    assert openai_live_smoke.main() == 2

    monkeypatch.setattr(
        openai_live_smoke,
        "_parse_args",
        lambda: Namespace(live=True, model="gpt-5.6"),
    )
    monkeypatch.setattr(
        openai_live_smoke,
        "_live_configuration",
        lambda _: (_ for _ in ()).throw(
            openai_live_smoke.OpenAILiveConfigurationError(
                "missing_openai_api_key"
            )
        ),
    )
    monkeypatch.setattr(openai_live_smoke.asyncio, "run", _unexpected)
    assert openai_live_smoke.main() == 2


def test_openai_smoke_accepts_model_options_from_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _run(**kwargs: object) -> tuple[str, tuple[object, ...]]:
        captured.update(kwargs)
        return "dataset-hash", ()

    monkeypatch.setattr(
        openai_live_smoke,
        "_parse_args",
        lambda: Namespace(
            live=True,
            model=None,
            dataset=None,
            timeout_seconds=60,
            max_retries=2,
            organization=None,
            project=None,
            reasoning_effort=None,
            verbosity=None,
            input_usd_per_million=None,
            output_usd_per_million=None,
        ),
    )
    live_configuration = openai_live_smoke.OpenAILiveConfiguration(
        api_key="secret",
        base_url="https://gateway.example/v1",
        model_name="dotenv-model",
        reasoning_effort="high",
    )
    monkeypatch.setattr(
        openai_live_smoke,
        "_live_configuration",
        lambda _: live_configuration,
    )
    monkeypatch.setattr(openai_live_smoke, "_run", _run)

    assert openai_live_smoke.main() == 0
    assert captured["model_name"] == "dotenv-model"
    assert captured["reasoning_effort"] == "high"


def test_openai_smoke_pricing_must_be_explicit_pair() -> None:
    with pytest.raises(ValueError):
        openai_live_smoke._pricing(
            Namespace(
                input_usd_per_million="1",
                output_usd_per_million=None,
            )
        )
