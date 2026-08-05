from __future__ import annotations

from pathlib import Path

import pytest

from scripts import openai_live_config


def test_loads_openai_options_from_synthetic_dotenv(
    tmp_path: Path,
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "TMDB_API_KEY=ignored\n"
        "OPENAI_API_KEY='dotenv-secret'\n"
        'OPENAI_BASE_URL="https://gateway.example/v1/"\n'
        "OPENAI_MODEL=gpt-5.6\n"
        "OPENAI_REASONING_EFFORT=high\n",
        encoding="utf-8",
    )

    loaded = openai_live_config.load_openai_live_configuration(
        dotenv_path=dotenv,
        environ={},
    )

    assert loaded.api_key == "dotenv-secret"
    assert loaded.base_url == "https://gateway.example/v1"
    assert loaded.model_name == "gpt-5.6"
    assert loaded.reasoning_effort == "high"


def test_process_environment_overrides_dotenv_per_field(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "OPENAI_API_KEY=dotenv-secret\n"
        "OPENAI_BASE_URL=https://dotenv.example/v1\n"
        "OPENAI_MODEL=dotenv-model\n"
        "OPENAI_REASONING_EFFORT=medium\n",
        encoding="utf-8",
    )

    loaded = openai_live_config.load_openai_live_configuration(
        dotenv_path=dotenv,
        environ={
            "OPENAI_API_KEY": "process-secret",
            "OPENAI_BASE_URL": "https://process.example/api/v1",
            "OPENAI_MODEL": "process-model",
            "OPENAI_REASONING_EFFORT": "xhigh",
        },
    )

    assert loaded.api_key == "process-secret"
    assert loaded.base_url == "https://process.example/api/v1"
    assert loaded.model_name == "process-model"
    assert loaded.reasoning_effort == "xhigh"


def test_explicit_model_options_override_process_and_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "OPENAI_API_KEY=dotenv-secret\n"
        "OPENAI_MODEL=invalid model from dotenv\n"
        "OPENAI_REASONING_EFFORT=invalid-from-dotenv\n",
        encoding="utf-8",
    )

    loaded = openai_live_config.load_openai_live_configuration(
        dotenv_path=dotenv,
        environ={
            "OPENAI_MODEL": "invalid process model",
            "OPENAI_REASONING_EFFORT": "invalid-from-process",
        },
        model_name_override="explicit-model",
        reasoning_effort_override="high",
    )

    assert loaded.model_name == "explicit-model"
    assert loaded.reasoning_effort == "high"


def test_missing_base_url_defaults_to_official_endpoint(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENAI_API_KEY=dotenv-secret\n", encoding="utf-8")

    loaded = openai_live_config.load_openai_live_configuration(
        dotenv_path=dotenv,
        environ={},
    )

    assert loaded.base_url == openai_live_config.DEFAULT_OPENAI_BASE_URL
    assert loaded.model_name is None
    assert loaded.reasoning_effort is None


@pytest.mark.parametrize(
    ("name", "value", "error_code"),
    (
        ("OPENAI_MODEL", "model with spaces", "invalid_openai_model"),
        (
            "OPENAI_REASONING_EFFORT",
            "unbounded",
            "invalid_openai_reasoning_effort",
        ),
    ),
)
def test_rejects_invalid_model_options(
    tmp_path: Path,
    name: str,
    value: str,
    error_code: str,
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f"OPENAI_API_KEY=dotenv-secret\n{name}={value}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        openai_live_config.OpenAILiveConfigurationError,
        match=error_code,
    ):
        openai_live_config.load_openai_live_configuration(
            dotenv_path=dotenv,
            environ={},
        )


@pytest.mark.parametrize(
    "base_url",
    (
        "http://gateway.example/v1",
        "https://user:secret@gateway.example/v1",
        "https://gateway.example/v1?secret=value",
        "https://gateway.example/v1#fragment",
        "file:///tmp/socket",
    ),
)
def test_rejects_unsafe_base_urls(tmp_path: Path, base_url: str) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f"OPENAI_API_KEY=dotenv-secret\nOPENAI_BASE_URL={base_url}\n",
        encoding="utf-8",
    )

    with pytest.raises(
        openai_live_config.OpenAILiveConfigurationError,
        match="invalid_openai_base_url",
    ):
        openai_live_config.load_openai_live_configuration(
            dotenv_path=dotenv,
            environ={},
        )


def test_rejects_symlink_dotenv(tmp_path: Path) -> None:
    target = tmp_path / "configuration"
    target.write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")
    dotenv = tmp_path / ".env"
    dotenv.symlink_to(target)

    with pytest.raises(
        openai_live_config.OpenAILiveConfigurationError,
        match="dotenv_open_failed",
    ):
        openai_live_config.load_openai_live_configuration(
            dotenv_path=dotenv,
            environ={},
        )


def test_rejects_duplicate_openai_keys(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "OPENAI_API_KEY=first\nOPENAI_API_KEY=second\n",
        encoding="utf-8",
    )

    with pytest.raises(
        openai_live_config.OpenAILiveConfigurationError,
        match="duplicate_openai_api_key",
    ):
        openai_live_config.load_openai_live_configuration(
            dotenv_path=dotenv,
            environ={},
        )
