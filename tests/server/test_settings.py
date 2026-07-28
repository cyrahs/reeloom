from __future__ import annotations

from pathlib import Path

import pytest

from reeloom.server.errors import ServerError, ServerErrorCode
from reeloom.server.settings import DeploymentSettings


def test_settings_require_explicit_dsn_and_one_worker(tmp_path: Path) -> None:
    environ = {
        "REELOOM_POSTGRES_DSN": "postgresql://reeloom@db/reeloom",
        "REELOOM_STATE_ROOT": str(tmp_path),
        "REELOOM_WORKERS": "1",
        "REELOOM_TMDB_API_KEY": "tmdb-test-key",
    }

    settings = DeploymentSettings.from_environ(environ)

    assert settings.postgres_dsn == environ["REELOOM_POSTGRES_DSN"]
    assert settings.state_root == tmp_path.resolve()
    assert settings.workers == 1
    assert repr(settings).find("postgresql://") == -1
    assert str(tmp_path) not in repr(settings)


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"REELOOM_POSTGRES_DSN": ""}, ServerErrorCode.INVALID_SETTINGS),
        ({"REELOOM_WORKERS": "2"}, ServerErrorCode.MULTIPLE_WORKERS),
        ({"REELOOM_WORKERS": "nope"}, ServerErrorCode.INVALID_SETTINGS),
        ({"REELOOM_STATE_ROOT": "relative"}, ServerErrorCode.INVALID_SETTINGS),
        ({"REELOOM_TMDB_API_KEY": ""}, ServerErrorCode.INVALID_SETTINGS),
    ],
)
def test_settings_fail_closed(
    tmp_path: Path,
    override: dict[str, str],
    code: ServerErrorCode,
) -> None:
    environ = {
        "REELOOM_POSTGRES_DSN": "postgresql://reeloom@db/reeloom",
        "REELOOM_STATE_ROOT": str(tmp_path),
        "REELOOM_WORKERS": "1",
        "REELOOM_TMDB_API_KEY": "tmdb-test-key",
        **override,
    }

    with pytest.raises(ServerError) as raised:
        DeploymentSettings.from_environ(environ)

    assert raised.value.code is code
    assert "postgresql://" not in str(raised.value)


def test_settings_preserve_state_root_symlink_for_no_follow_validation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    state_root = tmp_path / "state"
    state_root.symlink_to(target, target_is_directory=True)

    settings = DeploymentSettings.from_environ(
        {
            "REELOOM_POSTGRES_DSN": "postgresql://reeloom@db/reeloom",
            "REELOOM_STATE_ROOT": str(state_root),
            "REELOOM_TMDB_API_KEY": "tmdb-test-key",
        }
    )

    assert settings.state_root == state_root
    assert settings.state_root.is_symlink()
