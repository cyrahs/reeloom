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
    monkeypatch.setattr(openai_live_smoke, "_api_key", _unexpected)
    monkeypatch.setattr(openai_live_smoke.asyncio, "run", _unexpected)

    assert openai_live_smoke.main() == 2


def test_openai_smoke_requires_explicit_model_and_process_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        openai_live_smoke,
        "_parse_args",
        lambda: Namespace(live=True, model=None),
    )
    monkeypatch.setattr(openai_live_smoke, "_api_key", _unexpected)
    assert openai_live_smoke.main() == 2

    monkeypatch.setattr(
        openai_live_smoke,
        "_parse_args",
        lambda: Namespace(live=True, model="gpt-5.6"),
    )
    monkeypatch.setattr(openai_live_smoke, "_api_key", lambda: None)
    monkeypatch.setattr(openai_live_smoke.asyncio, "run", _unexpected)
    assert openai_live_smoke.main() == 2


def test_openai_smoke_reads_only_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "process-secret")

    assert openai_live_smoke._api_key() == "process-secret"


def test_openai_smoke_pricing_must_be_explicit_pair() -> None:
    with pytest.raises(ValueError):
        openai_live_smoke._pricing(
            Namespace(
                input_usd_per_million="1",
                output_usd_per_million=None,
            )
        )
