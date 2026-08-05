from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from reeloom.server.config import (
    ApplyPolicy,
    AcgripConfig,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
    ServerWorkType,
    TelegramConfig,
    SubtitleAcquisitionPolicy,
    WatchConfig,
)
from reeloom.server.config_edit import parse_config_edit
from reeloom.server.errors import ServerError, ServerErrorCode


def _current(tmp_path: Path) -> ConfigRevision:
    watch = tmp_path / "watch"
    archive = tmp_path / "archive"
    watch.mkdir()
    archive.mkdir()
    return ConfigRevision.create(
        revision_id="cfg-1",
        revision=1,
        created_at=datetime(2026, 7, 26, tzinfo=UTC),
        draft=ConfigDraft(
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
            provider=ProviderConfig(
                base_url="https://models.example.test/v1",
                model="gpt-5",
                secret_ref="secret-existing",
            ),
            apply_policy=ApplyPolicy.MANUAL,
        ),
    )


def test_config_edit_retains_exact_revision_roots_and_secret(
    tmp_path: Path,
) -> None:
    current = _current(tmp_path)

    edit = parse_config_edit(
        {
            "watches": [
                {
                    "watch_id": "watch-1",
                    "root": {"mode": "retain"},
                    "library_root": {"mode": "retain"},
                    "work_type": "anime",
                    "poll_interval_seconds": 20,
                    "settle_interval_seconds": 60,
                }
            ],
            "provider": {
                "base_url": "https://models.example.test/v1",
                "model": "gpt-5",
                "reasoning_effort": None,
                "verbosity": None,
                "credential": {"mode": "retain"},
            },
            "apply_policy": "plan_only",
        },
        current=current,
    )

    assert edit.draft.watches[0].root == current.watches[0].root
    assert edit.draft.watches[0].library_root == (
        current.watches[0].library_root
    )
    assert edit.draft.provider.secret_ref == "secret-existing"
    assert edit.replacement_api_key is None
    assert edit.draft.agent_budget.max_elapsed_seconds == 600
    assert edit.draft.agent_budget.max_failures == 16
    assert not edit.draft.acgrip.enabled
    assert (
        edit.draft.subtitle_acquisition_policy
        is SubtitleAcquisitionPolicy.AUTOMATIC
    )


def test_config_edit_requires_explicit_acgrip_opt_in_and_separate_policy(
    tmp_path: Path,
) -> None:
    current = _current(tmp_path)
    value = {
        "watches": [],
        "provider": {
            "base_url": "https://models.example.test/v1",
            "model": "gpt-5",
            "reasoning_effort": None,
            "verbosity": None,
            "credential": {"mode": "retain"},
        },
        "apply_policy": "plan_only",
        "acgrip": {"enabled": True},
        "subtitle_acquisition_policy": "manual",
    }

    edit = parse_config_edit(value, current=current)

    assert edit.draft.acgrip == AcgripConfig(enabled=True)
    assert (
        edit.draft.subtitle_acquisition_policy
        is SubtitleAcquisitionPolicy.MANUAL
    )
    assert edit.draft.apply_policy is ApplyPolicy.PLAN_ONLY

    value["acgrip"] = {"enabled": True, "base_url": "https://evil.invalid"}
    with pytest.raises(ServerError) as raised:
        parse_config_edit(value, current=current)
    assert raised.value.code is ServerErrorCode.INVALID_CONFIG


def test_config_edit_accepts_explicit_agent_budget(tmp_path: Path) -> None:
    current = _current(tmp_path)
    value = {
        "watches": [],
        "provider": {
            "base_url": "https://models.example.test/v1",
            "model": "gpt-5",
            "reasoning_effort": None,
            "verbosity": None,
            "credential": {"mode": "retain"},
        },
        "apply_policy": "plan_only",
        "agent_budget": {
            "max_model_turns": 32,
            "max_tool_calls": 48,
            "max_failures": 2,
            "max_total_tokens": 250_000,
            "max_elapsed_seconds": 900,
        },
    }

    edit = parse_config_edit(value, current=current)

    assert edit.draft.agent_budget.max_model_turns == 32
    assert edit.draft.agent_budget.max_elapsed_seconds == 900
    value["agent_budget"]["unexpected"] = True
    with pytest.raises(ServerError) as raised:
        parse_config_edit(value, current=current)
    assert raised.value.code is ServerErrorCode.INVALID_CONFIG


@pytest.mark.parametrize(
    ("watch_id", "work_type"),
    [("watch-new", "anime"), ("watch-1", "movie")],
)
def test_config_edit_rejects_retain_for_new_or_retyped_watch(
    tmp_path: Path,
    watch_id: str,
    work_type: str,
) -> None:
    current = _current(tmp_path)

    with pytest.raises(ServerError) as raised:
        parse_config_edit(
            {
                "watches": [
                    {
                        "watch_id": watch_id,
                        "root": {"mode": "retain"},
                        "library_root": {"mode": "retain"},
                        "work_type": work_type,
                        "poll_interval_seconds": 20,
                        "settle_interval_seconds": 60,
                    }
                ],
                "provider": {
                    "base_url": "https://models.example.test/v1",
                    "model": "gpt-5",
                    "reasoning_effort": None,
                    "verbosity": None,
                    "credential": {"mode": "retain"},
                },
                "apply_policy": "manual",
            },
            current=current,
        )

    assert raised.value.code is ServerErrorCode.INVALID_CONFIG


