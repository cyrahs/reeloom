from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from agents import ModelSettings
from agents.models.openai_responses import OpenAIResponsesModel
from openai import AsyncOpenAI
from openai.types.shared import Reasoning

_OFFICIAL_BASE_URL = "https://api.openai.com/v1"
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_CREDENTIAL_BYTES = 4096
_MAX_SCOPE_BYTES = 256
_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)
_VERBOSITY = frozenset({"low", "medium", "high"})
ReasoningEffort = Literal[
    "none", "minimal", "low", "medium", "high", "xhigh", "max"
]
Verbosity = Literal["low", "medium", "high"]


def _scope(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_SCOPE_BYTES
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ValueError(f"invalid {field}")
    return value


@dataclass(frozen=True, slots=True)
class OpenAIModelConfig:
    """Non-secret, reproducible configuration for the official Responses API."""

    model_name: str
    request_timeout_seconds: float = 60.0
    max_retries: int = 2
    organization: str | None = None
    project: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    verbosity: Verbosity | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.model_name, str)
            or _MODEL_NAME.fullmatch(self.model_name) is None
            or not isinstance(self.request_timeout_seconds, (int, float))
            or isinstance(self.request_timeout_seconds, bool)
            or not 0 < self.request_timeout_seconds <= 300
            or type(self.max_retries) is not int
            or not 0 <= self.max_retries <= 10
            or (
                self.reasoning_effort is not None
                and self.reasoning_effort not in _REASONING_EFFORTS
            )
            or (
                self.verbosity is not None
                and self.verbosity not in _VERBOSITY
            )
        ):
            raise ValueError("invalid OpenAI model configuration")
        object.__setattr__(
            self,
            "organization",
            _scope(self.organization, field="organization"),
        )
        object.__setattr__(
            self,
            "project",
            _scope(self.project, field="project"),
        )

    def model_settings(self) -> ModelSettings:
        return ModelSettings(
            reasoning=(
                Reasoning(effort=self.reasoning_effort)
                if self.reasoning_effort is not None
                else None
            ),
            verbosity=self.verbosity,
        )


class OpenAIModelProvider:
    """Own one explicit official OpenAI client; never loads configuration files."""

    def __init__(
        self,
        *,
        api_key: str,
        config: OpenAIModelConfig,
    ) -> None:
        if (
            not isinstance(api_key, str)
            or not api_key
            or len(api_key.encode("utf-8")) > _MAX_CREDENTIAL_BYTES
            or any(char.isspace() for char in api_key)
            or not isinstance(config, OpenAIModelConfig)
            or os.environ.get("OPENAI_CUSTOM_HEADERS") is not None
        ):
            raise ValueError("invalid OpenAI credentials or configuration")
        self.config = config
        self._client = AsyncOpenAI(
            api_key=api_key,
            admin_api_key="",
            organization=config.organization or "",
            project=config.project or "",
            webhook_secret="",
            base_url=_OFFICIAL_BASE_URL,
            timeout=float(config.request_timeout_seconds),
            max_retries=config.max_retries,
        )
        self._model = OpenAIResponsesModel(
            model=config.model_name,
            openai_client=self._client,
        )

    @property
    def model(self) -> OpenAIResponsesModel:
        return self._model

    async def close(self) -> None:
        await self._client.close()
