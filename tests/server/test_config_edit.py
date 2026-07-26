from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from reeloom.server.config import (
    ApplyPolicy,
    ArchiveRoute,
    ConfigDraft,
    ConfigRevision,
    ProviderConfig,
    ServerWorkType,
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
                    work_type=ServerWorkType.ANIME,
                    poll_interval_seconds=10,
                    settle_interval_seconds=60,
                ),
            ),
            archive_routes=(
                ArchiveRoute(
                    work_type=ServerWorkType.ANIME,
                    root=archive,
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
                    "work_type": "anime",
                    "poll_interval_seconds": 20,
                    "settle_interval_seconds": 60,
                }
            ],
            "archive_routes": [
                {
                    "work_type": "anime",
                    "root": {"mode": "retain"},
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
    assert edit.draft.archive_routes[0].root == (
        current.archive_routes[0].root
    )
    assert edit.draft.provider.secret_ref == "secret-existing"
    assert edit.replacement_api_key is None


def test_config_edit_rejects_retain_for_new_capability(
    tmp_path: Path,
) -> None:
    current = _current(tmp_path)

    with pytest.raises(ServerError) as raised:
        parse_config_edit(
            {
                "watches": [
                    {
                        "watch_id": "watch-new",
                        "root": {"mode": "retain"},
                        "work_type": "anime",
                        "poll_interval_seconds": 20,
                        "settle_interval_seconds": 60,
                    }
                ],
                "archive_routes": [
                    {
                        "work_type": "anime",
                        "root": {"mode": "retain"},
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
                    "work_type": "anime",
                    "poll_interval_seconds": 20,
                    "settle_interval_seconds": 60,
                }
            ],
            "archive_routes": [
                {
                    "work_type": "anime",
                    "root": {
                        "mode": "replace",
                        "path": str(archive),
                    },
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
    assert edit.replacement_api_key == b"new-key"


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
                        "work_type": "anime",
                        "poll_interval_seconds": 20,
                        "settle_interval_seconds": 60,
                    }
                ],
                "archive_routes": [
                    {
                        "work_type": "anime",
                        "root": {"mode": "retain"},
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
