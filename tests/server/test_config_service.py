from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reeloom.server.config import (
    ApplyPolicy,
    ConfigDraft,
    ConfigDraftInput,
    ProviderConfig,
    ProviderConfigInput,
    ServerWorkType,
    TelegramConfig,
    WatchConfig,
)
from reeloom.server.config_service import ConfigService
from reeloom.server.errors import ServerError, ServerErrorCode


@dataclass
class _Secrets:
    values: list[bytes] = field(default_factory=list)

    def put(self, value: bytes) -> str:
        self.values.append(value)
        return f"secret-{len(self.values)}"


@dataclass
class _Configs:
    head: object | None = None

    def compare_and_append(self, *, expected_revision: int, revision: object) -> object:
        current = 0 if self.head is None else self.head.revision
        if current != expected_revision:
            raise ServerError(ServerErrorCode.CONFIG_CONFLICT)
        self.head = revision
        return revision


def test_config_service_persists_secret_before_config(
    tmp_path: Path,
) -> None:
    watch = tmp_path / "watch"
    archive = tmp_path / "archive"
    watch.mkdir()
    archive.mkdir()
    secrets = _Secrets()
    configs = _Configs()
    service = ConfigService(
        configs=configs,
        secrets=secrets,
        clock=lambda: datetime(2026, 7, 25, tzinfo=UTC),
        id_factory=lambda: "cfg-1",
    )

    result = service.compare_and_append(
        expected_revision=0,
        value=ConfigDraftInput(
            watches=(
                WatchConfig(
                    watch_id="watch-1",
                    root=watch,
                    library_root=archive,
                    work_type=ServerWorkType.ANIME,
                    poll_interval_seconds=10,
                    settle_interval_seconds=60,
                ),
            ),
            provider=ProviderConfigInput(
                base_url="https://models.example.test/v1",
                model="gpt-5",
                api_key=b"key-value",
            ),
            apply_policy=ApplyPolicy.PLAN_ONLY,
        ),
    )

    assert secrets.values == [b"key-value"]
    assert result.provider.secret_ref == "secret-1"
    assert result.revision == 1
    assert result.agent_budget.max_elapsed_seconds == 600
    assert result.agent_budget.max_failures == 16


def test_config_cas_failure_leaves_only_unreferenced_secret(
    tmp_path: Path,
) -> None:
    secrets = _Secrets()
    configs = _Configs()
    service = ConfigService(
        configs=configs,
        secrets=secrets,
    )
    value = ConfigDraftInput(
        watches=(),
        provider=ProviderConfigInput(
            base_url="https://models.example.test/v1",
            model="gpt-5",
            api_key=b"orphan",
        ),
        apply_policy=ApplyPolicy.MANUAL,
    )

    with pytest.raises(ServerError) as raised:
        service.compare_and_append(expected_revision=9, value=value)

    assert raised.value.code is ServerErrorCode.CONFIG_CONFLICT
    assert secrets.values == [b"orphan"]
    assert configs.head is None


def test_config_service_writes_telegram_token_once_and_keeps_it_private() -> None:
    secrets = _Secrets()
    configs = _Configs()
    service = ConfigService(
        configs=configs,
        secrets=secrets,
        clock=lambda: datetime(2026, 8, 3, tzinfo=UTC),
        id_factory=lambda: "cfg-telegram",
    )

    result = service.compare_and_append_draft(
        expected_revision=0,
        draft=ConfigDraft(
            watches=(),
            provider=ProviderConfig(
                base_url="https://models.example.test/v1",
                model="gpt-5",
                secret_ref="secret-provider",
            ),
            apply_policy=ApplyPolicy.MANUAL,
            telegram=TelegramConfig(
                enabled=True,
                chat_id="-1001234567890",
                secret_ref="replacement-pending",
            ),
        ),
        replacement_telegram_token=(
            b"123456789:abcdefghijklmnopqrstuvwxyz_123456789"
        ),
    )

    assert secrets.values == [
        b"123456789:abcdefghijklmnopqrstuvwxyz_123456789"
    ]
    assert result.telegram.secret_ref == "secret-1"
    assert "123456789:" not in repr(result.public_payload())