def test_config_edit_replace_requires_explicit_existing_paths(
    tmp_path: Path,
) -> None:
    watch = tmp_path / "new-watch"
    archive = tmp_path / "new-archive"
    watch.mkdir()
    archive.mkdir()

    edit = parse_config_edit(
        {
            "watches": [
                {
                    "watch_id": "watch-new",
                    "root": {
                        "mode": "replace",
                        "path": str(watch),
                    },
                    "library_root": {
                        "mode": "replace",
                        "path": str(archive),
                    },
                    "work_type": "anime",
                    "poll_interval_seconds": 20,
                    "settle_interval_seconds": 60,
                }
            ],
            "provider": {
                "base_url": "https://models.example.test/v1",
                "model": "gpt-5",
                "reasoning_effort": "high",
                "verbosity": "low",
                "credential": {
                    "mode": "replace",
                    "api_key": "new-key",
                },
            },
            "apply_policy": "manual",
        },
        current=None,
    )

    assert edit.draft.watches[0].root == watch.resolve()
    assert edit.draft.watches[0].library_root == archive.resolve()
    assert edit.replacement_api_key == b"new-key"


def test_config_edit_replaces_library_without_reauthorizing_source(
    tmp_path: Path,
) -> None:
    current = _current(tmp_path)
    library = tmp_path / "replacement-library"
    library.mkdir()

    edit = parse_config_edit(
        {
            "watches": [
                {
                    "watch_id": "watch-1",
                    "root": {"mode": "retain"},
                    "library_root": {
                        "mode": "replace",
                        "path": str(library),
                    },
                    "work_type": "anime",
                    "poll_interval_seconds": 10,
                    "settle_interval_seconds": 60,
                }
            ],
            "provider": {
                "base_url": "https://models.example.test/v1",
                "model": "gpt-5",
                "reasoning_effort": None,
                "verbosity": None,
                "credential": {"mode": "retain"},
            },
            "apply_policy": "manual",
        },
        current=current,
    )

    assert edit.draft.watches[0].root == current.watches[0].root
    assert edit.draft.watches[0].library_root == library.resolve()


def test_config_edit_rejects_mixed_legacy_and_retain_wire_formats(
    tmp_path: Path,
) -> None:
    current = _current(tmp_path)

    with pytest.raises(ServerError) as raised:
        parse_config_edit(
            {
                "watches": [
                    {
                        "watch_id": "watch-1",
                        "root": str(current.watches[0].root),
                        "library_root": {"mode": "retain"},
                        "work_type": "anime",
                        "poll_interval_seconds": 20,
                        "settle_interval_seconds": 60,
                    }
                ],
                "provider": {
                    "base_url": "https://models.example.test/v1",
                    "model": "gpt-5",
                    "reasoning_effort": None,
                    "verbosity": None,
                    "credential": {"mode": "retain"},
                },
                "apply_policy": "manual",
            },
            current=current,
        )

    assert raised.value.code is ServerErrorCode.INVALID_CONFIG


def test_config_edit_replaces_write_only_telegram_destination(
    tmp_path: Path,
) -> None:
    current = _current(tmp_path)
    edit = parse_config_edit(
        {
            "watches": [],
            "provider": {
                "base_url": current.provider.base_url,
                "model": current.provider.model,
                "reasoning_effort": None,
                "verbosity": None,
                "credential": {"mode": "retain"},
            },
            "apply_policy": "manual",
            "telegram": {
                "enabled": True,
                "notification_types": [
                    "plan_ready",
                    "archive_completed",
                ],
                "destination": {
                    "mode": "replace",
                    "bot_token": (
                        "123456789:abcdefghijklmnopqrstuvwxyz_123456789"
                    ),
                    "chat_id": "-1001234567890",
                },
            },
        },
        current=current,
    )

    assert edit.draft.telegram.enabled
    assert edit.draft.telegram.chat_id == "-1001234567890"
    assert edit.draft.telegram.secret_ref == "replacement-pending"
    assert edit.replacement_telegram_token is not None
    assert "123456789:" not in repr(edit.draft)


def test_config_edit_retains_or_leaves_telegram_unset(
    tmp_path: Path,
) -> None:
    current = _current(tmp_path)
    configured = ConfigRevision.create(
        revision_id="cfg-telegram",
        revision=2,
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
        draft=ConfigDraft(
            watches=current.watches,
            provider=current.provider,
            apply_policy=current.apply_policy,
            telegram=TelegramConfig(
                enabled=True,
                chat_id="-1001234567890",
                secret_ref="secret-telegram",
            ),
        ),
    )
    base = {
        "watches": [],
        "provider": {
            "base_url": configured.provider.base_url,
            "model": configured.provider.model,
            "reasoning_effort": None,
            "verbosity": None,
            "credential": {"mode": "retain"},
        },
        "apply_policy": "manual",
    }
    retained = parse_config_edit(
        {
            **base,
            "telegram": {
                "enabled": False,
                "notification_types": ["attention_required"],
                "destination": {"mode": "retain"},
            },
        },
        current=configured,
    )
    unset = parse_config_edit(
        {
            **base,
            "telegram": {
                "enabled": False,
                "notification_types": ["attention_required"],
                "destination": {"mode": "unset"},
            },
        },
        current=current,
    )

    assert retained.draft.telegram.secret_ref == "secret-telegram"
    assert retained.replacement_telegram_token is None
    assert unset.draft.telegram.secret_ref == ""
